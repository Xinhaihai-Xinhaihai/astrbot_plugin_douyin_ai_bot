"""为 Linux 服务器浏览器提供独立 Xvfb 显示，不改写 AstrBot 进程环境。"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys


class BrowserEnvironmentError(RuntimeError):
    """浏览器运行环境缺失，可直接向管理员说明修复方法。"""


class BrowserDisplay:
    def __init__(self) -> None:
        self.process = None

    async def prepare(self, mode: str) -> dict:
        if mode == "headless":
            return {"headless": True}
        if mode not in {"auto", "headed"}:
            raise BrowserEnvironmentError(
                "browser_mode 只能为 auto、headed 或 headless。"
            )
        if not sys.platform.startswith("linux") or os.environ.get("DISPLAY"):
            return {"headless": False}
        executable = shutil.which("Xvfb")
        if not executable:
            raise BrowserEnvironmentError(
                "服务器未安装 Xvfb。请在 AstrBot 运行环境内安装 xvfb（Debian/Ubuntu："
                "apt-get install -y xvfb），然后重载插件；也可将 browser_mode 改为 headless，"
                "但该模式可能被抖音拒绝验证。"
            )
        read_fd, write_fd = os.pipe()
        transport = None
        stream = os.fdopen(read_fd, "rb", buffering=0)
        try:
            reader = asyncio.StreamReader()
            transport, _ = await asyncio.get_running_loop().connect_read_pipe(
                lambda: asyncio.StreamReaderProtocol(reader), stream
            )
            try:
                # Xvfb 原子选择空闲显示号，避免并发实例争用固定 :99。
                self.process = await asyncio.create_subprocess_exec(
                    executable,
                    "-displayfd",
                    str(write_fd),
                    "-screen",
                    "0",
                    "1280x900x24",
                    "-nolisten",
                    "tcp",
                    pass_fds=(write_fd,),
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
            finally:
                os.close(write_fd)
                write_fd = -1
            number = (await asyncio.wait_for(reader.readline(), 10)).strip()
            if not number.isdigit() or self.process.returncode is not None:
                raise BrowserEnvironmentError(
                    "Xvfb 启动失败，请检查服务器显示依赖和运行权限。"
                )
            return {
                "headless": False,
                "env": {**os.environ, "DISPLAY": ":" + number.decode("ascii")},
            }
        except BaseException:
            await self.close()
            raise
        finally:
            if write_fd != -1:
                os.close(write_fd)
            if transport is not None:
                transport.close()
            else:
                stream.close()

    async def close(self) -> None:
        process, self.process = self.process, None
        if process is None:
            return
        if process.returncode is None:
            try:
                process.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(process.wait(), 5)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
        else:
            await process.wait()
