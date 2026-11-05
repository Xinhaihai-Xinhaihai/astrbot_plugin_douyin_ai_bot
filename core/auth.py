"""抖音扫码登录：拉起非无头浏览器让人扫码，持久化 profile，轮询 sessionid.

抖音没有公开的 Web 扫码登录 API（需要 a_bogus 签名），直接用持久化 profile 最稳：
扫一次，cookie 落盘，后续开播检测/发评论都自动带登录态。
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from astrbot.api import logger

from .browser import DouyinBrowser

logger = logging.getLogger(__name__)


async def wait_for_login(profile_dir: Path, channel: str = "msedge", timeout: int = 180) -> bool:
    """打开非无头浏览器等扫码，返回是否拿到登录态（sessionid cookie）."""
    from playwright.async_api import async_playwright

    pw = None
    ctx = None
    try:
        pw = await async_playwright().start()
        kwargs = {
            "user_data_dir": str(profile_dir),
            "headless": False,
            "viewport": {"width": 1280, "height": 900},
            "locale": "zh-CN",
        }
        if channel:
            kwargs["channel"] = channel
        try:
            ctx = await pw.chromium.launch_persistent_context(**kwargs)
        except Exception:
            kwargs.pop("channel", None)
            ctx = await pw.chromium.launch_persistent_context(**kwargs)

        page = await ctx.new_page()
        try:
            await page.goto("https://www.douyin.com/", wait_until="domcontentloaded", timeout=30000)
        except Exception:
            pass

        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(2)
            try:
                cookies = await ctx.cookies()
                sid = next((c for c in cookies if c.get("name") == "sessionid" and c.get("value")), None)
                if sid:
                    logger.info("[douyin_ai] 扫码登录成功，sessionid 已落盘")
                    return True
            except Exception:
                continue
        logger.warning("[douyin_ai] 扫码登录超时")
        return False
    except Exception as e:
        logger.warning(f"[douyin_ai] 扫码登录异常: {e}")
        return False
    finally:
        if ctx is not None:
            try:
                await ctx.close()
            except Exception:
                pass
        if pw is not None:
            try:
                await pw.stop()
            except Exception:
                pass


def parse_cookie_string(raw: str) -> list[dict]:
    """把浏览器复制的整段 Cookie 串（a=1; b=2; ...）转成 Playwright 格式，douyin 域。"""
    raw = (raw or "").strip()
    if not raw:
        return []
    items: list[dict] = []
    for part in raw.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, _, value = part.partition("=")
        name = name.strip()
        value = value.strip()
        if not name:
            continue
        items.append({"name": name, "value": value, "domain": ".douyin.com", "path": "/"})
    return items


async def login_from_cookies(cookie_str: str, profile_dir: Path, channel: str = "msedge") -> tuple[bool, int]:
    """方式一：导入 Cookie。返回 (是否含 sessionid, 导入了几条)。"""
    items = parse_cookie_string(cookie_str)
    if not items:
        return False, 0
    browser = DouyinBrowser.get(profile_dir=profile_dir, headless=True, channel=channel)
    try:
        await browser.set_cookies(items)
    except Exception as e:
        logger.warning(f"[douyin_ai] 导入 cookie 失败: {e}")
        return False, 0
    has_sid = any(i["name"] == "sessionid" and i["value"] for i in items)
    logger.info(f"[douyin_ai] 导入 {len(items)} 条 cookie，sessionid={'有' if has_sid else '无'}")
    return has_sid, len(items)


async def _grab_qr(page) -> bytes:
    """从登录弹窗里截二维码（PNG 字节），没弹窗返回空 bytes。"""
    try:
        for e in await page.query_selector_all("img"):
            try:
                src = await e.get_attribute("src") or ""
            except Exception:
                continue
            if src.startswith("data:image") and len(src) > 10000:
                return await e.screenshot()
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


async def qr_login_bytes(profile_dir: Path, channel: str = "msedge", timeout: int = 150) -> tuple[bytes, bool]:
    """方式二：无头浏览器抓登录二维码。返回 (二维码PNG字节, 是否扫成)。

    先开无头页触发登录弹窗截图二维码，轮询 sessionid，扫成返回 (空字节, True)。
    """
    from playwright.async_api import async_playwright

    pw = None
    ctx = None
    page = None
    try:
        pw = await async_playwright().start()
        kwargs = {
            "user_data_dir": str(profile_dir),
            "headless": True,
            "viewport": {"width": 1280, "height": 900},
            "locale": "zh-CN",
            "args": ["--no-sandbox", "--disable-blink-features=AutomationControlled"],
        }
        if channel:
            kwargs["channel"] = channel
        try:
            ctx = await pw.chromium.launch_persistent_context(**kwargs)
        except Exception:
            kwargs.pop("channel", None)
            ctx = await pw.chromium.launch_persistent_context(**kwargs)
        page = await ctx.new_page()
        try:
            await page.goto("https://www.douyin.com/", wait_until="domcontentloaded", timeout=30000)
        except Exception:
            pass
        # 点「登录」按钮触发二维码弹窗（没按钮就直接找二维码图）
        try:
            btn = await page.query_selector("text=登录")
            if btn:
                await btn.click()
                await page.wait_for_timeout(2500)
        except Exception:
            pass
        qr = await _grab_qr(page)
        if not qr:
            qr = await page.screenshot(full_page=False) or b""
        # 轮询登录态
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(3)
            try:
                cookies = await ctx.cookies()
                if any(c.get("name") == "sessionid" and c.get("value") for c in cookies):
                    logger.info("[douyin_ai] 二维码登录成功")
                    return b"", True
            except Exception:
                continue
        logger.warning("[douyin_ai] 二维码登录超时")
        return qr, False
    except Exception as e:
        logger.warning(f"[douyin_ai] 二维码登录异常: {e}")
        return b"", False
    finally:
        try:
            if ctx:
                await ctx.close()
        except Exception:
            pass
        try:
            if pw:
                await pw.stop()
        except Exception:
            pass


async def is_logged_in() -> bool:
    """当前持久化 profile 是否已登录."""
    from astrbot.api.star import StarTools

    profile = Path(StarTools.get_data_dir("astrbot_plugin_douyin_ai_bot")) / "browser_profile"
    if not profile.exists():
        return False
    browser = DouyinBrowser.get(profile_dir=profile, headless=True, channel="msedge")
    return await browser.is_logged_in()
