"""共享 Playwright 浏览器：持久化登录态（扫码一次，长期复用）.

- 一个持久化 profile 目录存 douyin.com 全部 cookie（含登录态），
  开播检测 / 刷视频 / 发评论都用同一个上下文，登录态自动带。
- 浏览器懒启动，全局一个实例，插件 terminate 时关闭。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from astrbot.api import logger

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


class DouyinBrowser:
    """单例式共享浏览器（持久化 profile）."""

    _instance: "DouyinBrowser | None" = None

    def __init__(self, profile_dir: Path, headless: bool = True, channel: str = "msedge") -> None:
        self.profile_dir = Path(profile_dir)
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self.headless = headless
        self.channel = (channel or "").strip()
        self._pw = None
        self._ctx = None
        self._lock = asyncio.Lock()
        self._closed = False

    @classmethod
    def get(cls, profile_dir: Path, headless: bool = True, channel: str = "msedge") -> "DouyinBrowser":
        if cls._instance is None or cls._instance._closed:
            cls._instance = cls(profile_dir, headless, channel)
        return cls._instance

    async def _ensure(self) -> None:
        if self._ctx is not None and not self._closed:
            return
        async with self._lock:
            if self._ctx is not None and not self._closed:
                return
            from playwright.async_api import async_playwright

            self._pw = await async_playwright().start()
            kwargs: dict[str, Any] = {
                "user_data_dir": str(self.profile_dir),
                "headless": self.headless,
                "viewport": {"width": 1280, "height": 900},
                "locale": "zh-CN",
                "user_agent": USER_AGENT,
                "args": [
                    "--no-sandbox",
                    "--disable-blink-features=AutomationControlled",
                    "--disable-dev-shm-usage",
                    "--mute-audio",
                    "--autoplay-policy=no-user-gesture-required",
                ],
            }
            if self.channel:
                kwargs["channel"] = self.channel
            try:
                self._ctx = await self._pw.chromium.launch_persistent_context(**kwargs)
            except Exception as e:
                # 首选 channel 起不来就裸 chromium；还起不来就把真实原因抛出去
                logger.warning(f"[douyin_ai] 浏览器 channel={self.channel!r} 启动失败，降级裸 chromium: {e}")
                kwargs.pop("channel", None)
                try:
                    self._ctx = await self._pw.chromium.launch_persistent_context(**kwargs)
                except Exception as e2:
                    raise RuntimeError(
                        f"浏览器启动失败：channel={self.channel!r} 不可用且本机没有 playwright chromium。"
                        f"请装 Edge/Chrome 或运行 playwright install。原始错误: {e2}"
                    ) from e2
            self._ctx.set_default_timeout(30000)

    async def new_page(self, home: bool = True):
        await self._ensure()
        assert self._ctx is not None
        page = await self._ctx.new_page()
        if home:
            try:
                await page.goto("https://www.douyin.com/", wait_until="domcontentloaded", timeout=30000)
            except Exception:
                pass
        return page

    async def cookies(self) -> list[dict]:
        await self._ensure()
        assert self._ctx is not None
        return await self._ctx.cookies()

    def cookies_str(self, cookies: list[dict] | None = None) -> str:
        if cookies is None:
            return ""
        return "; ".join(f"{c['name']}={c['value']}" for c in cookies)

    async def set_cookies(self, items: list[dict]) -> None:
        """导入 cookie 到共享上下文（持久化到 profile）。"""
        await self._ensure()
        assert self._ctx is not None
        if not items:
            return
        await self._ctx.add_cookies(items)

    async def is_logged_in(self) -> bool:
        """当前 profile 是否处于登录态（有 sessionid cookie）."""
        try:
            cs = await self.cookies()
            return any(c.get("name") == "sessionid" and c.get("value") for c in cs)
        except Exception:
            return False

    async def open_login(self):
        """打开登录页，返回 page。若已登录会看到「我的」页，未登录自动弹扫码框。"""
        page = await self.new_page()
        try:
            await page.goto("https://www.douyin.com/", wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(5000)
        except Exception:
            pass
        return page

    async def qr_bytes(self, page) -> bytes:
        """从登录弹窗里截二维码，返回 PNG 字节。没弹窗返回空 bytes。"""
        try:
            els = await page.query_selector_all("img")
            for e in els:
                try:
                    src = await e.get_attribute("src") or ""
                except Exception:
                    continue
                if src.startswith("data:image") and len(src) > 10000:
                    return await e.screenshot()
            # 兜底：大 canvas
            for c in await page.query_selector_all("canvas"):
                try:
                    box = await c.bounding_box()
                except Exception:
                    continue
                if box and box["width"] >= 100:
                    return await c.screenshot()
        except Exception:
            pass
        return b""

    async def close_page(self, page) -> None:
        try:
            if page:
                await page.close()
        except Exception:
            pass

    async def close(self) -> None:
        async with self._lock:
            self._closed = True
            try:
                if self._ctx:
                    await self._ctx.close()
            except Exception:
                pass
            try:
                if self._pw:
                    await self._pw.stop()
            except Exception:
                pass
            self._ctx = None
            self._pw = None
            DouyinBrowser._instance = None
