import json
import os
import secrets
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta
from pathlib import Path

import requests
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from auth import AuthManager
from scanner import (
    ScanState,
    refresh_custom_core_cases,
    refresh_custom_language_cases,
    run_advanced_tests,
    start_scan_thread,
)

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
HOSTS_FILE = DATA_DIR / "hosts.json"
RESULTS_FILE = DATA_DIR / "scan_results.json"
LEADERBOARD_FILE = DATA_DIR / "leaderboard.json"
PING_STATUS_FILE = DATA_DIR / "ping_status.json"
AUDIT_LOG_FILE = DATA_DIR / "audit.json"
AUDIT_LOG_MAX_ENTRIES = 1000
SETTINGS_FILE = DATA_DIR / "settings.json"

# 历史趋势数据：只允许追加/读取/清理这一个固定路径，绝不接受任何用户输入拼出的路径，
# 避免被人通过参数注入去读写这个目录之外的任意文件（比如尝试路径穿越读 /etc/passwd 之类）。
HISTORY_FILE = DATA_DIR / "history.jsonl"
HISTORY_MAX_SIZE_BYTES_HARD_CAP = 200 * 1024 * 1024  # 200MB 硬上限，无论设置里怎么配都不会超过这个

DEFAULT_SETTINGS = {
    "schedule": {
        "enabled": False,
        "time": "09:00",       # 24小时制 HH:MM，本地时区（容器时区）
        "concurrency": 3,
        "model_concurrency": 4,
    },
    "notify": {
        "wecom": {"enabled": False, "webhook_url": ""},
        "telegram": {"enabled": False, "bot_token": "", "chat_id": ""},
        "bark": {"enabled": False, "key": "", "server": "https://api.day.app"},
        "email": {
            "enabled": False, "smtp_host": "", "smtp_port": 587,
            "username": "", "password": "", "from_addr": "", "to_addr": "",
            "use_tls": True,
        },
    },
    "history": {
        "retention_days": 180,     # 超过这个天数的记录会被定期清理自动删除
        "max_size_mb": 50,         # history.jsonl 超过这个大小时，从最旧的记录开始删，直到降到限制以下
        "auto_cleanup_enabled": True,
    },
    "share": {
        "enabled": False,
        "tokens": [],   # [{"token": str, "label": str, "created_at": iso str, "expires_at": iso str|None}]
    },
    "metrics": {
        "enabled": False,
        "token": "",    # Prometheus 抓取用的 token，通过 URL query 或 Authorization: Bearer 传入
    },
    "custom_language_tests": [],  # [{"name","prompt","rules":[...]}]，只做纯文本规则判定，不执行代码
    "custom_core_tests": [],      # [{"name","prompt","harness","expected"}]，会在沙箱子进程执行代码，新增需二次输入密码确认
}


def _load_json_file(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def _save_json_file(path: Path, data):
    """原子写：先写临时文件再 os.replace 覆盖，避免容器被强杀在写入中途导致 JSON 半写坏文件。"""
    try:
        tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}.{threading.get_ident()}")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False))
        os.replace(tmp, path)
    except Exception:
        pass


# 各持久化文件各自一把锁：保护"读整个文件 -> 内存改 -> 整体写回"这种非原子的
# read-modify-write 操作，避免并发请求互相覆盖导致更新丢失。
_hosts_lock = threading.Lock()
_leaderboard_lock = threading.Lock()
_ping_lock = threading.Lock()
_results_lock = threading.Lock()
_audit_lock = threading.Lock()
_settings_lock = threading.Lock()
_history_lock = threading.Lock()

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
_rate_cleanup_counter = 0
_RATE_CLEANUP_EVERY = 500  # 每处理这么多个请求，顺带清一次已经空掉的 IP 队列


def get_client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


@app.middleware("http")
async def rate_limit_middleware(request: Request, call_next):
    global _rate_cleanup_counter
    ip = get_client_ip(request)
    now = time.time()
    with _rate_lock:
        dq = _rate_log[ip]
        while dq and now - dq[0] > RATE_LIMIT_WINDOW:
            dq.popleft()
        if len(dq) >= RATE_LIMIT_MAX_REQUESTS:
            return JSONResponse({"detail": "请求过于频繁，请稍后再试"}, status_code=429)
        dq.append(now)

        _rate_cleanup_counter += 1
        if _rate_cleanup_counter >= _RATE_CLEANUP_EVERY:
            _rate_cleanup_counter = 0
            stale_ips = [k for k, v in _rate_log.items() if not v]
            for k in stale_ips:
                del _rate_log[k]
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
    totp_code: str | None = None


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
        _record_audit(request, "login_failed", f"ip={ip}")
        if remain > 0:
            raise HTTPException(
                status_code=429,
                detail=f"密码错误次数过多，已锁定 {int(remain)} 秒后再试",
            )
        raise HTTPException(status_code=401, detail="密码错误")

    if auth_mgr.is_totp_enabled():
        if not body.totp_code:
            # 密码正确但还没提供两步验证码：不算失败次数，前端据此弹出验证码输入框
            raise HTTPException(status_code=401, detail={"code": "totp_required", "message": "请输入两步验证码"})
        if not auth_mgr.verify_totp(body.totp_code):
            auth_mgr.register_failure(ip)
            remain = auth_mgr.is_locked(ip)
            _record_audit(request, "login_failed_totp", f"ip={ip}")
            if remain > 0:
                raise HTTPException(status_code=429, detail=f"验证码错误次数过多，已锁定 {int(remain)} 秒后再试")
            raise HTTPException(status_code=401, detail={"code": "totp_required", "message": "验证码错误"})

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
    _record_audit(request, "login_success", f"ip={ip}")
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


@app.get("/api/totp/status")
def totp_status(auth=Depends(require_auth)):
    return {"enabled": auth_mgr.is_totp_enabled()}


@app.post("/api/totp/setup")
def totp_setup(auth=Depends(require_auth)):
    """生成一个新的密钥(暂不启用)，返回 otpauth:// URI 供 Google Authenticator / 其它 TOTP App 手动添加。
    没有内置二维码生成库，只能提供文本 URI/密钥，用户需要在 App 里手动输入或用其它二维码工具自行转换。"""
    secret = auth_mgr.generate_totp_secret()
    otpauth_url = f"otpauth://totp/OllamaScanner?secret={secret}&issuer=OllamaScanner"
    return {"secret": secret, "otpauth_url": otpauth_url}


class TotpEnableIn(BaseModel):
    code: str


@app.post("/api/totp/enable")
def totp_enable(body: TotpEnableIn, request: Request, auth=Depends(require_auth)):
    if not auth_mgr.enable_totp(body.code):
        raise HTTPException(status_code=400, detail="验证码不正确，请重新扫描/输入密钥后重试")
    _record_audit(request, "totp_enabled", "")
    return {"status": "ok"}


class TotpDisableIn(BaseModel):
    password: str


