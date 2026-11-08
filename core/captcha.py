"""识别并操作抖音网页的拼图滑块；以页面验证框消失确认通过。"""

from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass

from playwright.async_api import Error as PlaywrightError

BACKGROUND = (
    "#captcha-verify-image, #captcha_verify_image, "
    ".captcha_verify_img--wrapper .captcha-verify-image"
)
PIECE = (
    ".captcha_verify_img_slide, #captcha-verify-image-slide, #captcha-verify_img_slide"
)
HANDLE = (
    ".secsdk-captcha-drag-icon, .captcha_verify_slide--slidebar, .captcha-slider-btn"
)
TRACK = ".secsdk-captcha-drag-wrapper, .captcha_verify_slide--slidebar-wrap, .captcha-slider-box"
CONTAINER = (
    ".captcha_verify_container, #captcha_container, "
    "[class*='captcha-verify-container'], .secsdk-captcha-drag-wrapper, .vc-captcha-verify"
)
DRAG_SEGMENTS = (0.30, 0.58, 0.82, 1.0)
SEGMENT_PAUSE_SECONDS = 1.5


class CaptchaRejected(RuntimeError):
    """页面明确拒绝当前验证，不继续提交同一被拒绝的会话。"""


@dataclass
class Challenge:
    frame: object
    root: object


async def first_visible(scope, selector: str):
    """避开隐藏的旧弹窗，兼容同页多个匹配元素。"""
    for locator in await scope.locator(selector).all():
        if await locator.is_visible():
            return locator
    return None


async def find_challenge(page) -> Challenge | None:
    # iframe 中的 bounding_box 也使用主页面坐标，可直接交给 page.mouse。
    for frame in page.frames:
        try:
            root = await first_visible(frame, CONTAINER)
            if root is None:
                root = await first_visible(frame, BACKGROUND)
            if root is not None:
                return Challenge(frame, root)
        except PlaywrightError:
            if not frame.is_detached():
                raise
    return None


def match_distance(
    background: bytes, piece: bytes, box: dict, piece_box: dict
) -> float:
    """透明拼图轮廓匹配缺口，按实际显示尺寸换算 CSS 拖动距离。"""
    import cv2
    import numpy as np

    bg = cv2.imdecode(np.frombuffer(background, np.uint8), cv2.IMREAD_COLOR)
    fg = cv2.imdecode(np.frombuffer(piece, np.uint8), cv2.IMREAD_UNCHANGED)
    if bg is None or fg is None or fg.ndim != 3 or fg.shape[2] != 4:
        raise ValueError("滑块图片缺少透明轮廓，无法可靠定位缺口")
    width, height = round(box["width"]), round(box["height"])
    pw, ph = round(piece_box["width"]), round(piece_box["height"])
    if min(width, height, pw, ph) < 8 or pw >= width or ph > height:
        raise ValueError("滑块图片尺寸异常")
    bg = cv2.resize(bg, (width, height))
    fg = cv2.resize(fg, (pw, ph))
    mask = (fg[:, :, 3] > 127).astype(np.uint8) * 255
    if not np.any(mask == 0) or not np.any(mask == 255):
        raise ValueError("滑块图片没有可识别的拼图轮廓")
    # 比较轮廓，避免拼图颜色与暗色缺口不一致导致模板匹配偏移。
    contour = cv2.morphologyEx(mask, cv2.MORPH_GRADIENT, np.ones((3, 3), np.uint8))
    edges = cv2.Canny(cv2.cvtColor(bg, cv2.COLOR_BGR2GRAY), 60, 160)
    edges = cv2.dilate(edges, np.ones((3, 3), np.uint8))
    scores = cv2.matchTemplate(edges, contour, cv2.TM_CCORR_NORMED)
    initial_x = piece_box["x"] - box["x"]
    initial_y = round(piece_box["y"] - box["y"])
    left = max(0, round(initial_x + pw * 0.7))
    top, bottom = max(0, initial_y - 4), min(scores.shape[0], initial_y + 5)
    region = scores[top:bottom, left:]
    if region.size == 0:
        raise ValueError("滑块缺口搜索范围为空")
    _, confidence, _, location = cv2.minMaxLoc(np.nan_to_num(region))
    if confidence < 0.35:
        raise ValueError("滑块缺口识别置信度不足")
    return float(left + location[0] - initial_x)


