"""
Ollama 集群扫描核心逻辑
改造自原始脚本: 支持动态主机列表 / 可中途停止 / 日志实时推送
"""
import requests
import re
import subprocess
import tempfile
import time
import json
import threading
import platform
from collections import defaultdict
from datetime import datetime

try:
    import resource  # 仅 Unix 可用，用于限制生成代码的资源消耗
except ImportError:
    resource = None

TIMEOUT = 120
QUICK_TEST_TIMEOUT = 20
CODE_RUN_TIMEOUT = 15

# 生成代码沙箱资源上限：防止模型生成的代码（例如被投毒/失控的模型）
# 消耗宿主机 CPU / 内存，或 fork 出大量进程（挖矿类滥用的典型特征）。
CODE_CPU_SECONDS = 10
CODE_MAX_MEMORY_BYTES = 512 * 1024 * 1024   # 512MB
# 注意: RLIMIT_NPROC 是按运行用户(uid)在全系统范围内计数的, 不是单进程独享,
# 高并发扫描时主程序自身会有较多线程, 这里给足余量避免误伤正常扫描, 但仍能挡住
# fork 炸弹式的挖矿/滥用代码 (试图瞬间拉起成百上千个子进程)。
CODE_MAX_PROCS = 512
CODE_MAX_FILE_BYTES = 10 * 1024 * 1024      # 10MB


def _limit_code_resources():
    """在子进程 exec 前调用（POSIX only），压低资源上限做纵深防御。
    注意：这不能替代真正的容器/命名空间隔离，只是尽量降低失控代码的破坏半径。"""
    if resource is None:
        return
    try:
        resource.setrlimit(resource.RLIMIT_CPU, (CODE_CPU_SECONDS, CODE_CPU_SECONDS))
        resource.setrlimit(resource.RLIMIT_AS, (CODE_MAX_MEMORY_BYTES, CODE_MAX_MEMORY_BYTES))
        resource.setrlimit(resource.RLIMIT_NPROC, (CODE_MAX_PROCS, CODE_MAX_PROCS))
        resource.setrlimit(resource.RLIMIT_FSIZE, (CODE_MAX_FILE_BYTES, CODE_MAX_FILE_BYTES))
    except Exception:
        pass

