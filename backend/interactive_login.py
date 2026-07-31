import asyncio
import json
import logging
import uuid
import time
from typing import Dict, Optional, Tuple
from datetime import datetime, timezone

from playwright.async_api import async_playwright, Page, Browser, BrowserContext, CDPSession
from playwright_stealth import stealth_async

logger = logging.getLogger(__name__)

def timestamp_iso() -> str:
    """生成符合 ISO 8601 标准的当前 UTC 时间字符串"""
    return datetime.now(timezone.utc).isoformat()

class LoginSession:
    """
    单个交互式登录会话，管理 Playwright 实例、页面和 CDP 截屏推送
    """
    def __init__(self, session_id: str, login_url: str):
        self.session_id = session_id
        self.login_url = login_url
        self.created_at = time.time()
        self.last_active = time.time()
        self.is_active = True
        
        self.viewport = {"width": 1280, "height": 800}
        self.quality = 60
        
        self.queue: asyncio.Queue = asyncio.Queue()
        
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None
        self._cdp = None
        self._frame_session_id = None

    async def start(self):
        try:
            self._playwright = await async_playwright().start()
            
            # 1. 优化 Chromium 启动参数以躲避 Cloudflare 检测
            self._browser = await self._playwright.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    "--disable-blink-features=AutomationControlled", # 禁用自动化标记
                    "--disable-infobars",
                    "--window-size=1280,800",
                ],
            )
            
            # 2. 伪装真实的上下文配置
            self._context = await self._browser.new_context(
                viewport=self.viewport,
                user_agent=(
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
                ),
                locale="zh-CN",
                timezone_id="Asia/Shanghai",
                device_scale_factor=1,
            )
            
            self._page = await self._context.new_page()
            
            # 3. 强行抹除 headless / automation 特征
            await self._page.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', {
                    get: () => undefined
                });
                Object.defineProperty(navigator, 'plugins', {
                    get: () => [1, 2, 3, 4, 5]
                });
                Object.defineProperty(navigator, 'languages', {
                    get: () => ['zh-CN', 'zh', 'en']
                });
            """)
            
            # 4. 注入 playwright-stealth 脚本
            await stealth_async(self._page)
            
            # 5. 设置 CDP Session 用于实时传屏
            self._cdp = await self._context.new_cdp_session(self._page)
            self._cdp.on("Page.screencastFrame", self._on_frame)
            
            await self._page.goto(self.login_url, wait_until="domcontentloaded", timeout=30000)
            
            await self._cdp.send(
                "Page.startScreencast",
                {
                    "format": "jpeg",
                    "quality": self.quality,
                    "maxWidth": self.viewport["width"],
                    "maxHeight": self.viewport["height"],
                    "everyNthFrame": 1,
                }
            )
            logger.info(f"会话 {self.session_id} 初始化并启动成功")
        except Exception as e:
            logger.error(f"启动会话 {self.session_id} 失败: {e}")
            await self.close()
            raise e

    def _on_frame(self, event):
        """CDP 截图回调函数"""
        self.last_active = time.time()
        data = event.get("data")
        metadata = event.get("metadata")
        sessionId = event.get("sessionId")
        
        self._frame_session_id = sessionId
        
        if data:
            frame_payload = {
                "type": "frame",
                "data": data,
                "metadata": metadata,
                "timestamp": timestamp_iso()
            }
            try:
                self.queue.put_nowait(frame_payload)
            except asyncio.QueueFull:
                pass

    async def ack_frame(self):
        """确认已接收帧，告知 CDP 允许发送下一帧"""
        if self._cdp and self._frame_session_id:
            try:
                await self._cdp.send("Page.screencastFrameAck", {"sessionId": self._frame_session_id})
            except Exception as e:
                logger.warning(f"确认帧失败: {e}")

    async def inject_click(self, x: int, y: int, button: str = "left"):
        """模拟点击"""
        self.last_active = time.time()
        if self._page:
            try:
                await self._page.mouse.click(x, y, button=button)
            except Exception as e:
                logger.error(f"注入点击失败: {e}")

    async def inject_type(self, text: str):
        """模拟键盘输入字符串"""
        self.last_active = time.time()
        if self._page:
            try:
                await self._page.keyboard.type(text)
            except Exception as e:
                logger.error(f"注入文本失败: {e}")

    async def inject_key(self, key: str):
        """模拟特定按键 (Enter, Backspace 等)"""
        self.last_active = time.time()
        if self._page:
            try:
                await self._page.keyboard.press(key)
            except Exception as e:
                logger.error(f"注入按键失败: {e}")

    async def close(self):
        """清理并关闭所有会话资源"""
        self.is_active = False
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
        logger.info(f"会话 {self.session_id} 已关闭")


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
                # 超过 10 分钟无操作自动清理
                if now - session.last_active > 600:
                    expired_ids.append(sid)
            
            for sid in expired_ids:
                logger.info(f"会话 {sid} 超时，开始自动清理")
                session = self.sessions.pop(sid, None)
                if session:
                    await session.close()

    async def create_session(self, login_url: str) -> str:
        session_id = str(uuid.uuid4())
        session = LoginSession(session_id, login_url)
        await session.start()
        self.sessions[session_id] = session
        return session_id

    async def create(self, login_url: str, **kwargs) -> str:
        """为保持 compatibility / main.py 调用的 create 别名"""
        return await self.create_session(login_url)

    def get_session(self, session_id: str) -> Optional[LoginSession]:
        return self.sessions.get(session_id)

    def get(self, session_id: str) -> Optional[LoginSession]:
        """别名方法"""
        return self.get_session(session_id)

    async def close_session(self, session_id: str):
        session = self.sessions.pop(session_id, None)
        if session:
            await session.close()

    async def close(self, session_id: str):
        """别名方法"""
        await self.close_session(session_id)

login_manager = SessionManager()