@app.post("/api/totp/disable")
def totp_disable(body: TotpDisableIn, request: Request, auth=Depends(require_auth)):
    if not auth_mgr.verify_password(body.password):
        raise HTTPException(status_code=401, detail="密码不正确")
    auth_mgr.disable_totp()
    _record_audit(request, "totp_disabled", "")
    return {"status": "ok"}


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
    主机记录格式: {"url": str, "enabled": bool, "favorite": bool, "tags": [str], "group": str}
    自动兼容旧版本(纯字符串列表 / 无 tags 字段 / 无 group 字段)数据, 迁移为新格式。
    """
    raw = _load_json_file(HOSTS_FILE, [])
    migrated = False
    hosts = []
    for item in raw:
        if isinstance(item, str):
            hosts.append({"url": item, "enabled": True, "favorite": False, "tags": [], "group": ""})
            migrated = True
        else:
            if "tags" not in item or "group" not in item:
                migrated = True
            hosts.append({
                "url": item.get("url"),
                "enabled": item.get("enabled", True),
                "favorite": item.get("favorite", False),
                "tags": item.get("tags", []),
                "group": item.get("group", ""),
            })
    if migrated:
        save_hosts(hosts)
    return hosts


def save_hosts(hosts):
    ordered = sorted(hosts, key=lambda h: not h.get("favorite", False))
    _save_json_file(HOSTS_FILE, ordered)


class HostIn(BaseModel):
    url: str
    tags: list[str] = []
    group: str = ""
    force: bool = False  # 探活失败时，前端二次确认后带上此字段强制新增


class HostPatch(BaseModel):
    url: str
    enabled: bool | None = None
    favorite: bool | None = None
    tags: list[str] | None = None
    group: str | None = None


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


def _probe_host_reachable(url: str, timeout: float = 3.0) -> tuple[bool, str]:
    """新增主机前的轻量探活：请求 Ollama 的 /api/tags，判断地址填错还是真的连不通。"""
    try:
        resp = requests.get(f"{url}/api/tags", timeout=timeout)
        if resp.status_code == 200:
            return True, ""
        return False, f"HTTP {resp.status_code}"
    except requests.exceptions.RequestException as e:
        return False, str(e)[:200]


def _host_scan_status_map():
    results = _load_json_file(RESULTS_FILE, {})
    return results.get("host_status", {})


def _enrich_host_status(hosts):
    status_map = _host_scan_status_map()
    for h in hosts:
        h["last_scan_status"] = status_map.get(h["url"], "unknown")
    return hosts


@app.get("/api/hosts")
def get_hosts(auth=Depends(require_auth)):
    return _enrich_host_status(load_hosts())


@app.delete("/api/hosts/failed")
def delete_failed_hosts(request: Request, auth=Depends(require_auth)):
    """一键删除"失败区"里的主机(最近一次扫描 unreachable 或 all_down 的)，防止误连/误加回来。"""
    status_map = _host_scan_status_map()
    with _hosts_lock:
        hosts = load_hosts()
        to_remove = [h for h in hosts if status_map.get(h["url"]) in ("unreachable", "all_down")]
        if not to_remove:
            return {"removed": 0, "hosts": _enrich_host_status(hosts)}
        removed_urls = {h["url"] for h in to_remove}
        hosts = [h for h in hosts if h["url"] not in removed_urls]
        save_hosts(hosts)
        remaining = load_hosts()
    for url in removed_urls:
        _purge_host_from_leaderboard(url)
        _purge_host_from_ping_status(url)
    _record_audit(request, "delete_failed_hosts", f"{len(removed_urls)} 个: {', '.join(list(removed_urls)[:20])}")
    return {"removed": len(removed_urls), "hosts": _enrich_host_status(remaining)}


@app.post("/api/hosts")
def add_host(host: HostIn, request: Request, auth=Depends(require_auth)):
    url = normalize_url(host.url)
    if not host.force:
        ok, err = _probe_host_reachable(url)
        if not ok:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "unreachable",
                    "message": f"无法连接到该地址（{err}），确认地址无误的话可以强制添加",
                },
            )
    with _hosts_lock:
        hosts = load_hosts()
        if find_host(hosts, url):
            raise HTTPException(status_code=400, detail="该地址已存在")
        hosts.append({"url": url, "enabled": True, "favorite": False, "tags": host.tags or [], "group": (host.group or "").strip()[:50]})
        save_hosts(hosts)
        result = load_hosts()
    _record_audit(request, "add_host", url)
    return _enrich_host_status(result)


@app.patch("/api/hosts")
def patch_host(patch: HostPatch, request: Request, auth=Depends(require_auth)):
    url = normalize_url(patch.url)
    with _hosts_lock:
        hosts = load_hosts()
        h = find_host(hosts, url)
        if not h:
            raise HTTPException(status_code=404, detail="未找到该地址")
        if patch.enabled is not None:
            h["enabled"] = patch.enabled
        if patch.favorite is not None:
            h["favorite"] = patch.favorite
        if patch.tags is not None:
            h["tags"] = patch.tags
        if patch.group is not None:
            h["group"] = patch.group.strip()[:50]
        save_hosts(hosts)
        result = load_hosts()
    _record_audit(request, "patch_host", url)
    return _enrich_host_status(result)


@app.delete("/api/hosts")
def delete_host(host: HostIn, request: Request, auth=Depends(require_auth)):
    url = normalize_url(host.url)
    with _hosts_lock:
        hosts = load_hosts()
        h = find_host(hosts, url)
        if not h:
            raise HTTPException(status_code=404, detail="未找到该地址")
        hosts.remove(h)
        save_hosts(hosts)
        remaining = load_hosts()
    remaining = _enrich_host_status(remaining)
    # 主机被移除后，排行榜和 ping 连通性状态里属于它的旧记录不会再被更新，
    # 顺手清掉避免这些文件随时间无限堆积陈旧数据。
    _purge_host_from_leaderboard(url)
    _purge_host_from_ping_status(url)
    _record_audit(request, "delete_host", url)
    return remaining


# ---------------------------------------------------------------------------
# 扫描控制
# ---------------------------------------------------------------------------


class ScanStartIn(BaseModel):
    concurrency: int = 3
    model_concurrency: int = 4


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


def _purge_host_from_leaderboard(url):
    with _leaderboard_lock:
        lb = _load_json_file(LEADERBOARD_FILE, [])
        kept = [e for e in lb if e.get("host") != url]
        if len(kept) != len(lb):
            _save_json_file(LEADERBOARD_FILE, kept)


def _purge_host_from_ping_status(url):
    with _ping_lock:
        status = _load_json_file(PING_STATUS_FILE, {})
        prefix = f"{url}|"
        kept = {k: v for k, v in status.items() if not k.startswith(prefix)}
        if len(kept) != len(status):
            _save_json_file(PING_STATUS_FILE, kept)


def _record_audit(request: Request, action: str, detail: str = ""):
    """记录一条操作审计日志：谁（来源IP）在什么时候做了什么。
    单管理员场景下没有用户名概念，用来源 IP 作为操作者标识。"""
    entry = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "ip": get_client_ip(request),
        "action": action,
        "detail": detail,
    }
    with _audit_lock:
        logs = _load_json_file(AUDIT_LOG_FILE, [])
        logs.append(entry)
        if len(logs) > AUDIT_LOG_MAX_ENTRIES:
            logs = logs[-AUDIT_LOG_MAX_ENTRIES:]
        _save_json_file(AUDIT_LOG_FILE, logs)


class LanguageRuleIn(BaseModel):
    type: str
    n: int | None = None
    word: str | None = None
    count: int | None = None
    words: list[str] | None = None


class CustomLanguageTestIn(BaseModel):
    name: str
    prompt: str
    rules: list[LanguageRuleIn] = []


@app.get("/api/custom-tests")
def list_custom_tests(auth=Depends(require_auth)):
    return _load_settings().get("custom_language_tests", [])


@app.post("/api/custom-tests")
def create_custom_test(body: CustomLanguageTestIn, request: Request, auth=Depends(require_auth)):
    if not body.prompt.strip():
        raise HTTPException(status_code=400, detail="prompt 不能为空")
    with _settings_lock:
        raw = _load_json_file(SETTINGS_FILE, {})
        settings = _deep_merge_defaults(DEFAULT_SETTINGS, raw)
        entry = {
            "id": secrets.token_hex(6),
            "name": body.name.strip()[:100] or "未命名用例",
            "prompt": body.prompt.strip(),
            "rules": [r.model_dump() for r in body.rules],
        }
        settings["custom_language_tests"].append(entry)
        _save_json_file(SETTINGS_FILE, settings)
    _record_audit(request, "custom_test_create", entry["name"])
    return entry


@app.delete("/api/custom-tests/{test_id}")
def delete_custom_test(test_id: str, request: Request, auth=Depends(require_auth)):
    with _settings_lock:
        raw = _load_json_file(SETTINGS_FILE, {})
        settings = _deep_merge_defaults(DEFAULT_SETTINGS, raw)
        before = len(settings["custom_language_tests"])
        settings["custom_language_tests"] = [t for t in settings["custom_language_tests"] if t.get("id") != test_id]
        changed = len(settings["custom_language_tests"]) != before
        if changed:
            _save_json_file(SETTINGS_FILE, settings)
    if not changed:
        raise HTTPException(status_code=404, detail="未找到该测试用例")
    _record_audit(request, "custom_test_delete", test_id)
    return {"status": "ok"}


class CustomCoreTestIn(BaseModel):
    password: str        # 二次确认：因为这会在服务器沙箱子进程里真实执行任意代码
    name: str
    prompt: str
    harness: str
    expected: str = "ALL_PASS"


@app.get("/api/custom-tests/core")
def list_custom_core_tests(auth=Depends(require_auth)):
    # 不返回 harness 源码到列表接口，避免在前端到处明文出现；需要看内容可以在创建时留存的名字里体现
    return [{"id": t["id"], "name": t["name"], "prompt": t["prompt"]} for t in _load_settings().get("custom_core_tests", [])]


@app.post("/api/custom-tests/core")
def create_custom_core_test(body: CustomCoreTestIn, request: Request, auth=Depends(require_auth)):
    if not auth_mgr.verify_password(body.password):
        _record_audit(request, "custom_core_test_create_denied", "密码校验失败")
        raise HTTPException(status_code=401, detail="密码不正确")
    if not body.prompt.strip() or not body.harness.strip():
        raise HTTPException(status_code=400, detail="prompt 和 harness 不能为空")
    with _settings_lock:
        raw = _load_json_file(SETTINGS_FILE, {})
        settings = _deep_merge_defaults(DEFAULT_SETTINGS, raw)
        entry = {
            "id": secrets.token_hex(6),
            "name": body.name.strip()[:100] or "未命名用例",
            "prompt": body.prompt.strip(),
            "harness": body.harness,
            "expected": body.expected or "ALL_PASS",
        }
        settings["custom_core_tests"].append(entry)
        _save_json_file(SETTINGS_FILE, settings)
    # 强提醒：这条审计日志明确标注"会执行任意代码"，方便事后审查是谁在什么时候加了什么用例
    _record_audit(
        request,
        "custom_core_test_create",
        f"⚠️ 新增会在服务器沙箱执行任意代码的自定义核心测试用例：{entry['name']}",
    )
    return {"id": entry["id"], "name": entry["name"], "prompt": entry["prompt"]}


@app.delete("/api/custom-tests/core/{test_id}")
def delete_custom_core_test(test_id: str, request: Request, auth=Depends(require_auth)):
    with _settings_lock:
        raw = _load_json_file(SETTINGS_FILE, {})
        settings = _deep_merge_defaults(DEFAULT_SETTINGS, raw)
        before = len(settings["custom_core_tests"])
        settings["custom_core_tests"] = [t for t in settings["custom_core_tests"] if t.get("id") != test_id]
        changed = len(settings["custom_core_tests"]) != before
        if changed:
            _save_json_file(SETTINGS_FILE, settings)
    if not changed:
        raise HTTPException(status_code=404, detail="未找到该测试用例")
    _record_audit(request, "custom_core_test_delete", test_id)
    return {"status": "ok"}


@app.get("/api/audit-log")
def get_audit_log(auth=Depends(require_auth)):
    logs = _load_json_file(AUDIT_LOG_FILE, [])
    return list(reversed(logs))  # 最新的排前面


@app.get("/api/audit-log/export")
def export_audit_log(auth=Depends(require_auth)):
    import csv
    import io

    logs = list(reversed(_load_json_file(AUDIT_LOG_FILE, [])))
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["时间", "来源IP", "操作", "详情"])
    for entry in logs:
        writer.writerow([entry.get("ts", ""), entry.get("ip", ""), entry.get("action", ""), entry.get("detail", "")])
    content = "\ufeff" + buf.getvalue()
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return Response(
        content=content,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="audit_log_{ts}.csv"'},
    )


# ---------------------------------------------------------------------------
# 设置：定时扫描 + 异常通知渠道
# ---------------------------------------------------------------------------


def _deep_merge_defaults(defaults, loaded):
    """把已保存的设置和默认结构做深度合并，保证以后新增字段时旧的 settings.json 不会缺键报错。"""
    if not isinstance(loaded, dict):
        return json.loads(json.dumps(defaults))
    merged = {}
    for k, dv in defaults.items():
        lv = loaded.get(k, None)
        if isinstance(dv, dict):
            merged[k] = _deep_merge_defaults(dv, lv if isinstance(lv, dict) else {})
        else:
            merged[k] = lv if lv is not None else dv
    return merged


def _load_settings():
    with _settings_lock:
        raw = _load_json_file(SETTINGS_FILE, {})
        return _deep_merge_defaults(DEFAULT_SETTINGS, raw)


def _save_settings(settings):
    with _settings_lock:
        _save_json_file(SETTINGS_FILE, settings)


@app.get("/api/settings")
def get_settings(auth=Depends(require_auth)):
    return _load_settings()


class NotifyWecomIn(BaseModel):
    enabled: bool = False
    webhook_url: str = ""


class NotifyTelegramIn(BaseModel):
    enabled: bool = False
    bot_token: str = ""
    chat_id: str = ""


class NotifyBarkIn(BaseModel):
    enabled: bool = False
    key: str = ""
    server: str = "https://api.day.app"


class NotifyEmailIn(BaseModel):
    enabled: bool = False
    smtp_host: str = ""
    smtp_port: int = 587
    username: str = ""
    password: str = ""
    from_addr: str = ""
    to_addr: str = ""
    use_tls: bool = True


class NotifySettingsIn(BaseModel):
    wecom: NotifyWecomIn = NotifyWecomIn()
    telegram: NotifyTelegramIn = NotifyTelegramIn()
    bark: NotifyBarkIn = NotifyBarkIn()
    email: NotifyEmailIn = NotifyEmailIn()


class ScheduleSettingsIn(BaseModel):
    enabled: bool = False
    time: str = "09:00"
    concurrency: int = 3
    model_concurrency: int = 4


class HistorySettingsIn(BaseModel):
    retention_days: int = 180
    max_size_mb: int = 50
    auto_cleanup_enabled: bool = True


class SettingsIn(BaseModel):
    schedule: ScheduleSettingsIn = ScheduleSettingsIn()
    notify: NotifySettingsIn = NotifySettingsIn()
    history: HistorySettingsIn = HistorySettingsIn()


@app.put("/api/settings")
def put_settings(body: SettingsIn, request: Request, auth=Depends(require_auth)):
    import re
    if not re.match(r"^([01]\d|2[0-3]):[0-5]\d$", body.schedule.time or ""):
        raise HTTPException(status_code=400, detail="定时时间格式应为 HH:MM，例如 09:00")
    if body.history.retention_days <= 0:
        raise HTTPException(status_code=400, detail="retention_days 必须是正整数")
    if body.history.max_size_mb <= 0:
        raise HTTPException(status_code=400, detail="max_size_mb 必须是正整数")
    settings = body.model_dump()
    # "share"(分享链接)和 "metrics"(Prometheus 抓取)都不是这个表单管理的字段，
    # 通过各自独立的接口维护，这里如果整体覆盖写回会把已生成的 token 冲掉，所以从旧设置里保留下来。
    existing = _load_settings()
    settings["share"] = existing.get("share", DEFAULT_SETTINGS["share"])
    settings["metrics"] = existing.get("metrics", DEFAULT_SETTINGS["metrics"])
    settings["custom_language_tests"] = existing.get("custom_language_tests", DEFAULT_SETTINGS["custom_language_tests"])
    settings["custom_core_tests"] = existing.get("custom_core_tests", DEFAULT_SETTINGS["custom_core_tests"])
    _save_settings(settings)
    _record_audit(request, "update_settings", "已更新定时扫描/通知/历史数据设置")
    return settings


@app.post("/api/notify/test")
def notify_test(request: Request, auth=Depends(require_auth)):
    settings = _load_settings()
    enabled_channels = [k for k, v in settings.get("notify", {}).items() if v.get("enabled")]
    if not enabled_channels:
        raise HTTPException(status_code=400, detail="还没有启用任何通知渠道，先勾选并保存设置")
    _send_notifications("✅ Ollama 扫描台测试通知", "如果收到这条消息，说明该通知渠道配置正确。")
    _record_audit(request, "notify_test", f"渠道: {', '.join(enabled_channels)}")
    return {"status": "sent", "channels": enabled_channels}


# ---------------------------------------------------------------------------
# 异常通知：发现主机/模型从「正常」变为「不可用」时，推送到配置好的渠道
# ---------------------------------------------------------------------------


def _notify_wecom(cfg, title, message):
    if not cfg.get("webhook_url"):
        return
    requests.post(cfg["webhook_url"], json={"msgtype": "text", "text": {"content": f"{title}\n{message}"}}, timeout=8)


def _notify_telegram(cfg, title, message):
    if not cfg.get("bot_token") or not cfg.get("chat_id"):
        return
    url = f"https://api.telegram.org/bot{cfg['bot_token']}/sendMessage"
    requests.post(url, json={"chat_id": cfg["chat_id"], "text": f"{title}\n{message}"}, timeout=8)


def _notify_bark(cfg, title, message):
    if not cfg.get("key"):
        return
    server = (cfg.get("server") or "https://api.day.app").rstrip("/")
    url = f"{server}/{cfg['key']}"
    requests.post(url, json={"title": title, "body": message, "group": "ollama-scanner"}, timeout=8)


def _notify_email(cfg, title, message):
    if not cfg.get("smtp_host") or not cfg.get("to_addr"):
        return
    import smtplib
    from email.mime.text import MIMEText

    msg = MIMEText(message, "plain", "utf-8")
    msg["Subject"] = title
    msg["From"] = cfg.get("from_addr") or cfg.get("username") or ""
    msg["To"] = cfg["to_addr"]

    with smtplib.SMTP(cfg["smtp_host"], int(cfg.get("smtp_port", 587)), timeout=10) as smtp:
        if cfg.get("use_tls", True):
            smtp.starttls()
        if cfg.get("username"):
            smtp.login(cfg["username"], cfg.get("password", ""))
        smtp.sendmail(msg["From"], [cfg["to_addr"]], msg.as_string())


_NOTIFY_SENDERS = {
    "wecom": _notify_wecom,
    "telegram": _notify_telegram,
    "bark": _notify_bark,
    "email": _notify_email,
}


def _send_notifications(title, message):
    settings = _load_settings()
    notify = settings.get("notify", {})
    for channel, sender in _NOTIFY_SENDERS.items():
        cfg = notify.get(channel, {})
        if not cfg.get("enabled"):
            continue
        try:
            sender(cfg, title, message)
        except Exception as e:
            # 单个渠道发送失败不应该影响其它渠道，记一笔审计方便排查（无 request 上下文，直接写日志文件）
            with _audit_lock:
                logs = _load_json_file(AUDIT_LOG_FILE, [])
                logs.append({
                    "ts": datetime.now().isoformat(timespec="seconds"),
                    "ip": "system",
                    "action": "notify_failed",
                    "detail": f"{channel}: {str(e)[:200]}",
                })
                if len(logs) > AUDIT_LOG_MAX_ENTRIES:
                    logs = logs[-AUDIT_LOG_MAX_ENTRIES:]
                _save_json_file(AUDIT_LOG_FILE, logs)


def _detect_regressions(prev_results, new_results):
    """对比本次和上一次扫描结果，找出「之前正常、这次变差」的主机/模型。返回通知正文，没有异常则返回 None。"""
    prev_viability = (prev_results or {}).get("viability", {}) or {}
    new_viability = (new_results or {}).get("viability", {}) or {}
    prev_hosts = set((prev_results or {}).get("discovered", {}).keys())
    new_hosts = set((new_results or {}).get("discovered", {}).keys())

    newly_unreachable_hosts = sorted(prev_hosts - new_hosts)
    newly_failed_models = sorted(
        key for key, ok in prev_viability.items()
        if ok and new_viability.get(key) is False
    )

    if not newly_unreachable_hosts and not newly_failed_models:
        return None

    lines = []
    if newly_unreachable_hosts:
        lines.append(f"主机完全不可达（{len(newly_unreachable_hosts)} 个）：")
        lines.extend(f"  · {h}" for h in newly_unreachable_hosts[:20])
    if newly_failed_models:
        lines.append(f"模型由正常变为不可用（{len(newly_failed_models)} 个）：")
        for key in newly_failed_models[:20]:
            host, model = key.split("|", 1)
            lines.append(f"  · {host} @ {model}")
    return "\n".join(lines)


def _launch_scan_and_watch(hosts, concurrency, model_concurrency):
    """启动一次扫描，并在后台等它跑完后做异常对比 + 发通知。手动触发和定时触发共用这个入口。"""
    prev_results = _load_json_file(RESULTS_FILE, {})
    refresh_custom_language_cases(_load_settings().get("custom_language_tests", []))
    refresh_custom_core_cases(_load_settings().get("custom_core_tests", []))
    ok = start_scan_thread(hosts, state, concurrency=concurrency, model_concurrency=model_concurrency)
    if not ok:
        return False

    def _watch():
        while state.running:
            time.sleep(2)
        new_results = state.results
        if new_results is None:
            return
        msg = _detect_regressions(prev_results, new_results)
        if msg:
            _send_notifications("⚠️ Ollama 集群扫描发现新异常", msg)

    threading.Thread(target=_watch, daemon=True).start()
    return True


# ---------------------------------------------------------------------------
# 定时扫描：每天固定时间自动跑一次全量扫描（时间可在设置里改，无需重启容器）
# ---------------------------------------------------------------------------

_last_scheduled_run_date = None
_last_history_snapshot_generated_at = None


def _scheduler_loop():
    global _last_scheduled_run_date
    _cycles = 0
    while True:
        time.sleep(20)
        _cycles += 1
        if _cycles % 180 == 0:  # 大约每 1 小时清理一次历史数据，不依赖是否刚好有扫描完成
            try:
                _enforce_history_limits()
            except Exception:
                pass
        try:
            settings = _load_settings()
            sched = settings.get("schedule", {})
            if not sched.get("enabled"):
                continue
            now = datetime.now()
            today = now.strftime("%Y-%m-%d")
            if now.strftime("%H:%M") != sched.get("time", "09:00"):
                continue
            if _last_scheduled_run_date == today:
                continue  # 这一天已经跑过了，避免同一分钟内被反复触发
            if state.running:
                continue  # 有正在进行的扫描（可能是手动触发的），跳过这次，等下一天
            hosts = [h["url"] for h in load_hosts() if h.get("enabled", True)]
            if not hosts:
                continue
            _last_scheduled_run_date = today
            _launch_scan_and_watch(
                hosts,
                max(1, min(100, sched.get("concurrency", 3))),
                max(1, min(20, sched.get("model_concurrency", 4))),
            )
        except Exception:
            pass  # 调度循环本身绝不能因为单次异常而退出


@app.on_event("startup")
def _start_background_threads():
    threading.Thread(target=_scheduler_loop, daemon=True).start()


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
    with _leaderboard_lock:
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
    ok = _launch_scan_and_watch(hosts, concurrency, model_concurrency)
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
    global _last_history_snapshot_generated_at
    if results is not None:
        with _results_lock:
            _save_json_file(RESULTS_FILE, results)
        update_leaderboard_from_results(results)
        generated_at = results.get("generated_at")
        if generated_at and generated_at != _last_history_snapshot_generated_at:
            _append_history_snapshot(_build_leaderboard_view())
            _last_history_snapshot_generated_at = generated_at
            for key, ok in (results.get("viability") or {}).items():
                if "|" in key:
                    h, m = key.split("|", 1)
                    _record_uptime_sample(h, m, ok)
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


def _build_leaderboard_view():
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


@app.get("/api/leaderboard")
def get_leaderboard(auth=Depends(require_auth)):
    return _build_leaderboard_view()


def _build_quick_leaderboard_view():
    """"快速测试"视图：不评判回答质量，只看"这个模型现在能不能正常聊天、多快回应"。
    直接用最新一次扫描的可用性(viability)+响应耗时，在线的排前面(按耗时升序)，离线的放到 failed 里。"""
    results = _load_json_file(RESULTS_FILE, {})
    discovered = results.get("discovered", {})
    viability = results.get("viability", {})
    timing = results.get("viability_timing", {})
    generated_at = results.get("generated_at")

    ranked, failed = [], []
    for host, models in discovered.items():
        for m in models:
            key = f"{host}|{m}"
            ok = viability.get(key)
            row = {
                "host": host,
                "model": m,
                "family_hint": guess_model_family(m),
                "ok": ok,
                "elapsed": timing.get(key),
                "last_tested": generated_at,
            }
            (ranked if ok else failed).append(row)

    ranked.sort(key=lambda r: r["elapsed"] if r["elapsed"] is not None else float("inf"))
    for i, r in enumerate(ranked):
        r["rank"] = i + 1
    return {"label": "快速测试（在线检测）", "ranked": ranked, "failed": failed, "generated_at": generated_at}


@app.get("/api/leaderboard/quick")
def get_quick_leaderboard(auth=Depends(require_auth)):
    return _build_quick_leaderboard_view()


@app.get("/api/leaderboard/export")
def export_leaderboard(fmt: str = "csv", auth=Depends(require_auth)):
    """把排行榜导出成 CSV 或 Markdown，方便甩给别人看而不用截图。"""
    if fmt not in ("csv", "md"):
        raise HTTPException(status_code=400, detail="fmt 只支持 csv 或 md")

    view = _build_leaderboard_view()
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    if fmt == "csv":
        import csv
        import io

        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["分类", "排名", "主机", "模型", "状态", "通过/总数", "总耗时(s)", "平均耗时(s)", "最近测试时间", "错误信息"])
        for cat, data in view.items():
            for r in data["ranked"]:
                writer.writerow([data["label"], r["rank"], r["host"], r["model"], r["status"],
                                  f"{r['passed']}/{r['total']}", r["elapsed_total"], r["elapsed_avg"],
                                  r["last_tested"], ""])
            for r in data["failed"]:
                writer.writerow([data["label"], "", r["host"], r["model"], r["status"],
                                  f"{r['passed']}/{r['total']}", "", "", r["last_tested"], r.get("error") or ""])
        content = "\ufeff" + buf.getvalue()  # 加 BOM，避免 Excel 打开中文乱码
        media_type = "text/csv"
        filename = f"leaderboard_{ts}.csv"
    else:
        lines = [f"# 模型排行榜导出（{ts}）", ""]
        for cat, data in view.items():
            lines.append(f"## {data['label']}")
            lines.append("")
            if data["ranked"]:
                lines.append("| 排名 | 主机 | 模型 | 通过/总数 | 总耗时(s) | 平均耗时(s) | 最近测试 |")
                lines.append("|---|---|---|---|---|---|---|")
                for r in data["ranked"]:
                    lines.append(
                        f"| {r['rank']} | {r['host']} | {r['model']} | {r['passed']}/{r['total']} "
                        f"| {r['elapsed_total']} | {r['elapsed_avg']} | {r['last_tested'] or ''} |"
                    )
                lines.append("")
            if data["failed"]:
                lines.append("失败/不可用：")
                for r in data["failed"]:
                    lines.append(f"- {r['host']} · {r['model']}：{r.get('error') or r['status']}")
                lines.append("")
        content = "\n".join(lines)
        media_type = "text/markdown"
        filename = f"leaderboard_{ts}.md"

    return Response(
        content=content,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ---------------------------------------------------------------------------
# 历史趋势数据：每次扫描完成后追加一条时间序列快照(JSONL，一行一条)，用于前端画趋势图。
# 所有读写都只针对硬编码的 HISTORY_FILE 这一个路径，不接受任何来自请求的文件名/路径参数，
# 防止被用来读取这个目录之外的其它文件(比如系统密码文件)或篡改成任意文件写入。
# ---------------------------------------------------------------------------


def _append_history_snapshot(view):
    """view: _build_leaderboard_view() 的返回值。summary 是每个分类的汇总数字；
    models 是排名前100的 主机+模型 各自的总耗时明细(数量截断，避免文件随主机/模型数量无限膨胀)，
    用于前端按"某个具体模型"画耗时趋势线，而不只是看整体汇总。"""
    summary = {}
    models_detail = {}
    for cat, data in view.items():
        ranked = data.get("ranked", [])
        failed = data.get("failed", [])
        elapsed_list = [r["elapsed_total"] for r in ranked if r.get("elapsed_total") is not None]
        summary[cat] = {
            "ranked_count": len(ranked),
            "failed_count": len(failed),
            "best_elapsed": min(elapsed_list) if elapsed_list else None,
            "avg_elapsed": round(sum(elapsed_list) / len(elapsed_list), 2) if elapsed_list else None,
        }
        models_detail[cat] = [
            {"host": r["host"], "model": r["model"], "elapsed_total": r["elapsed_total"]}
            for r in ranked[:100]
        ]
    record = {"ts": datetime.now().isoformat(timespec="seconds"), "categories": summary, "models": models_detail}
    with _history_lock:
        try:
            with open(HISTORY_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception:
            pass
    _enforce_history_limits()


def _read_history_lines():
    if not HISTORY_FILE.exists():
        return []
    lines = []
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    lines.append(json.loads(line))
                except Exception:
                    continue  # 跳过损坏的单行，不让整个历史读取失败
    except Exception:
        return []
    return lines


def _rewrite_history(records):
    with _history_lock:
        tmp = HISTORY_FILE.with_name(f".{HISTORY_FILE.name}.tmp.{os.getpid()}.{threading.get_ident()}")
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                for r in records:
                    f.write(json.dumps(r, ensure_ascii=False) + "\n")
            os.replace(tmp, HISTORY_FILE)
        except Exception:
            pass


def _history_file_size() -> int:
    try:
        return HISTORY_FILE.stat().st_size
    except Exception:
        return 0


def _enforce_history_limits():
    """按当前设置做定期清理：超过保留天数的记录删掉；文件超过大小上限则从最旧的开始删，
    直到降到限制以下，防止无限增长把服务器磁盘占满、拖垮宿主机。"""
    settings = _load_settings()
    hist_cfg = settings.get("history", {})
    if not hist_cfg.get("auto_cleanup_enabled", True):
        return

    retention_days = max(1, int(hist_cfg.get("retention_days", 180)))
    max_size_bytes = min(
        HISTORY_MAX_SIZE_BYTES_HARD_CAP,
        max(1, int(hist_cfg.get("max_size_mb", 50))) * 1024 * 1024,
    )

    records = _read_history_lines()
    if not records:
        return

    cutoff = datetime.now() - timedelta(days=retention_days)
    kept = []
    for r in records:
        try:
            ts = datetime.fromisoformat(r.get("ts", ""))
        except Exception:
            continue  # 时间戳解析不了的脏记录直接丢弃
        if ts >= cutoff:
            kept.append(r)
    changed = len(kept) != len(records)

    # 按天数过滤后仍然超过大小上限的话，从最旧的开始继续丢，直到低于上限
    if changed:
        _rewrite_history(kept)
    if _history_file_size() > max_size_bytes:
        while kept and _history_file_size() > max_size_bytes:
            kept = kept[1:]
            _rewrite_history(kept)


class HistoryDeleteIn(BaseModel):
    mode: str  # "days" | "all"
    days: int | None = None  # mode=="days" 时必填，比如 30/60/180/365


@app.get("/api/history/stats")
def history_stats(auth=Depends(require_auth)):
    records = _read_history_lines()
    return {
        "count": len(records),
        "size_bytes": _history_file_size(),
        "oldest_ts": records[0]["ts"] if records else None,
        "newest_ts": records[-1]["ts"] if records else None,
    }


@app.get("/api/history")
def get_history(days: int = 90, auth=Depends(require_auth)):
    """给趋势图用：默认只返回最近 90 天，避免一次性把全部历史都传给前端。"""
    records = _read_history_lines()
    if days > 0:
        cutoff = datetime.now() - timedelta(days=days)
        out = []
        for r in records:
            try:
                if datetime.fromisoformat(r.get("ts", "")) >= cutoff:
                    out.append(r)
            except Exception:
                continue
        return out
    return records


@app.get("/api/history/model")
def get_history_for_model(host: str, model: str, category: str = "core", days: int = 90, auth=Depends(require_auth)):
    """给"按模型看趋势"用：从历史快照里抽取指定 主机+模型+分类 的耗时序列。"""
    host = normalize_url(host)
    records = _read_history_lines()
    if days > 0:
        cutoff = datetime.now() - timedelta(days=days)
        records = [r for r in records if _ts_after(r.get("ts"), cutoff)]
    out = []
    for r in records:
        for entry in (r.get("models", {}) or {}).get(category, []):
            if entry.get("host") == host and entry.get("model") == model:
                out.append({"ts": r["ts"], "elapsed_total": entry.get("elapsed_total")})
                break
    return out


def _ts_after(ts, cutoff) -> bool:
    try:
        return datetime.fromisoformat(ts or "") >= cutoff
    except Exception:
        return False


@app.delete("/api/history")
def delete_history(body: HistoryDeleteIn, request: Request, auth=Depends(require_auth)):
    records = _read_history_lines()
    if body.mode == "all":
        _rewrite_history([])
        _record_audit(request, "history_delete", "all")
        return {"status": "ok", "remaining": 0}
    if body.mode == "days":
        if not body.days or body.days <= 0:
            raise HTTPException(status_code=400, detail="days 必须是正整数")
        cutoff = datetime.now() - timedelta(days=body.days)
        kept = []
        for r in records:
            try:
                ts = datetime.fromisoformat(r.get("ts", ""))
            except Exception:
                continue
            if ts >= cutoff:
                kept.append(r)
        _rewrite_history(kept)
        _record_audit(request, "history_delete", f"older_than_{body.days}_days")
        return {"status": "ok", "remaining": len(kept)}
    raise HTTPException(status_code=400, detail="mode 只支持 days 或 all")


# ---------------------------------------------------------------------------
# 只读分享链接：把排行榜以只读形式分享出去，不需要登录，但看不到主机地址/管理功能。
# 用独立的随机 token 做校验(不是登录 session)，token 在设置里生成/重置，enabled=false 时整个接口关闭。
# ---------------------------------------------------------------------------


def _build_public_leaderboard_view():
    """基于 _build_leaderboard_view() 做脱敏：主机地址替换成"主机N"匿名标签(同一次请求内地址->
    编号保持一致，方便看出"同一台机器的不同模型"，但看不到真实地址)，且不返回任何管理相关字段。"""
    view = _build_leaderboard_view()
    host_alias = {}

    def alias_for(host):
        if host not in host_alias:
            host_alias[host] = f"主机{len(host_alias) + 1}"
        return host_alias[host]

    public = {}
    for cat, data in view.items():
        ranked = [
            {
                "host": alias_for(r["host"]),
                "model": r["model"],
                "family_hint": r["family_hint"],
                "rank": r["rank"],
                "passed": r["passed"],
                "total": r["total"],
                "elapsed_total": r["elapsed_total"],
                "elapsed_avg": r["elapsed_avg"],
                "last_tested": r["last_tested"],
            }
            for r in data["ranked"]
        ]
        failed = [
            {
                "host": alias_for(r["host"]),
                "model": r["model"],
                "family_hint": r["family_hint"],
                "status": r["status"],
                "last_tested": r["last_tested"],
            }
            for r in data["failed"]
        ]
        public[cat] = {"label": data["label"], "ranked": ranked, "failed": failed}
    return public


@app.get("/api/share/settings")
def get_share_settings(auth=Depends(require_auth)):
    share = _load_settings().get("share", {})
    tokens = [t for t in share.get("tokens", []) if not _token_expired(t)]
    return {"enabled": share.get("enabled", False), "tokens": tokens}


def _token_expired(t: dict) -> bool:
    exp = t.get("expires_at")
    if not exp:
        return False
    try:
        return datetime.fromisoformat(exp) < datetime.now()
    except Exception:
        return False


class ShareSettingsIn(BaseModel):
    enabled: bool


@app.put("/api/share/settings")
def put_share_settings(body: ShareSettingsIn, request: Request, auth=Depends(require_auth)):
    with _settings_lock:
        raw = _load_json_file(SETTINGS_FILE, {})
        settings = _deep_merge_defaults(DEFAULT_SETTINGS, raw)
        settings["share"]["enabled"] = body.enabled
        _save_json_file(SETTINGS_FILE, settings)
    _record_audit(request, "share_settings_update", f"enabled={body.enabled}")
    return {"enabled": settings["share"]["enabled"]}


class ShareTokenCreateIn(BaseModel):
    label: str = ""
    expires_days: int | None = None  # 不填/None 表示永不过期


@app.post("/api/share/tokens")
def create_share_token(body: ShareTokenCreateIn, request: Request, auth=Depends(require_auth)):
    with _settings_lock:
        raw = _load_json_file(SETTINGS_FILE, {})
        settings = _deep_merge_defaults(DEFAULT_SETTINGS, raw)
        expires_at = None
        if body.expires_days and body.expires_days > 0:
            expires_at = (datetime.now() + timedelta(days=body.expires_days)).isoformat(timespec="seconds")
        entry = {
            "token": secrets.token_urlsafe(24),
            "label": (body.label or "").strip()[:100],
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "expires_at": expires_at,
        }
        settings["share"]["tokens"].append(entry)
        _save_json_file(SETTINGS_FILE, settings)
    _record_audit(request, "share_token_create", entry["label"] or "(未命名)")
    return entry


@app.delete("/api/share/tokens/{token}")
def revoke_share_token(token: str, request: Request, auth=Depends(require_auth)):
    with _settings_lock:
        raw = _load_json_file(SETTINGS_FILE, {})
        settings = _deep_merge_defaults(DEFAULT_SETTINGS, raw)
        before = len(settings["share"]["tokens"])
        settings["share"]["tokens"] = [t for t in settings["share"]["tokens"] if t["token"] != token]
        changed = len(settings["share"]["tokens"]) != before
        if changed:
            _save_json_file(SETTINGS_FILE, settings)
    if not changed:
        raise HTTPException(status_code=404, detail="未找到该链接")
    _record_audit(request, "share_token_revoke", "")
    return {"status": "ok"}


@app.get("/api/public/leaderboard/{token}")
def public_leaderboard(token: str):
    """无需登录的只读入口。只暴露排行榜脱敏视图，不暴露主机地址、审计日志、设置等任何管理功能。"""
    share = _load_settings().get("share", {})
    if not share.get("enabled"):
        raise HTTPException(status_code=404, detail="分享链接未开启")
    matched = None
    for t in share.get("tokens", []):
        if secrets.compare_digest(token, t.get("token", "")):
            matched = t
            break
    if not matched or _token_expired(matched):
        raise HTTPException(status_code=404, detail="分享链接无效或已过期")
    return _build_public_leaderboard_view()


# ---------------------------------------------------------------------------
# Prometheus 抓取端点：无需登录 session，但需要单独配置的 token（URL 参数或 Bearer 头），
# enabled=false 时整个端点关闭。只暴露聚合数字，不暴露主机地址等敏感信息。
# ---------------------------------------------------------------------------


def _check_metrics_token(request: Request, token: str | None):
    cfg = _load_settings().get("metrics", {})
    if not cfg.get("enabled") or not cfg.get("token"):
        raise HTTPException(status_code=404, detail="metrics 未启用")
    provided = token
    if not provided:
        authz = request.headers.get("authorization", "")
        if authz.lower().startswith("bearer "):
            provided = authz[7:]
    if not provided or not secrets.compare_digest(provided, cfg.get("token", "")):
        raise HTTPException(status_code=401, detail="metrics token 无效")


@app.get("/api/metrics/settings")
def get_metrics_settings(auth=Depends(require_auth)):
    cfg = _load_settings().get("metrics", {})
    return {"enabled": cfg.get("enabled", False), "has_token": bool(cfg.get("token"))}


class MetricsSettingsIn(BaseModel):
    enabled: bool


@app.put("/api/metrics/settings")
def put_metrics_settings(body: MetricsSettingsIn, request: Request, auth=Depends(require_auth)):
    with _settings_lock:
        raw = _load_json_file(SETTINGS_FILE, {})
        settings = _deep_merge_defaults(DEFAULT_SETTINGS, raw)
        settings["metrics"]["enabled"] = body.enabled
        if body.enabled and not settings["metrics"].get("token"):
            settings["metrics"]["token"] = secrets.token_urlsafe(24)
        _save_json_file(SETTINGS_FILE, settings)
    _record_audit(request, "metrics_settings_update", f"enabled={body.enabled}")
    return {"enabled": settings["metrics"]["enabled"], "token": settings["metrics"]["token"] if body.enabled else None}


@app.post("/api/metrics/regenerate")
def regenerate_metrics_token(request: Request, auth=Depends(require_auth)):
    with _settings_lock:
        raw = _load_json_file(SETTINGS_FILE, {})
        settings = _deep_merge_defaults(DEFAULT_SETTINGS, raw)
        settings["metrics"]["token"] = secrets.token_urlsafe(24)
        _save_json_file(SETTINGS_FILE, settings)
    _record_audit(request, "metrics_token_regenerate", "")
    return {"enabled": settings["metrics"]["enabled"], "token": settings["metrics"]["token"]}


@app.get("/api/metrics")
def prometheus_metrics(request: Request, token: str | None = None):
    _check_metrics_token(request, token)
    hosts = load_hosts()
    view = _build_leaderboard_view()
    hist_records = _read_history_lines()

    lines = []

    def gauge(name, help_text, value, labels=""):
        lines.append(f"# HELP {name} {help_text}")
        lines.append(f"# TYPE {name} gauge")
        lines.append(f"{name}{labels} {value}")

    gauge("ollama_scanner_hosts_total", "已添加的主机数量", len(hosts))
    gauge("ollama_scanner_hosts_enabled", "已启用的主机数量", sum(1 for h in hosts if h.get("enabled", True)))
    gauge("ollama_scanner_scan_running", "当前是否有扫描正在进行(1/0)", 1 if state.running else 0)
    gauge("ollama_scanner_history_records_total", "历史趋势记录条数", len(hist_records))
    gauge("ollama_scanner_history_size_bytes", "历史趋势文件大小(字节)", _history_file_size())

    for cat, data in view.items():
        gauge("ollama_scanner_leaderboard_ranked", "该分类下全部通过并参与排名的 主机+模型 数量",
              len(data["ranked"]), labels=f'{{category="{cat}"}}')
        gauge("ollama_scanner_leaderboard_failed", "该分类下存在失败项的 主机+模型 数量",
              len(data["failed"]), labels=f'{{category="{cat}"}}')

    return Response(content="\n".join(lines) + "\n", media_type="text/plain; version=0.0.4")


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

    viable, verr, _elapsed = quick_test(host, model)
    if not viable:
        with _leaderboard_lock:
            lb = _load_json_file(LEADERBOARD_FILE, [])
            lb_map = {f"{e['host']}|{e['model']}": e for e in lb}
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
    refresh_custom_language_cases(_load_settings().get("custom_language_tests", []))
    refresh_custom_core_cases(_load_settings().get("custom_core_tests", []))
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
    with _leaderboard_lock:
        lb = _load_json_file(LEADERBOARD_FILE, [])
        lb_map = {f"{e['host']}|{e['model']}": e for e in lb}
        entry = _merge_leaderboard_entry(lb_map, host, model, tests)
        _save_json_file(LEADERBOARD_FILE, list(lb_map.values()))
    return entry


# ---------------------------------------------------------------------------
# 侧边栏：每个主机 / 每个模型的连通性状态（绿色正常 / 红色失败），支持一键 Ping 重测
# ---------------------------------------------------------------------------


UPTIME_HISTORY_CAP = 100  # 每个 主机+模型 最多保留这么多条最近的在线/离线采样，避免文件无限增长


def _load_ping_status():
    return _load_json_file(PING_STATUS_FILE, {})


def _save_ping_status(host, model, result):
    with _ping_lock:
        status = _load_ping_status()
        key = f"{host}|{model}"
        entry = status.get(key, {})
        history = entry.get("history", [])
        history.append({"ok": bool(result.get("ok")), "ts": result.get("ts") or datetime.now().isoformat(timespec="seconds")})
        if len(history) > UPTIME_HISTORY_CAP:
            history = history[-UPTIME_HISTORY_CAP:]
        entry.update(result)
        entry["history"] = history
        status[key] = entry
        _save_json_file(PING_STATUS_FILE, status)


def _record_uptime_sample(host, model, ok):
    """扫描过程中的可用性结果也计入在线率采样，不影响"最近一次手动 Ping"这个字段本身。"""
    with _ping_lock:
        status = _load_ping_status()
        key = f"{host}|{model}"
        entry = status.setdefault(key, {})
        history = entry.get("history", [])
        history.append({"ok": bool(ok), "ts": datetime.now().isoformat(timespec="seconds")})
        if len(history) > UPTIME_HISTORY_CAP:
            history = history[-UPTIME_HISTORY_CAP:]
        entry["history"] = history
        _save_json_file(PING_STATUS_FILE, status)


def _uptime_pct(entry) -> float | None:
    history = (entry or {}).get("history") or []
    if not history:
        return None
    return round(100 * sum(1 for h in history if h.get("ok")) / len(history), 1)


@app.get("/api/hosts/status")
def hosts_status(auth=Depends(require_auth)):
    """给左侧栏用：每个主机 -> 已知模型列表 及 各自的连通性状态 + 最近样本的在线率。
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
            if ping and ping.get("ts"):
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
                "uptime_pct": _uptime_pct(ping),
            })
        model_list.sort(key=lambda m: (m["ok"] is not True, m["model"]))  # 在线的排前面
        out.append({
            "url": url,
            "enabled": h.get("enabled", True),
            "favorite": h.get("favorite", False),
            "last_scan_status": results.get("host_status", {}).get(url, "unknown"),
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

class NoCacheStaticFiles(StaticFiles):
    """静态文件不做浏览器强缓存，每次都向服务器确认版本，
    避免重新部署后手机/浏览器还在用旧的 app.js / style.css。"""

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
        return response


static_dir = Path(__file__).parent.parent / "static"
app.mount("/", NoCacheStaticFiles(directory=str(static_dir), html=True), name="static")
