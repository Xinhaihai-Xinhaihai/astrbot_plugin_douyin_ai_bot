"""截取登录界面并解码验证二维码，拒绝占位图和验证码截图。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

LOGIN_ROOTS = (
    "#douyin_login_landing_flat_container, [role='dialog'], [class*='login-panel'], [class*='loginPanel'], "
    "[class*='login-modal'], [class*='loginModal'], [id*='login']"
)


@dataclass(frozen=True)
class QrCapture:
    png: bytes
    payload: str


def decode_qr(png: bytes) -> QrCapture | None:
    import cv2
    import numpy as np

    pixels = cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_COLOR)
    if pixels is None:
        return None
    pixels = cv2.copyMakeBorder(
        pixels, 24, 24, 24, 24, cv2.BORDER_CONSTANT, value=(255, 255, 255)
    )
    payload, points, _ = cv2.QRCodeDetector().detectAndDecode(pixels)
    if not payload or points is None:
        return None
    points = points.reshape(-1, 2)
    x0, y0 = np.floor(points.min(axis=0) - 16).astype(int)
    x1, y1 = np.ceil(points.max(axis=0) + 16).astype(int)
    crop = pixels[
        max(0, y0) : min(y1, pixels.shape[0]), max(0, x0) : min(x1, pixels.shape[1])
    ]
    ok, encoded = cv2.imencode(".png", crop)
    return QrCapture(encoded.tobytes(), payload) if ok else None


async def login_visible(page) -> bool:
    for frame in page.frames:
        for text in ("扫码登录", "打开抖音", "使用抖音扫码"):
            if await frame.get_by_text(text, exact=False).first.is_visible():
                return True
    return False


async def open_login(page) -> None:
    """按页面当前状态打开弹窗、切换扫码。"""
    for frame in page.frames:
        tab = frame.get_by_text("扫码登录", exact=True).first
        if await tab.is_visible():
            await tab.click(timeout=1500)
            return
    if await login_visible(page):
        return
    for frame in page.frames:
        for button in (
            frame.get_by_role("button", name="登录", exact=True).first,
            frame.get_by_text("登录", exact=True).first,
        ):
            if await button.is_visible():
                await button.click(timeout=1500)
                return


async def refresh_qr(page) -> None:
    for frame in page.frames:
        for text in ("点击刷新", "刷新二维码", "重新获取二维码"):
            button = frame.get_by_text(text, exact=True).first
            if await button.is_visible():
                await button.click(timeout=1500)
                return


async def capture_qr(page) -> QrCapture | None:
    # 始终从当前渲染画面截图，不按 img src 长度猜二维码。
    await page.screenshot(type="png", full_page=False, timeout=5000)
    if not await login_visible(page):
        return None
    for frame in page.frames:
        for root in await frame.locator(LOGIN_ROOTS).all():
            if not await root.is_visible():
                continue
            text = await root.inner_text(timeout=1500)
            if not any(
                hint in text for hint in ("扫码登录", "使用抖音扫码", "打开抖音")
            ):
                continue
            shot = await root.screenshot(type="png", timeout=3000)
            result = await asyncio.to_thread(decode_qr, shot)
            if result is not None:
                return result
    # 不对整个首页解码，防止把下载 App / 视频分享的二维码当成登录码。
    return None
