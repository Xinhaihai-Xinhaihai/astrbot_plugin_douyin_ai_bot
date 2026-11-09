import asyncio
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from astrbot.api.event import MessageEventResult
from astrbot.api.message_components import Image


@pytest.fixture
def plugin_module():
    root = Path(__file__).resolve().parents[1]
    name = "douyin_plugin_test"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            name, root / "__init__.py", submodule_search_locations=[str(root)]
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name + ".main"]


@pytest.fixture
def plugin(plugin_module, tmp_path):
    obj = object.__new__(plugin_module.DouyinAiBotPlugin)
    obj._c = lambda key, default=None: default
    obj._profile_dir = lambda: tmp_path / "profile"
    obj._login_task = None
    return obj


@pytest.fixture
def event():
    return SimpleNamespace(
        plain_result=lambda text: MessageEventResult().message(text),
        chain_result=lambda parts: MessageEventResult(chain=parts),
        send=AsyncMock(),
        get_platform_id=lambda: "test",
    )


async def test_login_sends_astrbot_image_to_requesting_event(
    plugin_module, plugin, event, monkeypatch
):
    async def run(browser, on_qr, on_status, **kwargs):
        await on_qr(b"test-png")
        return True

    monkeypatch.setattr(plugin_module, "run_qr_login", run)
    monkeypatch.setattr(plugin_module.DouyinBrowser, "get", lambda **kwargs: object())
    await plugin._run_login(event)
    image_message = event.send.await_args_list[0].args[0]
    assert isinstance(image_message.chain[0], Image)
    assert image_message.chain[0].file.startswith("base64://")
    assert "登录成功" in event.send.await_args_list[-1].args[0].chain[0].text


async def test_duplicate_login_does_not_spawn_another_task(plugin, event):
    started = asyncio.Event()

    async def wait(event):
        started.set()
        await asyncio.Event().wait()

    plugin._run_login = wait
    first = [x async for x in plugin._start_login(event)]
    task = plugin._login_task
    await started.wait()
    second = [x async for x in plugin._start_login(event)]
    assert plugin._login_task is task
    assert first and "已有扫码登录" in second[0].chain[0].text
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_unload_cancels_login_and_closes_shared_browser(
    plugin_module, plugin, monkeypatch
):
    plugin._login_task = asyncio.create_task(asyncio.Event().wait())
    task = plugin._login_task
    plugin._stop_polling = AsyncMock()
    plugin._stop_proactive = AsyncMock()
    plugin._stop_interact = AsyncMock()
    plugin._checker = None
    browser = SimpleNamespace(close=AsyncMock())
    monkeypatch.setattr(plugin_module.DouyinBrowser, "_instance", browser)
    await plugin.terminate()
    assert task.cancelled()
    assert plugin._login_task is None
    browser.close.assert_awaited_once()


async def test_notification_failure_does_not_leave_unobserved_task_error(
    plugin_module, plugin, event, monkeypatch
):
    monkeypatch.setattr(
        plugin_module,
        "run_qr_login",
        AsyncMock(side_effect=plugin_module.LoginError("test")),
    )
    monkeypatch.setattr(plugin_module.DouyinBrowser, "get", lambda **kwargs: object())
    event.send.side_effect = RuntimeError("transport unavailable")
    await plugin._run_login(event)


async def test_remote_login_requires_cdp_configuration(plugin, event):
    plugin._c = lambda key, default=None: default
    results = [item async for item in plugin._start_remote_login(event)]
    assert "browser_cdp_url" in results[0].chain[0].text
    assert plugin._login_task is None


async def test_remote_login_returns_vnc_url_and_starts_background_task(plugin, event):
    values = {
        "browser_cdp_url": "http://douyin-browser:9222",
        "browser_vnc_url": "https://vnc.example.test/vnc.html",
        "browser_manual_timeout": 600,
    }
    plugin._c = lambda key, default=None: values.get(key, default)
    started = asyncio.Event()

    async def wait(event):
        started.set()
        await asyncio.Event().wait()

    plugin._run_remote_login = wait
    results = [item async for item in plugin._start_remote_login(event)]
    task = plugin._login_task
    await started.wait()
    text = results[0].chain[0].text
    assert "https://vnc.example.test/vnc.html" in text
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
