"""
交互式登录模块（给"地址自动发现"用）
====================================

背景：地址自动发现原本只是一次普通的 requests.get()，如果目标网页需要登录/挂了
Cloudflare Turnstile 人机验证，普通 HTTP 请求过不去，也没法执行 JS 去点验证框。

这里的做法：后端起一个真实的无头 Chromium（Playwright），把它的画面通过 CDP 的
Page.startScreencast 实时转发到前端（就是 WebSocket 里一帧一帧发 JPEG base64），
你在自己电脑/手机浏览器里看到的就是这个远程浏览器"正在渲染的画面"，点击/输入
会通过 WebSocket 转发回后端，由 Playwright 在真实浏览器里执行——包括你自己去点
Cloudflare 的勾选框，这一步没法也不该由代码代劳，必须是真人来点。

登录成功后，只把这次会话产生的 Cookie 落盘保存，用户名/密码只在这一次会话里
用一次（自动尝试填进输入框），不会被写进任何文件。之后地址自动发现的定时抓取
会带着这份 Cookie 去请求，不需要你再手动复制粘贴 Cookie 字符串。

依赖：pip install playwright playwright-stealth && playwright install --with-deps chromium
"""
import asyncio
import secrets
import time
from typing import Optional

from playwright.async_api import async_playwright
from playwright_stealth import stealth_async  # 🔥 1. 导入 stealth 模块

# 会话相关的时间限制：
# - 一个交互登录会话最多存活这么久（哪怕一直没人点"完成"），防止有人开了就忘、
#   浏览器进程一直占着内存/CPU。
SESSION_TTL_SECONDS = 15 * 60
# 画面分辨率：固定下来，前端按这个尺寸做坐标换算（canvas 显示尺寸 -> 这个尺寸）。
VIEWPORT = {"width": 1280, "height": 800}

# 常见的"用户名/账号"输入框选择器，按顺序尝试，命中第一个就填。
_USERNAME_SELECTORS = [
    "input[type='email']",
    "input[autocomplete='username']",
    "input[name*='user' i]",
    "input[id*='user' i]",
    "input[name*='account' i]",
    "input[id*='account' i]",
    "input[name*='login' i]",
    "input[id*='login' i]",
    "input[placeholder*='用户' i]",
    "input[placeholder*='账号' i]",
    "input[placeholder*='email' i]",
    "input[placeholder*='邮箱' i]",
]