ADVANCED_TEST_CASES = [
    {
        "name": "Path Traversal Protection",
        "prompt": "write a Python function safe_read_file(base_dir, user_path) for a file manager panel. User passes relative path user_path, function should only allow reading files inside base_dir. If user_path tries to escape base_dir via ../, must raise PermissionError. For legal paths return file content as string. Return code only, no explanation, only import os.",
        "harness": """
import tempfile, os, shutil

tmpdir = tempfile.mkdtemp()
base = os.path.join(tmpdir, 'base')
os.makedirs(base)
outside = os.path.join(tmpdir, 'outside')
os.makedirs(outside)

with open(os.path.join(base, 'note.txt'), 'w') as f:
    f.write('hello inside')
with open(os.path.join(outside, 'secret.txt'), 'w') as f:
    f.write('TOP SECRET')

result1 = safe_read_file(base, 'note.txt')
assert result1 == 'hello inside', f"normal read failed: {result1!r}"

blocked = False
try:
    safe_read_file(base, '../outside/secret.txt')
except PermissionError:
    blocked = True
assert blocked, "SECURITY_FAIL: relative traversal NOT blocked!"

blocked2 = False
try:
    safe_read_file(base, os.path.join(outside, 'secret.txt'))
except PermissionError:
    blocked2 = True
assert blocked2, "SECURITY_FAIL: absolute traversal NOT blocked!"

shutil.rmtree(tmpdir)
print("ALL_PASS")
""",
        "expected": "ALL_PASS",
    },
    {
        "name": "SiteManager Multi-method",
        "prompt": "Write Python class SiteManager for website panel. Methods: add_site(name,port)-ValueError if name/port exists. remove_site(name)-KeyError if not found. list_sites()-sorted list [(name,port),...]. get_port(name)-return port or KeyError. Return code only.",
        "harness": """
sm = SiteManager()
sm.add_site('blog', 8080)
sm.add_site('shop', 8081)
sm.add_site('api', 8082)

assert sm.list_sites() == [('api', 8082), ('blog', 8080), ('shop', 8081)]

try:
    sm.add_site('blog', 9999)
    assert False, "dup name not caught"
except ValueError:
    pass

try:
    sm.add_site('new', 8080)
    assert False, "dup port not caught"
except ValueError:
    pass

assert sm.get_port('shop') == 8081

sm.remove_site('api')
assert sm.list_sites() == [('blog', 8080), ('shop', 8081)]

try:
    sm.remove_site('notexist')
    assert False
except KeyError:
    pass

sm.add_site('api2', 8082)
assert sm.get_port('api2') == 8082
print("ALL_PASS")
""",
        "expected": "ALL_PASS",
    },
    {
        "name": "Thread-Safe LRU Cache",
        "prompt": "Write a thread-safe Python class LRUCache(capacity) used as a caching layer accessed by multiple worker threads concurrently. Methods: get(key) returns the value or -1 if missing, and must mark the key as most recently used; put(key, value) inserts/updates the value and marks it as most recently used, evicting the Least Recently Used entry when size would exceed capacity. Must be safe under concurrent access from many threads at once (use proper locking, no race conditions or deadlocks). Return code only, no explanation, only standard library (threading/collections allowed).",
        "harness": """
import threading

cache = LRUCache(2)
cache.put(1, 'a')
cache.put(2, 'b')
assert cache.get(1) == 'a', "basic get failed"
cache.put(3, 'c')  # capacity=2, key 1 was just accessed so key 2 is LRU and must be evicted
assert cache.get(2) == -1, "LRU eviction order wrong: key 2 should have been evicted"
assert cache.get(1) == 'a'
assert cache.get(3) == 'c'

cache2 = LRUCache(50)
errors = []

def worker(base):
    try:
        for i in range(300):
            k = (base + i) % 50
            cache2.put(k, base * 1000 + i)
            cache2.get(k)
    except Exception as e:
        errors.append(repr(e))

threads = [threading.Thread(target=worker, args=(t,)) for t in range(8)]
for t in threads:
    t.start()
for t in threads:
    t.join(timeout=8)
assert not any(t.is_alive() for t in threads), "DEADLOCK_FAIL: worker thread did not finish (possible deadlock)"
assert not errors, f"concurrency errors: {errors}"
print("ALL_PASS")
""",
        "expected": "ALL_PASS",
    },
    {
        "name": "Quoted CSV Field Parser",
        "prompt": "Write a Python function parse_csv_line(line) implementing RFC4180-style CSV parsing for a single line: fields are separated by commas; a field may be wrapped in double quotes to contain literal commas; inside a quoted field, two consecutive double quotes represent one literal escaped double quote character; return a list of field strings with quoting/escaping resolved (quotes removed, escaped quotes unescaped). An empty line should return ['']. Return code only, no explanation, only standard library.",
        "harness": """
tests = [
    ('a,b,c', ['a', 'b', 'c']),
    ('"a,b",c,"d""e"', ['a,b', 'c', 'd"e']),
    ('a,,c', ['a', '', 'c']),
    ('"quoted only"', ['quoted only']),
    ('"a""b""c"', ['a"b"c']),
    ('', ['']),
    ('x,"",y', ['x', '', 'y']),
]
for line, expected in tests:
    got = parse_csv_line(line)
    assert got == expected, f"line={line!r} expected={expected!r} got={got!r}"
print("ALL_PASS")
""",
        "expected": "ALL_PASS",
    },
    {
        "name": "Precision-Safe Average",
        "prompt": "Write a Python function safe_average(values) for a billing/metering system. values is a list that may contain ints, floats, and None (None means a missing reading and must be skipped/ignored). Round the final result to 2 decimal places using Python's built-in round() (banker's rounding). If, after ignoring None entries, there are zero numeric values left (including on an empty input list), raise ValueError. If any element is not an int, float, or None (e.g. a string or list), raise TypeError. Return code only, no explanation, only standard library.",
        "harness": """
assert safe_average([1, 2, 3]) == 2.0
assert safe_average([10, None, 20, None]) == 15.0
assert safe_average([1, 2, 2]) == round(5 / 3, 2)

try:
    safe_average([])
    assert False, "empty list should raise ValueError"
except ValueError:
    pass

try:
    safe_average([None, None])
    assert False, "all-None input should raise ValueError"
except ValueError:
    pass

try:
    safe_average([1, "2", 3])
    assert False, "non-numeric element should raise TypeError"
except TypeError:
    pass

print("ALL_PASS")
""",
        "expected": "ALL_PASS",
    },
]


