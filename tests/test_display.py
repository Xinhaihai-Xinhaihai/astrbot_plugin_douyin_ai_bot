import asyncio
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from core import display


async def test_headless_needs_no_virtual_display(monkeypatch):
    monkeypatch.setattr(display, "sys", SimpleNamespace(platform="linux"))
    which = Mock(side_effect=AssertionError("must not search for Xvfb"))
    monkeypatch.setattr(display.shutil, "which", which)
    assert await display.BrowserDisplay().prepare("headless") == {"headless": True}


async def test_auto_uses_existing_display(monkeypatch):
    monkeypatch.setattr(display, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setenv("DISPLAY", ":42")
    assert await display.BrowserDisplay().prepare("auto") == {"headless": False}


async def test_missing_xvfb_has_actionable_error(monkeypatch):
    monkeypatch.setattr(display, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.setattr(display.shutil, "which", lambda name: None)
    with pytest.raises(
        display.BrowserEnvironmentError, match="apt-get install -y xvfb"
    ):
        await display.BrowserDisplay().prepare("auto")


async def test_virtual_display_is_private_and_stopped_on_close(monkeypatch):
    monkeypatch.setattr(display, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.setattr(display.shutil, "which", lambda name: "/usr/bin/Xvfb")
    proc = SimpleNamespace(returncode=None, terminate=Mock(), wait=AsyncMock())
    spawn = AsyncMock(return_value=proc)

    async def connect(factory, stream):
        protocol = factory()
        protocol._stream_reader.feed_data(b"93\n")
        return SimpleNamespace(close=stream.close), protocol

    monkeypatch.setattr(
        display,
        "asyncio",
        SimpleNamespace(
            **{
                **vars(asyncio),
                "get_running_loop": lambda: SimpleNamespace(connect_read_pipe=connect),
                "create_subprocess_exec": spawn,
            }
        ),
    )
    manager = display.BrowserDisplay()
    result = await manager.prepare("auto")
    assert result["headless"] is False
    assert result["env"]["DISPLAY"] == ":93"
    assert "DISPLAY" not in os.environ
    assert "-displayfd" in spawn.await_args.args
    assert spawn.await_args.args[-2:] == ("-nolisten", "tcp")
    await manager.close()
    await manager.close()
    proc.terminate.assert_called_once()
    proc.wait.assert_awaited_once()


async def test_cancellation_during_xvfb_start_cleans_up(monkeypatch):
    monkeypatch.setattr(display, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.setattr(display.shutil, "which", lambda name: "/usr/bin/Xvfb")
    proc = SimpleNamespace(returncode=None, terminate=Mock(), wait=AsyncMock())

    async def connect(factory, stream):
        protocol = factory()
        protocol._stream_reader.readline = AsyncMock(
            side_effect=asyncio.CancelledError()
        )
        return SimpleNamespace(close=stream.close), protocol

    monkeypatch.setattr(
        display,
        "asyncio",
        SimpleNamespace(
            **{
                **vars(asyncio),
                "get_running_loop": lambda: SimpleNamespace(connect_read_pipe=connect),
                "create_subprocess_exec": AsyncMock(return_value=proc),
            }
        ),
    )
    manager = display.BrowserDisplay()
    with pytest.raises(asyncio.CancelledError):
        await manager.prepare("auto")
    assert manager.process is None
    proc.terminate.assert_called_once()


async def test_virtual_display_force_stop_after_timeout(monkeypatch):
    proc = SimpleNamespace(
        returncode=None, terminate=Mock(), kill=Mock(), wait=AsyncMock()
    )
    monkeypatch.setattr(
        display,
        "asyncio",
        SimpleNamespace(
            **{
                **vars(asyncio),
                "wait_for": AsyncMock(side_effect=asyncio.TimeoutError()),
            }
        ),
    )
    # 模拟 wait_for 消费的 awaitable，避免未 await 协程警告。
    proc.wait = Mock(return_value=asyncio.get_running_loop().create_future())
    proc.wait.return_value.set_result(0)
    manager = display.BrowserDisplay()
    manager.process = proc
    await manager.close()
    proc.kill.assert_called_once()
