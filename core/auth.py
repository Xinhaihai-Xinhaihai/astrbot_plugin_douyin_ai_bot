"""共享浏览器扫码登录：等待验证、即时推码、轮询登录并保存会话。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path

from astrbot.api import logger
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeout

from .browser import DouyinBrowser
from .captcha import CaptchaRejected, find_challenge, refresh_challenge, solve_slider
from .login_capture import capture_qr, open_login, refresh_qr

POST_CAPTCHA_DELAY = 5.0
POLL_INTERVAL = 1.0
MANUAL_POLL_INTERVAL = 2.0


class LoginError(RuntimeError):
    """可以向用户展示的登录错误（不包含 cookie 或二维码内容）。"""


async def run_manual_login(
    browser: DouyinBrowser,
    on_status: Callable[[str], Awaitable[None]],
    *,
    timeout: int = 600,
) -> bool:
    """在服务器浏览器中打开抖音，等待管理员通过 noVNC 完成登录。

    Args:
        browser: 已配置 CDP 地址的共享浏览器。
        on_status: 向发起命令的管理员发送状态文本。
        timeout: 等待管理员完成扫码、短信或滑块验证的秒数。

    Returns:
        抖音页面确认登录成功时为 True，超时或页面关闭时为 False。

    Raises:
        LoginError: 未配置 CDP 浏览器地址。
    """
    if not browser.cdp_url:
        raise LoginError("未配置 browser_cdp_url，无法使用服务器人工接管登录。")
    if await browser.is_logged_in():
        await browser.save_login_state()
        return True

    page = await browser.new_page(home=False)
    page.set_default_timeout(3000)
    loop = asyncio.get_running_loop()
    success = False
    try:
        try:
            await page.goto(
                "https://www.douyin.com/",
                wait_until="domcontentloaded",
                timeout=30000,
            )
        except PlaywrightTimeout:
            logger.info("[douyin_ai] 人工登录首页加载较慢，继续等待页面可用")
        try:
            await open_login(page)
        except PlaywrightError:
            logger.info("[douyin_ai] 未能自动打开登录框，管理员可在 noVNC 中手动点击")
        await on_status(
            "抖音登录页已打开。请通过 noVNC 完成扫码、短信或滑块验证，"
            "插件会持续检查真实登录状态。"
        )

        deadline = loop.time() + max(30, timeout)
        while loop.time() < deadline:
            if page.is_closed():
                raise LoginError("人工登录页面已关闭，请重新发送 dy_remote_login。")
            if await browser.is_logged_in():
                await browser.save_login_state()
                success = True
                return True
            await asyncio.sleep(min(MANUAL_POLL_INTERVAL, deadline - loop.time()))
        return False
    finally:
        if success and not page.is_closed():
            await browser.close_page(page)


async def run_qr_login(
    browser: DouyinBrowser,
    on_qr: Callable[[bytes], Awaitable[None]],
    on_status: Callable[[str], Awaitable[None]],
    *,
    prepare_timeout: int = 180,
    scan_timeout: int = 150,
    slider_attempts: int = 3,
) -> bool:
    try:
        return await asyncio.wait_for(
            _run_qr_login(
                browser,
                on_qr,
                on_status,
                prepare_timeout=prepare_timeout,
                scan_timeout=scan_timeout,
                slider_attempts=slider_attempts,
            ),
            timeout=prepare_timeout + scan_timeout + 60,
        )
    except asyncio.TimeoutError:
        raise LoginError("登录流程超时，请重新发送 dy_qr。") from None


async def _run_qr_login(
    browser: DouyinBrowser,
    on_qr: Callable[[bytes], Awaitable[None]],
    on_status: Callable[[str], Awaitable[None]],
    *,
    prepare_timeout: int = 180,
    scan_timeout: int = 150,
    slider_attempts: int = 3,
) -> bool:
    """二维码就绪即调用 on_qr，随后保持同一页面等待扫码。

    准备和扫码分别计时，二维码刷新不延长总扫码期限。
    取消、发送失败或超时只关闭登录页，其他任务继续使用共享上下文。
    """
    if await browser.is_logged_in():
        await browser.save_login_state()
        return True
    page = await browser.new_page(home=False)
    page.set_default_timeout(3000)
    loop = asyncio.get_running_loop()
    try:
        try:
            await page.goto(
                "https://www.douyin.com/", wait_until="domcontentloaded", timeout=30000
            )
        except PlaywrightTimeout:
            logger.info("[douyin_ai] 首页加载较慢，继续检查已渲染的登录画面")
        deadline = loop.time() + prepare_timeout
        scan_deadline = None
        ready_at = 0.0
        attempts = 0
        last_payload = ""
        last_sent = 0.0
        last_open = -10.0
        pending_challenge = False
        while loop.time() < (scan_deadline or deadline):
            if page.is_closed():
                raise LoginError("登录页面已关闭，请重新发送 dy_qr。")
            if await browser.is_logged_in():
                await browser.save_login_state()
                return True
            challenge = await find_challenge(page)
            if challenge is not None:
                if attempts >= slider_attempts:
                    raise LoginError(
                        "滑块验证未通过或遇到不支持的验证类型，请稍后重试 dy_qr，或用 dy_cookie 导入登录态。"
                    )
                attempts += 1
                await on_status(
                    f"检测到滑块验证，正在尝试处理（{attempts}/{slider_attempts}）…"
                )
                try:
                    solved = await solve_slider(page, challenge)
                except CaptchaRejected as exc:
                    raise LoginError(str(exc)) from exc
                except (ValueError, PlaywrightError) as exc:
                    logger.info(
                        "[douyin_ai] 滑块尝试 %s 未完成: %s",
                        attempts,
                        type(exc).__name__,
                    )
                    solved = False
                pending_challenge = not solved
                if solved:
                    ready_at = loop.time() + POST_CAPTCHA_DELAY
                    await on_status("滑块验证已通过，等待 5 秒后截取登录二维码。")
                else:
                    ready_at = loop.time() + POST_CAPTCHA_DELAY
                    try:
                        await refresh_challenge(challenge)
                    except PlaywrightError:
                        logger.debug("[douyin_ai] 验证框已变化，下一轮重新检测")
                await asyncio.sleep(POLL_INTERVAL)
                continue
            if pending_challenge:
                ready_at = loop.time() + POST_CAPTCHA_DELAY
                pending_challenge = False
                await on_status("验证框已消失，等待 5 秒后截取登录二维码。")
            if loop.time() < ready_at:
                await asyncio.sleep(min(POLL_INTERVAL, ready_at - loop.time()))
                continue
            try:
                if not last_payload and loop.time() - last_open >= 4:
                    await open_login(page)
                    last_open = loop.time()
                await refresh_qr(page)
                qr = await capture_qr(page)
            except PlaywrightError:
                if page.is_closed():
                    raise LoginError("登录页面已关闭，请重新发送 dy_qr。") from None
                await asyncio.sleep(POLL_INTERVAL)
                continue
            if qr is not None and await find_challenge(page) is None:
                if qr.payload != last_payload and (
                    not last_payload or loop.time() - last_sent >= 10
                ):
                    await on_qr(qr.png)
                    last_payload, last_sent = qr.payload, loop.time()
                    if scan_deadline is None:
                        scan_deadline = loop.time() + scan_timeout
            await asyncio.sleep(POLL_INTERVAL)
        if not last_payload:
            raise LoginError(
                "等待登录二维码超时，尚未检测到可扫描的二维码。请稍后重试 dy_qr。"
            )
        return False
    finally:
        await browser.close_page(page)


def parse_cookie_string(raw: str) -> list[dict]:
    """将 a=1; b=2 格式 Cookie 转成 Playwright 格式。"""
    items: list[dict] = []
    for part in (raw or "").strip().split(";"):
        if "=" not in part:
            continue
        name, _, value = part.strip().partition("=")
        if name.strip():
            items.append(
                {
                    "name": name.strip(),
                    "value": value.strip(),
                    "domain": ".douyin.com",
                    "path": "/",
                }
            )
    return items


async def login_from_cookies(
    cookie_str: str, profile_dir: Path, channel: str = "msedge"
) -> tuple[bool, int]:
    items = parse_cookie_string(cookie_str)
    if not items:
        return False, 0
    browser = DouyinBrowser.get(profile_dir=profile_dir, headless=True, channel=channel)
    await browser.set_cookies(items)
    has_sid = await browser.is_logged_in()
    if has_sid:
        await browser.save_login_state()
    logger.info("[douyin_ai] 导入 %s 条 cookie，sessionid=%s", len(items), has_sid)
    return has_sid, len(items)


async def is_logged_in() -> bool:
    from astrbot.api.star import StarTools

    profile = (
        Path(StarTools.get_data_dir("astrbot_plugin_douyin_ai_bot")) / "browser_profile"
    )
    if not profile.exists():
        return False
    browser = DouyinBrowser.get(profile_dir=profile, headless=True, channel="msedge")
    return await browser.is_logged_in()


async def login_status(profile_dir: Path | None = None) -> tuple[bool, str]:
    """返回登录确认状态和可供管理面板展示的原因。

    Args:
        profile_dir: 插件数据目录中的 browser_profile；留空时使用默认路径。

    Returns:
        `(True, "verified")` 表示抖音页面已确认登录；
        `(False, "cookie_only")` 表示只有会话 Cookie、尚未确认；
        `(False, "missing")` 表示没有可用会话 Cookie。
    """
    if profile_dir is None:
        from astrbot.api.star import StarTools

        profile_dir = (
            Path(StarTools.get_data_dir("astrbot_plugin_douyin_ai_bot"))
            / "browser_profile"
        )
    browser = DouyinBrowser.get(
        profile_dir=profile_dir,
        headless=True,
        channel="msedge",
    )
    cookies = await browser.cookies()
    if not DouyinBrowser._has_session(cookies):
        return False, "missing"
    if await browser.is_logged_in():
        return True, "verified"
    return False, "cookie_only"
