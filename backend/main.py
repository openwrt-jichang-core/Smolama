import json
import os
import threading
import time
from collections import defaultdict, deque
from datetime import datetime
from pathlib import Path

import requests
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from auth import AuthManager
from scanner import ScanState, run_advanced_tests, start_scan_thread

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
HOSTS_FILE = DATA_DIR / "hosts.json"
RESULTS_FILE = DATA_DIR / "scan_results.json"
LEADERBOARD_FILE = DATA_DIR / "leaderboard.json"
PING_STATUS_FILE = DATA_DIR / "ping_status.json"

SESSION_COOKIE = "scanner_session"
# 部署在 Coolify/反代之后走 HTTPS 时应保持默认 true；纯本地 http 调试可设 COOKIE_SECURE=false
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "true").lower() != "false"

app = FastAPI(title="Ollama Cluster Scanner")
state = ScanState()
auth_mgr = AuthManager(DATA_DIR)

# ---------------------------------------------------------------------------
# 简单的进程内限流：防止接口被刷（爆破登录之外的一般性滥用/流量攻击）
# ---------------------------------------------------------------------------
RATE_LIMIT_WINDOW = 60
RATE_LIMIT_MAX_REQUESTS = 180
_rate_log = defaultdict(deque)
_rate_lock = threading.Lock()


def get_client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


@app.middleware("http")
async def rate_limit_middleware(request: Request, call_next):
    ip = get_client_ip(request)
    now = time.time()
    with _rate_lock:
        dq = _rate_log[ip]
        while dq and now - dq[0] > RATE_LIMIT_WINDOW:
            dq.popleft()
        if len(dq) >= RATE_LIMIT_MAX_REQUESTS:
            return JSONResponse({"detail": "请求过于频繁，请稍后再试"}, status_code=429)
        dq.append(now)
    return await call_next(request)


@app.middleware("http")
async def security_headers_middleware(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "style-src 'self' https://fonts.googleapis.com 'unsafe-inline'; "
        "font-src https://fonts.gstatic.com; "
        "script-src 'self'; "
        "connect-src 'self'; "
        "img-src 'self' data:; "
        "frame-ancestors 'none'"
    )
    return response


# ---------------------------------------------------------------------------
# 鉴权：密码登录 + 会话 Cookie + 失败次数指数退避锁定
# ---------------------------------------------------------------------------


def require_auth(request: Request):
    token = request.cookies.get(SESSION_COOKIE)
    if not auth_mgr.validate_session(token):
        raise HTTPException(status_code=401, detail="未登录或会话已过期，请重新登录")
    return True


class LoginIn(BaseModel):
    password: str


@app.post("/api/login")
def login(body: LoginIn, request: Request, response: Response):
    ip = get_client_ip(request)
    locked_remain = auth_mgr.is_locked(ip)
    if locked_remain > 0:
        raise HTTPException(
            status_code=429,
            detail=f"登录尝试次数过多，账户已锁定，请在 {int(locked_remain) + 1} 秒后重试",
        )
    if not auth_mgr.verify_password(body.password):
        auth_mgr.register_failure(ip)
        remain = auth_mgr.is_locked(ip)
        if remain > 0:
            raise HTTPException(
                status_code=429,
                detail=f"密码错误次数过多，已锁定 {int(remain)} 秒后再试",
            )
        raise HTTPException(status_code=401, detail="密码错误")

    auth_mgr.register_success(ip)
    token = auth_mgr.create_session()
    response.set_cookie(
        SESSION_COOKIE,
        token,
        httponly=True,
        samesite="strict",
        secure=COOKIE_SECURE,
        max_age=12 * 3600,
        path="/",
    )
    return {"status": "ok"}


@app.post("/api/logout")
def logout(request: Request, response: Response):
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        auth_mgr.destroy_session(token)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"status": "ok"}


@app.get("/api/session")
def session_check(request: Request):
    token = request.cookies.get(SESSION_COOKIE)
    return {"authenticated": auth_mgr.validate_session(token)}


@app.get("/")
def root(request: Request):
    token = request.cookies.get(SESSION_COOKIE)
    if not auth_mgr.validate_session(token):
        return RedirectResponse(url="/login.html")
    return FileResponse(str(Path(__file__).parent.parent / "static" / "index.html"))


# ---------------------------------------------------------------------------
# 主机管理
# ---------------------------------------------------------------------------


def load_hosts():
    """
    主机记录格式: {"url": str, "enabled": bool, "favorite": bool}
    自动兼容旧版本(纯字符串列表)数据, 迁移为新格式。
    """
    if not HOSTS_FILE.exists():
        return []
    try:
        raw = json.loads(HOSTS_FILE.read_text())
    except Exception:
        return []

    migrated = False
    hosts = []
    for item in raw:
        if isinstance(item, str):
            hosts.append({"url": item, "enabled": True, "favorite": False})
            migrated = True
        else:
            hosts.append({
                "url": item.get("url"),
                "enabled": item.get("enabled", True),
                "favorite": item.get("favorite", False),
            })
    if migrated:
        save_hosts(hosts)
    return hosts


