"""真实 Playwright 浏览器测试；页面及 Cookie 全部为本地构造的测试数据。"""

import base64
import io
import os
import time

import cv2
import numpy as np
import pytest
import qrcode

from core.auth import run_qr_login
from core.browser import DouyinBrowser
from core.captcha import match_distance
from core.login_capture import capture_qr, decode_qr


def png(pixels):
    return cv2.imencode(".png", pixels)[1].tobytes()


def data_url(data):
    return "data:image/png;base64," + base64.b64encode(data).decode()


def puzzle():
    mask = np.zeros((60, 60), np.uint8)
    cv2.rectangle(mask, (8, 12), (48, 52), 255, -1)
    cv2.circle(mask, (28, 12), 9, 255, -1)
    cv2.circle(mask, (48, 32), 8, 0, -1)
    bg = np.full((200, 320, 3), 210, np.uint8)
    bg[70:130, 190:250][mask > 0] = 35
    piece = np.full((60, 60, 4), 160, np.uint8)
    piece[:, :, 3] = mask
    return png(bg), png(piece)


def qr_png():
    output = io.BytesIO()
    qrcode.make("https://www.douyin.com/passport/sso/qrcode?token=TEST_ONLY").save(
        output, format="PNG"
    )
    return output.getvalue()


def test_qr_decoder_rejects_placeholder_and_keeps_real_code():
    assert decode_qr(png(np.full((240, 240, 3), 30, np.uint8))) is None
    result = decode_qr(qr_png())
    assert result is not None
    assert "TEST_ONLY" in result.payload
    assert decode_qr(result.png).payload == result.payload


@pytest.mark.parametrize("scale", [0.5, 1.0, 1.5])
def test_slider_distance_uses_display_scale(scale):
    bg, piece = puzzle()
    distance = match_distance(
        bg,
        piece,
        {"x": 400, "y": 200, "width": 320 * scale, "height": 200 * scale},
        {
            "x": 400 + 10 * scale,
            "y": 200 + 70 * scale,
            "width": 60 * scale,
            "height": 60 * scale,
        },
    )
    assert distance == pytest.approx(180 * scale, abs=3)


def test_slider_rejects_opaque_piece():
    bg, _ = puzzle()
    with pytest.raises(ValueError, match="透明轮廓"):
        match_distance(bg, bg, {}, {})


@pytest.fixture
async def browser(tmp_path):
    instance = DouyinBrowser(
        tmp_path / "profile", channel=os.environ.get("PLAYWRIGHT_TEST_CHANNEL", "")
    )
    await instance._ensure()
    yield instance
    await instance.close()


