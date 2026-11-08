"""共享 Playwright 浏览器：持久化登录态（扫码一次，长期复用）.

- 一个持久化 profile 目录存 douyin.com 全部 cookie（含登录态），
  开播检测 / 刷视频 / 发评论都用同一个上下文，登录态自动带。
- 浏览器懒启动，全局一个实例，插件 terminate 时关闭。
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

from astrbot.api import logger

from .display import BrowserDisplay

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


class DouyinBrowser:
    """单例式共享浏览器（持久化 profile）."""

    _instance: DouyinBrowser | None = None
    default_mode = "auto"

    def __init__(
        self,
        profile_dir: Path,
        headless: bool = True,
        channel: str = "msedge",
        mode: str | None = None,
    ) -> None:
        self.profile_dir = Path(profile_dir)
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self.headless = headless
        self.mode = mode or ("headless" if headless else "headed")
        self._display = BrowserDisplay()
        self.channel = (channel or "").strip()
        self._pw = None
        self._ctx = None
        self._lock = asyncio.Lock()
        self._state_lock = asyncio.Lock()
        self._closed = False
        self._state_path = self.profile_dir.parent / "login_cookies.json"

    @classmethod
    def get(
        cls, profile_dir: Path, headless: bool = True, channel: str = "msedge"
    ) -> DouyinBrowser:
        if cls._instance is None or cls._instance._closed:
            cls._instance = cls(profile_dir, headless, channel, mode=cls.default_mode)
        return cls._instance

    async def _ensure(self) -> None:
        if self._closed:
            raise RuntimeError("浏览器已关闭")
        if self._ctx is not None and not self._closed:
            return
        async with self._lock:
            if self._closed:
                raise RuntimeError("浏览器已关闭")
            if self._ctx is not None and not self._closed:
                return
            from playwright.async_api import async_playwright

            display_options = await self._display.prepare(self.mode)
            try:
                self._pw = await async_playwright().start()
            except BaseException:
                await self._display.close()
                raise
            kwargs: dict[str, Any] = {
                "user_data_dir": str(self.profile_dir),
                **display_options,
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
            except asyncio.CancelledError:
                await self._pw.stop()
                self._pw = None
                await self._display.close()
                raise
            except Exception as e:
                # 首选 channel 起不来就裸 chromium；还起不来就把真实原因抛出去
                logger.warning(
                    f"[douyin_ai] 浏览器 channel={self.channel!r} 启动失败，降级裸 chromium: {e}"
                )
                kwargs.pop("channel", None)
                try:
                    self._ctx = await self._pw.chromium.launch_persistent_context(
                        **kwargs
                    )
                except asyncio.CancelledError:
                    await self._pw.stop()
                    self._pw = None
                    await self._display.close()
                    raise
                except Exception as e2:
                    await self._pw.stop()
                    self._pw = None
                    await self._display.close()
                    raise RuntimeError(
                        f"浏览器启动失败：channel={self.channel!r} 不可用且本机没有 playwright chromium。"
                        f"请装 Edge/Chrome 或运行 playwright install。原始错误: {e2}"
                    ) from e2
            self._ctx.set_default_timeout(30000)
            try:
                if self._state_path.exists():
                    saved = await asyncio.to_thread(self._read_login_state)
                    current = await self._ctx.cookies()
                    if not self._has_session(current) and saved:
                        await self._ctx.add_cookies(saved)
            except (OSError, ValueError, TypeError) as exc:
                logger.warning(
                    "[douyin_ai] 已保存的登录态无法读取: %s", type(exc).__name__
                )

    @staticmethod
    def _has_session(cookies: list[dict]) -> bool:
        return any(
            c.get("name") in {"sessionid", "sessionid_ss"}
            and c.get("value")
            and c.get("domain", "").lstrip(".") in {"douyin.com", "www.douyin.com"}
            and (c.get("expires", -1) == -1 or c.get("expires", 0) > time.time())
            for c in cookies
        )

    def _read_login_state(self) -> list[dict]:
        cookies = json.loads(self._state_path.read_text(encoding="utf-8"))
        if not isinstance(cookies, list):
            raise ValueError("Invalid cookie state")
        return [
            c
            for c in cookies
            if isinstance(c, dict)
            and c.get("domain", "").lstrip(".") in {"douyin.com", "www.douyin.com"}
            and (c.get("expires", -1) == -1 or c.get("expires", 0) > time.time())
        ]

    async def save_login_state(self) -> None:
        """额外保存会话 cookie，确保浏览器重启后仍可恢复扫码结果。"""
        async with self._state_lock:
            cookies = await self.cookies()
            cookies = [
                c
                for c in cookies
                if c.get("domain", "").lstrip(".") in {"douyin.com", "www.douyin.com"}
            ]
            await asyncio.to_thread(self._write_login_state, cookies)

    def _write_login_state(self, cookies: list[dict]) -> None:
        fd, filename = tempfile.mkstemp(
            prefix=".login-", suffix=".tmp", dir=self._state_path.parent
        )
        temporary = Path(filename)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(cookies, stream, ensure_ascii=False)
            temporary.replace(self._state_path)
        finally:
            temporary.unlink(missing_ok=True)

    async def new_page(self, home: bool = True):
        await self._ensure()
        assert self._ctx is not None
        page = await self._ctx.new_page()
        if home:
            try:
                await page.goto(
                    "https://www.douyin.com/",
                    wait_until="domcontentloaded",
                    timeout=30000,
                )
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
            return self._has_session(cs)
        except Exception:
            return False

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
            await self._display.close()
            if DouyinBrowser._instance is self:
                DouyinBrowser._instance = None
