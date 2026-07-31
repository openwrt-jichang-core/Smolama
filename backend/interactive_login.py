import asyncio
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Dict, Optional

from playwright.async_api import async_playwright

try:
    from playwright_stealth import stealth_async
except Exception:  # pragma: no cover - 依赖缺失时优雅降级，不影响核心功能
    stealth_async = None

logger = logging.getLogger(__name__)


def timestamp_iso() -> str:
    """生成符合 ISO 8601 标准的当前 UTC 时间字符串"""
    return datetime.now(timezone.utc).isoformat()


class LoginSession:
    """
    单个交互式登录会话，管理 Playwright 实例、页面和 CDP 截屏推送。

    对外暴露的属性/方法是 main.py 里 REST + WebSocket 接口实际依赖的契约：
    id / status / error / login_url / viewport / ws_clients，
    以及 fill_credentials / click / move / type_text / press_key / scroll / get_cookies。
    """

    def __init__(self, session_id: str, login_url: str):
        self.id = session_id
        self.session_id = session_id  # 兼容旧字段名
        self.login_url = login_url
        self.created_at = time.time()
        self.last_active = time.time()

        # starting -> ready / error，main.py 会读这两个字段返回给前端
        self.status = "starting"
        self.error: Optional[str] = None

        self.viewport = {"width": 1280, "height": 800}
        self.quality = 60

        # 所有连接到这个会话的 WebSocket 客户端，收到新帧时逐个广播
        self.ws_clients: set = set()

        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None
        self._cdp = None
        self._frame_session_id = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    async def start(self):
        self._loop = asyncio.get_event_loop()
        try:
            self._playwright = await async_playwright().start()
            # 使用 False 搭配 Docker Xvfb 虚拟屏幕，补充服务器防崩溃参数
            self._browser = await self._playwright.chromium.launch(
                headless=False,
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    "--disable-software-rasterizer",
                    "--disable-blink-features=AutomationControlled",
                    "--no-first-run",
                    "--no-service-autorun",
                    "--password-store=basic",
                ],
            )
            self._context = await self._browser.new_context(
                viewport=self.viewport,
                user_agent=(
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
                ),
                locale="zh-CN",
                timezone_id="Asia/Shanghai",
            )
            self._page = await self._context.new_page()

            # 强行抹除 automation 特征
            await self._page.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
            """)
            if stealth_async:
                try:
                    await stealth_async(self._page)
                except Exception as e:
                    logger.warning(f"stealth 注入失败，继续（不影响核心功能）: {e}")

            # CDP Session 画面传输
            self._cdp = await self._context.new_cdp_session(self._page)
            self._cdp.on("Page.screencastFrame", self._on_frame)
            await self._cdp.send(
                "Page.startScreencast",
                {
                    "format": "jpeg",
                    "quality": self.quality,
                    "maxWidth": self.viewport["width"],
                    "maxHeight": self.viewport["height"],
                    "everyNthFrame": 1,
                },
            )

            # 打开目标页面
            try:
                await self._page.goto(
                    self.login_url, wait_until="domcontentloaded", timeout=45000
                )
            except Exception as goto_err:
                logger.warning(
                    f"页面跳转未彻底完成，但继续传输画面: {goto_err}"
                )

            self.status = "ready"
            logger.info(f"会话 {self.id} 初始化并启动成功")
        except Exception as e:
            self.status = "error"
            self.error = f"{type(e).__name__}: {e}"
            logger.error(f"启动会话 {self.id} 失败: {e}", exc_info=True)
            await self.close()
            raise

    def _on_frame(self, event):
        """CDP 截图回调（同步回调，转成异步任务去广播 + ack，不阻塞 CDP 事件循环）"""
        self.last_active = time.time()
        data = event.get("data")
        metadata = event.get("metadata")
        frame_session_id = event.get("sessionId")
        self._frame_session_id = frame_session_id

        if not data:
            return

        payload = {
            "type": "frame",
            "data": data,
            "metadata": metadata,
            "timestamp": timestamp_iso(),
        }
        if self._loop:
            self._loop.create_task(self._broadcast(payload))
            if frame_session_id is not None:
                self._loop.create_task(self._ack_frame(frame_session_id))

    async def _broadcast(self, payload):
        """把一帧画面推给所有连接的 WebSocket 客户端"""
        dead = []
        for ws in list(self.ws_clients):
            try:
                await ws.send_json(payload)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.ws_clients.discard(ws)

    async def _ack_frame(self, frame_session_id):
        """确认帧，让 CDP 继续推送下一帧"""
        if self._cdp:
            try:
                await self._cdp.send(
                    "Page.screencastFrameAck", {"sessionId": frame_session_id}
                )
            except Exception as e:
                logger.warning(f"确认帧失败: {e}")

    async def fill_credentials(
        self,
        username: str = "",
        password: str = "",
        username_selector: str = "",
        password_selector: str = "",
    ):
        """尝试把账号/密码自动填进登录表单，找不到就算了，不抛出去阻断流程"""
        if not self._page:
            return
        if username:
            selector = username_selector or (
                'input[type="text"], input[type="email"], '
                'input[name*="user" i], input[id*="user" i], '
                'input[name*="email" i], input[id*="email" i]'
            )
            try:
                await self._page.fill(selector, username, timeout=3000)
            except Exception as e:
                logger.warning(f"自动填充账号失败（可以手动填）: {e}")
        if password:
            selector = password_selector or 'input[type="password"]'
            try:
                await self._page.fill(selector, password, timeout=3000)
            except Exception as e:
                logger.warning(f"自动填充密码失败（可以手动填）: {e}")

    async def click(self, x: float, y: float, button: str = "left"):
        """模拟点击。down/up 之间留一点小延迟（真人点击不可能是 0ms），
        配合前端现在会转发的真实 mousemove 轨迹，让这次点击尽量贴近真实人类操作，
        而不是"鼠标瞬移到坐标后立刻按下抬起"这种典型自动化特征。"""
        self.last_active = time.time()
        if self._page:
            try:
                await self._page.mouse.move(x, y)
                await self._page.mouse.down(button=button)
                await asyncio.sleep(0.06)
                await self._page.mouse.up(button=button)
            except Exception as e:
                logger.error(f"注入点击失败: {e}")

    async def move(self, x: float, y: float):
        """模拟鼠标移动（用于 hover 触发的菜单/校验框）"""
        self.last_active = time.time()
        if self._page:
            try:
                await self._page.mouse.move(x, y)
            except Exception as e:
                logger.error(f"注入移动失败: {e}")

    async def type_text(self, text: str):
        """模拟键盘输入字符串"""
        self.last_active = time.time()
        if self._page and text:
            try:
                await self._page.keyboard.type(text)
            except Exception as e:
                logger.error(f"注入文本失败: {e}")

    async def press_key(self, key: str):
        """模拟特定按键 (Enter, Backspace 等)"""
        self.last_active = time.time()
        if self._page and key:
            try:
                await self._page.keyboard.press(key)
            except Exception as e:
                logger.error(f"注入按键失败: {e}")

    async def scroll(self, dx: float, dy: float):
        """模拟滚轮滚动"""
        self.last_active = time.time()
        if self._page:
            try:
                await self._page.mouse.wheel(dx, dy)
            except Exception as e:
                logger.error(f"注入滚动失败: {e}")

    async def get_cookies(self):
        """拿到当前上下文的全部 Cookie，供 finish 接口保存"""
        if not self._context:
            return []
        try:
            return await self._context.cookies()
        except Exception as e:
            logger.error(f"获取 Cookie 失败: {e}")
            return []

    async def close(self):
        """清理资源"""
        self.status = "closed"
        if self._cdp:
            try:
                await self._cdp.send("Page.stopScreencast")
            except Exception:
                pass
        if self._page:
            try:
                await self._page.close()
            except Exception:
                pass
        if self._context:
            try:
                await self._context.close()
            except Exception:
                pass
        if self._browser:
            try:
                await self._browser.close()
            except Exception:
                pass
        if self._playwright:
            try:
                await self._playwright.stop()
            except Exception:
                pass
        self.ws_clients.clear()
        logger.info(f"会话 {self.id} 已关闭")


class SessionManager:
    """全局会话管理器"""

    def __init__(self):
        self.sessions: Dict[str, LoginSession] = {}
        self._cleanup_task = None

    def start_cleanup_loop(self):
        if not self._cleanup_task:
            self._cleanup_task = asyncio.create_task(self._cleanup_loop())

    async def _cleanup_loop(self):
        while True:
            await asyncio.sleep(60)
            now = time.time()
            expired_ids = []
            for sid, session in list(self.sessions.items()):
                if now - session.last_active > 600:
                    expired_ids.append(sid)
            for sid in expired_ids:
                logger.info(f"会话 {sid} 超时，开始自动清理")
                session = self.sessions.pop(sid, None)
                if session:
                    await session.close()

    async def create(self, login_url: str, **kwargs) -> LoginSession:
        """创建并启动一个新会话，返回会话对象本身（main.py 会直接用 sess.xxx）"""
        session_id = str(uuid.uuid4())
        session = LoginSession(session_id, login_url)
        self.sessions[session_id] = session
        try:
            await session.start()
        except Exception:
            self.sessions.pop(session_id, None)
            raise
        return session

    def get(self, session_id: str) -> Optional[LoginSession]:
        return self.sessions.get(session_id)

    async def close(self, session_id: str):
        session = self.sessions.pop(session_id, None)
        if session:
            await session.close()


login_manager = SessionManager()