def save_hosts(hosts):
    ordered = sorted(hosts, key=lambda h: not h.get("favorite", False))
    HOSTS_FILE.write_text(json.dumps(ordered, indent=2, ensure_ascii=False))


class HostIn(BaseModel):
    url: str


class HostPatch(BaseModel):
    url: str
    enabled: bool | None = None
    favorite: bool | None = None


def normalize_url(url: str) -> str:
    url = url.strip().rstrip("/")
    if not url:
        raise HTTPException(status_code=400, detail="地址不能为空")
    if len(url) > 512:
        raise HTTPException(status_code=400, detail="地址过长")
    if not url.startswith("http://") and not url.startswith("https://"):
        url = "http://" + url
    return url


def find_host(hosts, url):
    for h in hosts:
        if h["url"] == url:
            return h
    return None


@app.get("/api/hosts")
def get_hosts(auth=Depends(require_auth)):
    return load_hosts()


@app.post("/api/hosts")
def add_host(host: HostIn, auth=Depends(require_auth)):
    url = normalize_url(host.url)
    hosts = load_hosts()
    if find_host(hosts, url):
        raise HTTPException(status_code=400, detail="该地址已存在")
    hosts.append({"url": url, "enabled": True, "favorite": False})
    save_hosts(hosts)
    return load_hosts()


@app.patch("/api/hosts")
def patch_host(patch: HostPatch, auth=Depends(require_auth)):
    url = normalize_url(patch.url)
    hosts = load_hosts()
    h = find_host(hosts, url)
    if not h:
        raise HTTPException(status_code=404, detail="未找到该地址")
    if patch.enabled is not None:
        h["enabled"] = patch.enabled
    if patch.favorite is not None:
        h["favorite"] = patch.favorite
    save_hosts(hosts)
    return load_hosts()


@app.delete("/api/hosts")
def delete_host(host: HostIn, auth=Depends(require_auth)):
    url = normalize_url(host.url)
    hosts = load_hosts()
    h = find_host(hosts, url)
    if not h:
        raise HTTPException(status_code=404, detail="未找到该地址")
    hosts.remove(h)
    save_hosts(hosts)
    return load_hosts()


# ---------------------------------------------------------------------------
# 扫描控制
# ---------------------------------------------------------------------------


class ScanStartIn(BaseModel):
    concurrency: int = 3
    model_concurrency: int = 4