class ScanState:
    """扫描运行状态, 供 API 层轮询读取"""

    def __init__(self):
        self.lock = threading.Lock()
        self.results_lock = threading.Lock()
        self.running = False
        self.logs = []          # [{seq, ts, text}]
        self.results = None     # 最终 JSON 结果
        self.stop_event = threading.Event()
        self._seq = 0
        self.thread = None
        self.concurrency = 3
        self.active_hosts = set()   # 正在运行中的主机, 防止同一主机被重复并发运行
        self.active_hosts_lock = threading.Lock()

    def log(self, text):
        with self.lock:
            self._seq += 1
            self.logs.append({
                "seq": self._seq,
                "ts": datetime.now().strftime("%H:%M:%S"),
                "text": text,
            })

    def get_logs_since(self, since):
        with self.lock:
            return [l for l in self.logs if l["seq"] > since]

    def reset(self):
        with self.lock:
            self.logs = []
            self.results = None
            self._seq = 0
        with self.active_hosts_lock:
            self.active_hosts = set()
        self.stop_event.clear()

    def request_stop(self):
        self.stop_event.set()

    def is_stopping(self):
        return self.stop_event.is_set()

    def try_acquire_host(self, host):
        """尝试独占运行某个主机, 已在运行中则返回 False (不允许重复并发)"""
        with self.active_hosts_lock:
            if host in self.active_hosts:
                return False
            self.active_hosts.add(host)
            return True

    def release_host(self, host):
        with self.active_hosts_lock:
            self.active_hosts.discard(host)


def discover_models(host):
    try:
        resp = requests.get(f"{host}/api/tags", timeout=10)
        data = resp.json()
        models = [m["name"] for m in data.get("models", [])]
        return models, None
    except Exception as e:
        return [], str(e)


def quick_test(host, model):
    try:
        resp = requests.post(
            f"{host}/api/generate",
            json={"model": model, "prompt": "say ok", "stream": False},
            timeout=QUICK_TEST_TIMEOUT,
        )
        data = resp.json()
        if "error" in data:
            return False, data["error"]
        return True, None
    except Exception as e:
        return False, str(e)


def extract_code(text):
    patterns = [r"```python\s*(.*?)```", r"```\s*(.*?)```"]
    for pat in patterns:
        matches = re.findall(pat, text, re.DOTALL)
        if matches:
            return matches[0].strip()
    return text.strip()


def run_code(code, harness):
    """
    在受限子进程中执行模型生成的代码 + 测试 harness。
    安全说明：这里执行的是不受信任模型返回的代码，因此:
      - 使用 python3 -I (隔离模式) 忽略用户 site-packages / 环境变量注入
      - 通过 preexec_fn 设置 CPU / 内存 / 进程数 / 文件大小上限，压低失控代码
        （例如尝试常驻挖矿、fork 炸弹）的破坏半径
      - 在独立的临时目录中运行并在结束后清理，避免污染宿主文件系统
    这是尽力而为的纵深防御，不是完整沙箱；生产环境建议进一步用容器/gVisor级隔离。
    """
    full_code = code + "\n\n" + harness
    preexec = _limit_code_resources if platform.system() != "Windows" else None
    with tempfile.TemporaryDirectory(prefix="ollama-scan-") as workdir:
        try:
            result = subprocess.run(
                ["python3", "-I", "-c", full_code],
                capture_output=True,
                text=True,
                timeout=CODE_RUN_TIMEOUT,
                preexec_fn=preexec,
                cwd=workdir,
            )
            if result.returncode != 0:
                return None, (result.stdout + result.stderr).strip()[-400:]
            return result.stdout.strip(), None
        except subprocess.TimeoutExpired:
            return None, "execution timeout"
        except Exception as e:
            return None, str(e)


def query_model(host, model, prompt):
    try:
        resp = requests.post(
            f"{host}/api/generate",
            json={"model": model, "prompt": prompt, "stream": False},
            timeout=TIMEOUT,
        )
        data = resp.json()
        if "error" in data:
            return None, data["error"]
        return data.get("response", ""), None
    except Exception as e:
        return None, str(e)


def process_host(host, state: ScanState):
    """
    单个主机的完整流水线: 发现模型 -> 快速可用性测试 -> 高级测试
    在线程池的一个 worker 中运行, 日志加主机前缀以便在并发输出中区分
    """
    tag = f"[{host}]"
    host_result = {"models": [], "viability": {}, "advanced": {}}

    if state.is_stopping():
        return host_result

    state.log(f"{tag} 开始扫描 ...")
    models, err = discover_models(host)
    if err:
        state.log(f"{tag} 发现模型失败: {err}")
        return host_result
    if not models:
        state.log(f"{tag} 未发现模型")
        return host_result

    state.log(f"{tag} 发现 {len(models)} 个模型: {', '.join(models)}")
    host_result["models"] = models

    viable = {}
    for model in models:
        if state.is_stopping():
            state.log(f"{tag} 收到停止信号, 中断可用性测试")
            host_result["viability"] = viable
            return host_result
        ok, err = quick_test(host, model)
        viable[model] = ok
        state.log(f"{tag} {model}: {'可用' if ok else '不可用 - ' + str(err)[:80]}")
    host_result["viability"] = viable

    viable_models = [m for m, ok in viable.items() if ok]
    if not viable_models:
        state.log(f"{tag} 没有可用模型, 跳过高级测试")
        return host_result

    for model in viable_models:
        if state.is_stopping():
            state.log(f"{tag} 收到停止信号, 中断高级测试")
            break
        state.log(f"{tag} {model}: 开始高级测试")
        results = run_advanced_tests(host, model, state, tag=tag)
        host_result["advanced"][model] = results

    state.log(f"{tag} 扫描完成")
    return host_result