class LoginSession:
    def __init__(self, session_id: str, login_url: str):
        self.id = session_id
        self.login_url = login_url
        self.created_at = time.time()
        self.status = "starting"  # starting | live | error | closed
        self.error: Optional[str] = None
        self.viewport = dict(VIEWPORT)
        self.ws_clients: set = set()

        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None
        self._cdp = None
        self._frame_session_id = None

    async def start(self):
        try:
            self._playwright = await async_playwright().start()
            # --no-sandbox：容器里通常没有给 Chromium 沙箱需要的那些 Linux
            # capabilities（本项目 docker-compose 是 cap_drop: ALL），不加这个
            # 参数浏览器直接起不来。这是"跑在受限容器里的无头浏览器"的常规取舍。
            self._browser = await self._playwright.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    # 🔥 2. 移除 Chromium Blink 引擎的自动化标志
                    "--disable-blink-features=AutomationControlled",
                ],
            )
            self._context = await self._browser.new_context(
                viewport=self.viewport,
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
                ),
            )
            self._page = await self._context.new_page()

            # 🔥 3. 在跳转页面前给 page 注入反检测隐身补丁
            await stealth_async(self._page)

            self._cdp = await self._context.new_cdp_session(self._page)
            self._cdp.on("Page.screencastFrame", self._on_frame)
            await self._page.goto(self.login_url, wait_until="domcontentloaded", timeout=20000)
            await self._cdp.send(
                "Page.startScreencast",
                {
                    "format": "jpeg",
                    "quality": 60,
                    "maxWidth": self.viewport["width"],
                    "maxHeight": self.viewport["height"],
                    "everyNthFrame": 1,
                },
            )
            self.status = "live"
        except Exception as e:
            self.status = "error"
            self.error = f"{type(e).__name__}: {e}"
            await self._safe_close()

    def _on_frame(self, params):
        # CDP 的事件回调是同步调用的，这里只负责把处理丢进当前 event loop，
        # 不在回调里直接 await（pyee 的同步回调不允许你在里面 await）。
        asyncio.create_task(self._broadcast_frame(params))

    async def _broadcast_frame(self, params):
        try:
            await self._cdp.send("Page.screencastFrameAck", {"sessionId": params["sessionId"]})
        except Exception:
            pass
        data = params.get("data")
        if not data:
            return
        dead = []
        for ws in list(self.ws_clients):
            try:
                await ws.send_json({"type": "frame", "data": data})
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.ws_clients.discard(ws)

    async def fill_credentials(self, username: str, password: str,
                                username_selector: str = "", password_selector: str = ""):
        """尝试自动把账号/密码填进登录表单。找不到对应输入框就静默跳过——
        用户仍然可以在画面里自己点、自己打字，这只是省事儿，不是必须成功的步骤。"""
        if not self._page:
            return
        if username:
            selectors = [username_selector] if username_selector else _USERNAME_SELECTORS
            for sel in selectors:
                if not sel:
                    continue
                try:
                    loc = self._page.locator(sel).first
                    if await loc.count() > 0:
                        await loc.fill(username, timeout=2000)
                        break
                except Exception:
                    continue
        if password:
            sel = password_selector or "input[type='password']"
            try:
                loc = self._page.locator(sel).first
                if await loc.count() > 0:
                    await loc.fill(password, timeout=2000)
            except Exception:
                pass

    async def click(self, x: float, y: float):
        if self._page:
            await self._page.mouse.click(x, y)

    async def move(self, x: float, y: float):
        if self._page:
            await self._page.mouse.move(x, y)

    async def type_text(self, text: str):
        if self._page:
            await self._page.keyboard.type(text)

    async def press_key(self, key: str):
        if self._page:
            await self._page.keyboard.press(key)

    async def scroll(self, dx: float, dy: float):
        if self._page:
            await self._page.mouse.wheel(dx, dy)

    async def current_url(self) -> str:
        return self._page.url if self._page else ""

    async def get_cookies(self):
        if not self._context:
            return []
        return await self._context.cookies()

    async def _safe_close(self):
        try:
            if self._cdp:
                await self._cdp.send("Page.stopScreencast")
        except Exception:
            pass
        try:
            if self._context:
                await self._context.close()
        except Exception:
            pass
        try:
            if self._browser:
                await self._browser.close()
        except Exception:
            pass
        try:
            if self._playwright:
                await self._playwright.stop()
        except Exception:
            pass

    async def close(self):
        await self._safe_close()
        self.status = "closed"
        for ws in list(self.ws_clients):
            try:
                await ws.close()
            except Exception:
                pass
        self.ws_clients.clear()


class InteractiveLoginManager:
    """进程内单例，管理所有正在进行的交互登录会话（正常情况下同一时间只有一个，
    但没有强制限制成单会话，避免多个管理员同时用时互相冲突）。"""

    def __init__(self):
        self.sessions: dict[str, LoginSession] = {}
        self._lock = asyncio.Lock()

    async def create(self, login_url: str) -> LoginSession:
        await self._gc()
        session_id = secrets.token_urlsafe(24)
        sess = LoginSession(session_id, login_url)
        self.sessions[session_id] = sess
        await sess.start()
        return sess

    def get(self, session_id: str) -> Optional[LoginSession]:
        return self.sessions.get(session_id)

    async def close(self, session_id: str):
        sess = self.sessions.pop(session_id, None)
        if sess:
            await sess.close()

    async def _gc(self):
        now = time.time()
        stale = [sid for sid, s in self.sessions.items() if now - s.created_at > SESSION_TTL_SECONDS]
        for sid in stale:
            await self.close(sid)


login_manager = InteractiveLoginManager()