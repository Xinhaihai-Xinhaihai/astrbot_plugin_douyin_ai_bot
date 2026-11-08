import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core import captcha


async def test_cancelling_during_segment_pause_releases_mouse(monkeypatch):
    mouse = SimpleNamespace(move=AsyncMock(), down=AsyncMock(), up=AsyncMock())

    async def sleep(seconds):
        if 1 <= seconds <= 2:
            mouse.down.assert_awaited_once()
            mouse.up.assert_not_awaited()
            raise asyncio.CancelledError

    monkeypatch.setattr(captcha, "asyncio", SimpleNamespace(sleep=sleep))
    with pytest.raises(asyncio.CancelledError):
        await captcha._drag_in_segments(mouse, 100, 200, 180)
    mouse.up.assert_awaited_once()
    assert 100 < mouse.move.await_args.args[0] < 280