def run_advanced_tests(host, model, state, tag=""):
    results = []
    for case in ADVANCED_TEST_CASES:
        if state.is_stopping():
            state.log(f"{tag} 已收到停止信号, 中断高级测试")
            break
        start = time.time()
        response, err = query_model(host, model, case["prompt"])
        elapsed = time.time() - start

        if err:
            results.append((case["name"], "REQUEST_FAIL", err, elapsed))
            state.log(f"{tag}   [{case['name']}] REQUEST_FAIL ({elapsed:.1f}s) {err[:100]}")
            continue

        code = extract_code(response)
        output, run_err = run_code(code, case["harness"])

        if run_err:
            results.append((case["name"], "CODE_ERROR", run_err, elapsed))
            state.log(f"{tag}   [{case['name']}] CODE_ERROR ({elapsed:.1f}s) {run_err[:100]}")
            continue

        if output == case["expected"] or "ALL_PASS" in output:
            results.append((case["name"], "PASS", None, elapsed))
            state.log(f"{tag}   [{case['name']}] PASS ({elapsed:.1f}s)")
        else:
            results.append((case["name"], "WRONG_OUTPUT", output, elapsed))
            state.log(f"{tag}   [{case['name']}] WRONG_OUTPUT ({elapsed:.1f}s) {str(output)[:100]}")

    return results


def run_scan(hosts, state: ScanState, concurrency=3):
    """
    主扫描流程, 在后台线程中执行。
    使用线程池并发处理多个主机, 并发数由 concurrency (1-100) 控制。
    同一个主机不会被重复并发运行 (try_acquire_host 保证独占)。
    """
    import concurrent.futures

    state.reset()
    state.running = True
    state.concurrency = concurrency
    try:
        state.log("=" * 60)
        state.log(f"开始扫描 {len(hosts)} 个主机, 并发数: {concurrency}")
        state.log("=" * 60)

        all_results = {}  # host -> host_result

        def worker(host):
            if not state.try_acquire_host(host):
                state.log(f"[{host}] 已在运行中, 跳过重复任务")
                return host, None
            try:
                return host, process_host(host, state)
            finally:
                state.release_host(host)

        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, min(100, concurrency))) as pool:
            futures = {pool.submit(worker, host): host for host in hosts}
            for future in concurrent.futures.as_completed(futures):
                host, result = future.result()
                if result is not None:
                    with state.results_lock:
                        all_results[host] = result

        state.log("")
        state.log("=" * 60)
        state.log("最终结果")
        state.log("=" * 60)

        discovered = {h: r["models"] for h, r in all_results.items() if r.get("models")}
        viability = {}
        advanced = {}
        for h, r in all_results.items():
            for m, ok in r.get("viability", {}).items():
                viability[f"{h}|{m}"] = ok
            for m, tests in r.get("advanced", {}).items():
                advanced[f"{h}|{m}"] = tests

        passed_models = []
        failed_models = []
        for key, tests in advanced.items():
            host, model = key.split("|", 1)
            passed_tests = sum(1 for _, status, _, _ in tests if status == "PASS")
            total_tests = len(tests)
            model_key = f"{model} @ {host}"
            if total_tests > 0 and passed_tests == total_tests:
                passed_models.append(model_key)
            else:
                failed_models.append(model_key)

        if passed_models:
            state.log("全部通过的模型:")
            for m in passed_models:
                state.log(f"  ✔ {m}")
        if failed_models:
            state.log("存在失败项的模型:")
            for m in failed_models:
                state.log(f"  ✘ {m}")
        if not passed_models and not failed_models:
            state.log("没有模型进入高级测试阶段")

        state.results = {
            "discovered": discovered,
            "viability": viability,
            "advanced": {
                key: [
                    {"test": name, "status": status, "detail": (str(detail)[:300] if detail else None), "elapsed": elapsed}
                    for name, status, detail, elapsed in tests
                ]
                for key, tests in advanced.items()
            },
            "generated_at": datetime.now().isoformat(),
        }
        state.log("\n扫描完成" if not state.is_stopping() else "\n扫描已停止")
    except Exception as e:
        state.log(f"扫描过程中发生异常: {e}")
    finally:
        state.running = False


def start_scan_thread(hosts, state: ScanState, concurrency=3):
    if state.running:
        return False
    t = threading.Thread(target=run_scan, args=(hosts, state, concurrency), daemon=True)
    state.thread = t
    t.start()
    return True