async def test_real_browser_slider_iframe_delay_send_and_save(browser):
    bg, piece = puzzle()
    # 使用当前线上 v2 验证框的类名；轨道宽度特意与背景不同。
    challenge = f"""
    <meta charset="utf-8"><style>body{{margin:0}} .captcha-slider-btn{{width:40px;height:36px;background:#ddd}}
    .captcha-slider-box{{width:260px;height:36px;background:#eee}}</style>
    <div id="captcha_container">
      <div style="position:relative;width:320px;height:200px">
        <img id="captcha_verify_image" width="320" height="200" src="{data_url(bg)}">
        <img id="captcha-verify_img_slide" style="position:absolute;left:10px;top:70px;width:60px;height:60px" src="{data_url(piece)}">
      </div>
      <div class="captcha-slider-box"><div class="captcha-slider-btn"></div></div>
    </div>
    <script>
      let start = null;
      let lastMove = 0;
      let pauses = [];
      let held = true;
      document.querySelector('.captcha-slider-btn').onmousedown = e => {{
        start = e.clientX;
        lastMove = performance.now();
        pauses = [];
        held = true;
      }};
      document.onmousemove = e => {{
        if (start === null) return;
        held = held && (e.buttons & 1) === 1;
        const now = performance.now();
        if (now - lastMove >= 1000) pauses.push(now - lastMove);
        lastMove = now;
      }};
      document.onmouseup = e => {{
        if (start !== null && Math.abs(e.clientX - start - 180) < 4) {{
          pauses.push(performance.now() - lastMove);
          parent.postMessage({{type: 'verified', pauses, held}}, '*');
        }}
        start = null;
      }};
    </script>"""
    import html

    document = f"""<meta charset="utf-8"><iframe id="verify" width="400" height="300" srcdoc="{html.escape(challenge, quote=True)}"></iframe>
    <div role="dialog" style="display:none" id="login"><p>扫码登录</p><img width="240" height="240" src="{data_url(qr_png())}"></div>
    <script>window.addEventListener('message', e => {{
      if(e.data.type === 'verified') {{
        window.dragResult=e.data;
        document.getElementById('verify').remove();
        document.getElementById('login').style.display='block';
        window.verifiedAt=performance.now();
      }}
    }});</script>"""
    await browser._ctx.route(
        "https://www.douyin.com/**",
        lambda route: route.fulfill(
            body=document, content_type="text/html; charset=utf-8"
        ),
    )
    statuses = []
    received = []

    async def send(png_bytes):
        page = browser._ctx.pages[-1]
        assert not page.is_closed()
        drag = await page.evaluate("window.dragResult")
        assert drag["held"]
        assert len(drag["pauses"]) == 4
        assert all(1400 <= pause < 2500 for pause in drag["pauses"])
        assert await page.evaluate("performance.now() - window.verifiedAt") >= 5000
        assert decode_qr(png_bytes) is not None
        received.append(png_bytes)
        await browser.set_cookies(
            [
                {
                    "name": "sessionid",
                    "value": "TEST_ONLY",
                    "domain": ".douyin.com",
                    "path": "/",
                }
            ]
        )

    async def status(text):
        statuses.append(text)

    assert await run_qr_login(
        browser, send, status, prepare_timeout=30, scan_timeout=10
    )
    assert len(received) == 1
    assert any("验证已通过" in item for item in statuses)
    assert browser._state_path.exists()
    assert all(page.url == "about:blank" for page in browser._ctx.pages)


async def test_restores_session_cookie_after_browser_restart(browser):
    await browser.set_cookies(
        [
            {
                "name": "sessionid",
                "value": "TEST_ONLY",
                "domain": ".douyin.com",
                "path": "/",
            }
        ]
    )
    await browser.save_login_state()
    # 删除浏览器自带 cookie，确保本测试检查的是额外保存的会话恢复机制。
    await browser._ctx.clear_cookies()
    await browser.close()
    restored = DouyinBrowser(
        browser.profile_dir, channel=os.environ.get("PLAYWRIGHT_TEST_CHANNEL", "")
    )
    try:
        assert await restored.is_logged_in()
    finally:
        await restored.close()


async def test_screenshot_waits_for_actual_qr(browser):
    page = await browser.new_page(home=False)
    await page.set_content(
        '<div role="dialog"><p>扫码登录</p><canvas width="200" height="200"></canvas></div>'
    )
    assert await capture_qr(page) is None
    await page.locator('[role="dialog"]').evaluate(
        "(el, src) => { const img = new Image(); img.src=src; el.append(img); }",
        data_url(qr_png()),
    )
    await page.locator("img").evaluate("img => img.decode()")
    assert await capture_qr(page) is not None


async def test_does_not_send_unrelated_qr_outside_login_dialog(browser):
    page = await browser.new_page(home=False)
    await page.set_content(
        f'<div role="dialog"><p>扫码登录</p><canvas width="200" height="200"></canvas></div><img src="{data_url(qr_png())}">'
    )
    await page.locator("img").evaluate("img => img.decode()")
    assert await capture_qr(page) is None


def test_session_cookie_requires_matching_domain_and_expiry():
    cookie = {
        "name": "sessionid",
        "value": "TEST_ONLY",
        "domain": ".douyin.com",
        "expires": -1,
    }
    assert DouyinBrowser._has_session([cookie])
    assert not DouyinBrowser._has_session([{**cookie, "domain": ".example.com"}])
    assert not DouyinBrowser._has_session([{**cookie, "expires": time.time() - 1}])