async def _image_bytes(page, locator) -> bytes:
    """读取已加载图片原始像素，保留拼图透明通道。"""
    src = await locator.evaluate("el => el.currentSrc || el.src || ''")
    if src.startswith("data:image/") and ";base64," in src:
        return base64.b64decode(src.split(",", 1)[1])
    if src.startswith(("https://", "http://")):
        response = await page.context.request.get(src, timeout=10000)
        try:
            if response.ok:
                return await response.body()
        finally:
            await response.dispose()
    raise ValueError("滑块图片尚未加载或图片地址不受支持")


async def solve_slider(page, challenge: Challenge) -> bool:
    """执行一次拼图滑动。旋转、点选等类型返回 False，由调用方限次处理。"""
    frame = challenge.frame
    background = await first_visible(frame, BACKGROUND)
    piece = await first_visible(frame, PIECE)
    handle = await first_visible(frame, HANDLE)
    if background is None or piece is None or handle is None:
        return False
    box, piece_box, handle_box = await asyncio.gather(
        background.bounding_box(), piece.bounding_box(), handle.bounding_box()
    )
    if not box or not piece_box or not handle_box:
        return False
    bg_data, piece_data = await asyncio.gather(
        background.screenshot(type="png", timeout=3000), _image_bytes(page, piece)
    )
    distance = await asyncio.to_thread(
        match_distance, bg_data, piece_data, box, piece_box
    )
    track = await first_visible(frame, TRACK)
    # 当前 v2 拼图和把手按相同 CSS 像素移动；旧组件才按可移动范围换算。
    modern = "captcha-slider-btn" in (await handle.get_attribute("class") or "").split()
    if track is not None and not modern:
        track_box = await track.bounding_box()
        if track_box:
            distance *= (track_box["width"] - handle_box["width"]) / (
                box["width"] - piece_box["width"]
            )
    if distance <= 0:
        return False
    x = handle_box["x"] + handle_box["width"] / 2
    y = handle_box["y"] + handle_box["height"] / 2
    await _drag_in_segments(page.mouse, x, y, distance)
    clear_samples = 0
    for _ in range(16):
        await asyncio.sleep(0.25)
        current = await find_challenge(page)
        if current is None:
            clear_samples += 1
            if clear_samples >= 3:
                return True
        else:
            clear_samples = 0
            text = await current.root.inner_text(timeout=1500)
            if "操作过快" in text or "5014" in text:
                raise CaptchaRejected(
                    "抖音拒绝了当前验证会话（5014/502），并非继续放慢就一定有效。"
                    "本次已停止自动提交。请使用 browser_mode=auto，在 Linux 安装 Xvfb "
                    "并重载插件后再登录；若仍失败，可用 dy_cookie 导入登录态。"
                )
    return False


async def _drag_in_segments(mouse, x: float, y: float, distance: float) -> None:
    """按住鼠标分四段移动，每段停 1.5 秒，全部完成才松开提交。"""
    await mouse.move(x, y)
    await asyncio.sleep(0.3)
    await mouse.down()
    try:
        start = 0.0
        for end in DRAG_SEGMENTS:
            for step in range(1, 13):
                progress = step / 12
                position = start + (end - start) * (1 - (1 - progress) ** 3)
                await mouse.move(x + distance * position, y)
                await asyncio.sleep(0.04)
            # 保持按住，松开会提前提交验证码；暂停期间允许任务被取消。
            await asyncio.sleep(SEGMENT_PAUSE_SECONDS)
            start = end
    finally:
        await mouse.up()


async def refresh_challenge(challenge: Challenge) -> None:
    button = await first_visible(
        challenge.frame,
        ".secsdk_captcha_refresh, .secsdk-captcha-refresh, .vc-captcha-refresh, "
        "[class*='captcha'] [class*='refresh']",
    )
    if button is not None:
        await button.click(timeout=1500)