def _load_json_file(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def _save_json_file(path: Path, data):
    try:
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    except Exception:
        pass


CATEGORY_LABELS = {
    "core": "核心测试",
    "control": "控制性 (Agent 工具调用)",
    "language": "语言性",
}

# 已知模型系列的模糊匹配表：仅用于给排行榜提供"这个标签名字看起来属于哪个厂商系列"
# 的粗略参考，不是任何实时联网比对结果，也不代表对具体版本号的质量评分——
# Ollama 里的模型标签可以被使用者随意自定义命名(比如加上 "claude"、"gpt" 等字样)，
# 无法从名字本身可靠验证它是否真的是对应厂商发布的模型，请自行核实来源。
KNOWN_MODEL_FAMILIES = [
    ("qwen", "阿里云 Qwen 系列"),
    ("deepseek", "DeepSeek 系列"),
    ("glm", "智谱 GLM 系列"),
    ("llama", "Meta Llama 系列"),
    ("gemma", "Google Gemma 系列"),
    ("gemini", "Google Gemini 系列"),
    ("mistral", "Mistral AI 系列"),
    ("mixtral", "Mistral AI 系列"),
    ("phi", "Microsoft Phi 系列"),
    ("gpt-oss", "OpenAI 开源 GPT-OSS 系列"),
    ("minimax", "MiniMax 系列"),
    ("kimi", "月之暗面 Kimi 系列"),
    ("moonshot", "月之暗面 Kimi 系列"),
    ("nemotron", "NVIDIA Nemotron 系列"),
    ("command-r", "Cohere Command-R 系列"),
    ("yi", "零一万物 Yi 系列"),
    ("internlm", "上海AI实验室 InternLM 系列"),
    ("starcoder", "BigCode StarCoder 系列"),
    ("codellama", "Meta Code Llama 系列"),
    ("claude", "标签含 claude 字样(⚠️ Anthropic 并未向 Ollama 生态发布过官方模型，请务必自行核实来源，不要默认信任)"),
]


def guess_model_family(model_name):
    """模糊匹配模型标签属于哪个已知厂商系列，仅供参考，不代表验证过真实性"""
    name_lower = (model_name or "").lower()
    for keyword, label in KNOWN_MODEL_FAMILIES:
        if keyword in name_lower:
            return label
    return None


def update_leaderboard_from_results(results):
    """扫描/重测得到的结果并入排行榜持久化文件。
    每个 主机+模型 按 core/control/language 三个类别分别统计：
    某一类全部通过才参与该类排名(按该类总耗时升序)；有失败项的只额外记录，不参与排名。"""
    if not results:
        return
    lb = _load_json_file(LEADERBOARD_FILE, [])
    lb_map = {f"{e['host']}|{e['model']}": e for e in lb}
    for key, tests in results.get("advanced", {}).items():
        host, model = key.split("|", 1)
        _merge_leaderboard_entry(lb_map, host, model, tests)
    _save_json_file(LEADERBOARD_FILE, list(lb_map.values()))


def _merge_leaderboard_entry(lb_map, host, model, tests):
    key = f"{host}|{model}"
    entry = lb_map.get(key, {})
    categories = {}
    for cat in CATEGORY_LABELS:
        cat_tests = [t for t in tests if t.get("category", "core") == cat]
        total = len(cat_tests)
        if total == 0:
            continue
        passed = sum(1 for t in cat_tests if t.get("status") == "PASS")
        elapsed_total = round(sum(t.get("elapsed") or 0 for t in cat_tests), 2)
        categories[cat] = {
            "status": "pass" if passed == total else "fail",
            "passed": passed,
            "total": total,
            "elapsed_total": elapsed_total,
            "elapsed_avg": round(elapsed_total / total, 2) if total else None,
        }
    entry.update({
        "host": host,
        "model": model,
        "last_tested": datetime.now().isoformat(),
        "tests": tests,
        "categories": categories,
    })
    lb_map[key] = entry
    return entry


@app.post("/api/scan/start")
def scan_start(body: ScanStartIn = ScanStartIn(), auth=Depends(require_auth)):
    hosts = [h["url"] for h in load_hosts() if h.get("enabled", True)]
    if not hosts:
        raise HTTPException(status_code=400, detail="请先添加并启用至少一个主机地址")
    if state.running:
        raise HTTPException(status_code=409, detail="扫描已在进行中")
    concurrency = max(1, min(100, body.concurrency))
    model_concurrency = max(1, min(20, body.model_concurrency))
    ok = start_scan_thread(hosts, state, concurrency=concurrency, model_concurrency=model_concurrency)
    if not ok:
        raise HTTPException(status_code=409, detail="扫描已在进行中")
    return {"status": "started", "hosts": hosts, "concurrency": concurrency, "model_concurrency": model_concurrency}


@app.post("/api/scan/stop")
def scan_stop(auth=Depends(require_auth)):
    if not state.running:
        return {"status": "not_running"}
    state.request_stop()
    return {"status": "stopping"}


@app.get("/api/scan/status")
def scan_status(since: int = 0, auth=Depends(require_auth)):
    logs = state.get_logs_since(since)
    results = state.results
    if results is not None:
        _save_json_file(RESULTS_FILE, results)
        update_leaderboard_from_results(results)
    return JSONResponse({
        "running": state.running,
        "logs": logs,
        "results": results,
    })


@app.get("/api/scan/results")
def scan_results(auth=Depends(require_auth)):
    return JSONResponse(_load_json_file(RESULTS_FILE, {}))


# ---------------------------------------------------------------------------
# 排行榜：按响应耗时排名，可对单个 主机+模型 一键重新测试
# ---------------------------------------------------------------------------


@app.get("/api/leaderboard")
def get_leaderboard(auth=Depends(require_auth)):
    lb = _load_json_file(LEADERBOARD_FILE, [])
    result = {}
    for cat, label in CATEGORY_LABELS.items():
        ranked = []
        failed = []
        for e in lb:
            cat_data = (e.get("categories") or {}).get(cat)
            if not cat_data:
                continue
            row = {
                "host": e["host"],
                "model": e["model"],
                "family_hint": guess_model_family(e["model"]),
                "status": cat_data["status"],
                "passed": cat_data["passed"],
                "total": cat_data["total"],
                "elapsed_total": cat_data["elapsed_total"],
                "elapsed_avg": cat_data["elapsed_avg"],
                "last_tested": e.get("last_tested"),
                "error": e.get("error"),
            }
            if cat_data["status"] == "pass":
                ranked.append(row)
            else:
                failed.append(row)
        ranked.sort(key=lambda r: r["elapsed_total"] if r["elapsed_total"] is not None else float("inf"))
        for i, r in enumerate(ranked):
            r["rank"] = i + 1
        result[cat] = {"label": label, "ranked": ranked, "failed": failed}
    return result


class RetestIn(BaseModel):
    host: str
    model: str


@app.post("/api/leaderboard/retest")
def leaderboard_retest(body: RetestIn, auth=Depends(require_auth)):
    from scanner import quick_test  # 局部导入，避免和顶部导入顺序耦合

    host = normalize_url(body.host)
    model = body.model.strip()
    if not model:
        raise HTTPException(status_code=400, detail="模型名不能为空")

    lb = _load_json_file(LEADERBOARD_FILE, [])
    lb_map = {f"{e['host']}|{e['model']}": e for e in lb}

    viable, verr = quick_test(host, model)
    if not viable:
        entry = lb_map.get(f"{host}|{model}", {})
        entry.update({
            "host": host,
            "model": model,
            "categories": {},
            "last_tested": datetime.now().isoformat(),
            "tests": [],
            "error": f"主机/模型不可达: {verr}",
        })
        lb_map[f"{host}|{model}"] = entry
        _save_json_file(LEADERBOARD_FILE, list(lb_map.values()))
        return entry

    tmp_state = ScanState()  # 独立状态，不与正在进行的全量扫描互相干扰
    raw_tests = run_advanced_tests(host, model, tmp_state)
    tests = [
        {
            "test": name,
            "category": category,
            "status": status,
            "detail": (str(detail)[:300] if detail else None),
            "elapsed": elapsed,
        }
        for name, category, status, detail, elapsed in raw_tests
    ]
    entry = _merge_leaderboard_entry(lb_map, host, model, tests)
    _save_json_file(LEADERBOARD_FILE, list(lb_map.values()))
    return entry


# ---------------------------------------------------------------------------
# 侧边栏：每个主机 / 每个模型的连通性状态（绿色正常 / 红色失败），支持一键 Ping 重测
# ---------------------------------------------------------------------------


def _load_ping_status():
    return _load_json_file(PING_STATUS_FILE, {})


def _save_ping_status(host, model, result):
    status = _load_ping_status()
    status[f"{host}|{model}"] = result
    _save_json_file(PING_STATUS_FILE, status)


@app.get("/api/hosts/status")
def hosts_status(auth=Depends(require_auth)):
    """给左侧栏用：每个主机 -> 已知模型列表 及 各自的连通性状态。
    优先使用最近一次手动 Ping 的结果，没有 Ping 过则回退到最近一次扫描的可用性结果。"""
    hosts = load_hosts()
    results = _load_json_file(RESULTS_FILE, {})
    discovered = results.get("discovered", {})
    viability = results.get("viability", {})
    pings = _load_ping_status()

    out = []
    for h in hosts:
        url = h["url"]
        models = discovered.get(url, [])
        model_list = []
        for m in models:
            key = f"{url}|{m}"
            ping = pings.get(key)
            if ping:
                ok = ping.get("ok")
                checked_at = ping.get("ts")
                source = "ping"
            else:
                ok = viability.get(key)
                checked_at = results.get("generated_at")
                source = "scan"
            model_list.append({
                "model": m,
                "ok": ok,
                "last_checked": checked_at,
                "source": source,
            })
        out.append({
            "url": url,
            "enabled": h.get("enabled", True),
            "favorite": h.get("favorite", False),
            "models": model_list,
        })
    return out


class PingIn(BaseModel):
    host: str
    model: str


@app.post("/api/ping")
def ping_model(body: PingIn, auth=Depends(require_auth)):
    """侧边栏点击模型时调用：发一条"你好"，只关心是否能收到正常回复。"""
    host = normalize_url(body.host)
    model = body.model.strip()
    if not model:
        raise HTTPException(status_code=400, detail="模型名不能为空")

    start = time.time()
    try:
        resp = requests.post(
            f"{host}/api/generate",
            json={"model": model, "prompt": "你好", "stream": False},
            timeout=20,
        )
        elapsed = round(time.time() - start, 2)
        data = resp.json()
        if "error" in data:
            result = {"ok": False, "error": str(data["error"])[:300], "elapsed": elapsed}
        else:
            reply = (data.get("response") or "").strip()
            result = {"ok": bool(reply), "reply": reply[:200], "elapsed": elapsed}
    except Exception as e:
        result = {"ok": False, "error": str(e)[:300], "elapsed": round(time.time() - start, 2)}

    result.update({"host": host, "model": model, "ts": datetime.now().isoformat()})
    _save_ping_status(host, model, result)
    return result


# ---------------------------------------------------------------------------
# 静态资源（login.html 不需要鉴权；index.html 走上面的 "/" 路由做了鉴权拦截）
# ---------------------------------------------------------------------------

static_dir = Path(__file__).parent.parent / "static"
app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="static")
