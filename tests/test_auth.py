import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from core import auth
from core.captcha import CaptchaRejected
from core.login_capture import QrCapture


class Clock:
    now = 0.0

    def time(self):
        return self.now

    async def sleep(self, seconds):
        self.now += seconds


@pytest.fixture
def session(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(
        auth,
        "asyncio",
        SimpleNamespace(
            get_running_loop=lambda: clock,
            sleep=clock.sleep,
            wait_for=asyncio.wait_for,
            TimeoutError=asyncio.TimeoutError,
        ),
    )
    page = SimpleNamespace(
        goto=AsyncMock(), is_closed=Mock(return_value=False), set_default_timeout=Mock()
    )
    browser = SimpleNamespace(
        new_page=AsyncMock(return_value=page),
        is_logged_in=AsyncMock(return_value=False),
        close_page=AsyncMock(),
        save_login_state=AsyncMock(),
    )
    for name in ("open_login", "refresh_qr", "refresh_challenge"):
        monkeypatch.setattr(auth, name, AsyncMock())
    monkeypatch.setattr(auth, "find_challenge", AsyncMock(return_value=None))
    monkeypatch.setattr(
        auth, "capture_qr", AsyncMock(return_value=QrCapture(b"qr", "token"))
    )
    monkeypatch.setattr(auth, "solve_slider", AsyncMock(return_value=True))
    return clock, browser


async def test_sends_before_login_and_keeps_page_alive(session):
    clock, browser = session

    async def send(png):
        assert png == b"qr"
        browser.close_page.assert_not_called()
        browser.is_logged_in.return_value = True

    assert await auth.run_qr_login(browser, send, AsyncMock())
    assert clock.now < 150
    browser.save_login_state.assert_awaited_once()
    browser.close_page.assert_awaited_once()


async def test_fresh_capture_at_least_five_seconds_after_slider(session):
    clock, browser = session
    auth.find_challenge.side_effect = lambda page: object() if clock.now == 0 else None
    captured_at = []

    async def capture(page):
        captured_at.append(clock.now)
        return QrCapture(b"fresh", "token")

    auth.capture_qr.side_effect = capture
    send = AsyncMock(
        side_effect=lambda png: setattr(browser.is_logged_in, "return_value", True)
    )
    assert await auth.run_qr_login(browser, send, AsyncMock())
    assert captured_at == [5.0]
    send.assert_awaited_once_with(b"fresh")


async def test_second_challenge_resets_five_second_timer(session):
    clock, browser = session
    auth.find_challenge.side_effect = lambda page: (
        object() if clock.now in (0, 3) else None
    )
    send_times = []

    async def send(png):
        send_times.append(clock.now)
        browser.is_logged_in.return_value = True

    assert await auth.run_qr_login(browser, send, AsyncMock())
    assert send_times == [8.0]
    assert auth.solve_slider.await_count == 2


async def test_placeholder_is_never_sent_and_scan_budget_starts_on_send(session):
    clock, browser = session
    auth.capture_qr.side_effect = lambda page: (
        None if clock.now < 20 else QrCapture(b"qr", "token")
    )
    send = AsyncMock()
    assert not await auth.run_qr_login(
        browser, send, AsyncMock(), prepare_timeout=25, scan_timeout=30
    )
    send.assert_awaited_once()
    assert clock.now == 50


async def test_timeout_without_a_qr_has_no_image(session):
    _, browser = session
    auth.capture_qr.return_value = None
    send = AsyncMock()
    with pytest.raises(auth.LoginError, match="二维码超时"):
        await auth.run_qr_login(browser, send, AsyncMock(), prepare_timeout=5)
    send.assert_not_called()
    browser.close_page.assert_awaited_once()


async def test_failed_slider_is_bounded_and_never_sends(session):
    _, browser = session
    auth.find_challenge.return_value = object()
    auth.solve_slider.return_value = False
    send = AsyncMock()
    with pytest.raises(auth.LoginError, match="滑块验证未通过"):
        await auth.run_qr_login(browser, send, AsyncMock(), slider_attempts=3)
    assert auth.solve_slider.await_count == 3
    send.assert_not_called()
    browser.close_page.assert_awaited_once()


async def test_server_rejection_stops_without_refresh_or_more_submissions(session):
    _, browser = session
    auth.find_challenge.return_value = object()
    auth.solve_slider.side_effect = CaptchaRejected("5014/502")
    send = AsyncMock()
    with pytest.raises(auth.LoginError, match="5014/502"):
        await auth.run_qr_login(browser, send, AsyncMock())
    auth.solve_slider.assert_awaited_once()
    auth.refresh_challenge.assert_not_awaited()
    send.assert_not_awaited()
    browser.close_page.assert_awaited_once()


async def test_qr_rotation_sends_new_token_only_without_extending_timeout(session):
    clock, browser = session
    auth.capture_qr.side_effect = lambda page: QrCapture(
        b"new" if clock.now >= 12 else b"old", "new" if clock.now >= 12 else "old"
    )
    send = AsyncMock()
    assert not await auth.run_qr_login(browser, send, AsyncMock(), scan_timeout=20)
    assert [call.args[0] for call in send.await_args_list] == [b"old", b"new"]
    assert clock.now == 20


async def test_send_failure_closes_page(session):
    _, browser = session
    with pytest.raises(RuntimeError, match="send failed"):
        await auth.run_qr_login(
            browser, AsyncMock(side_effect=RuntimeError("send failed")), AsyncMock()
        )
    browser.close_page.assert_awaited_once()


async def test_cancellation_closes_page(session):
    _, browser = session
    with pytest.raises(asyncio.CancelledError):
        await auth.run_qr_login(
            browser, AsyncMock(side_effect=asyncio.CancelledError()), AsyncMock()
        )
    browser.close_page.assert_awaited_once()


async def test_already_logged_in_does_not_open_login_page(session):
    _, browser = session
    browser.is_logged_in.return_value = True
    send = AsyncMock()
    assert await auth.run_qr_login(browser, send, AsyncMock())
    browser.new_page.assert_not_called()
    browser.save_login_state.assert_awaited_once()
    send.assert_not_called()


async def test_manual_login_saves_confirmed_session(session):
    _, browser = session
    browser.cdp_url = "http://browser:9222"
    browser.is_logged_in.side_effect = [False, True]
    statuses = []

    async def status(text):
        statuses.append(text)

    assert await auth.run_manual_login(browser, status, timeout=600)
    browser.save_login_state.assert_awaited_once()
    browser.close_page.assert_awaited_once()
    assert any("noVNC" in text for text in statuses)


async def test_manual_login_timeout_keeps_page_open(session):
    clock, browser = session
    browser.cdp_url = "http://browser:9222"
    assert not await auth.run_manual_login(browser, AsyncMock(), timeout=30)
    assert clock.now == 30
    browser.close_page.assert_not_awaited()


async def test_manual_login_requires_cdp(session):
    _, browser = session
    browser.cdp_url = ""
    with pytest.raises(auth.LoginError, match="browser_cdp_url"):
        await auth.run_manual_login(browser, AsyncMock())


async def test_login_status_distinguishes_cookie_only(tmp_path, monkeypatch):
    browser = SimpleNamespace(
        cookies=AsyncMock(
            return_value=[
                {
                    "name": "sessionid",
                    "value": "TEST_ONLY",
                    "domain": ".douyin.com",
                    "expires": -1,
                }
            ]
        ),
        is_logged_in=AsyncMock(return_value=False),
    )
    monkeypatch.setattr(auth.DouyinBrowser, "get", lambda **kwargs: browser)
    assert await auth.login_status(tmp_path / "profile") == (False, "cookie_only")
