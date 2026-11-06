"""抖音 AI 插件：直播开播订阅检测 + 陪看评论 + 主动刷视频 + 互动（私信/评论回复）.

- 直播订阅：`订阅 <抖音号/链接>` / `取消订阅 <key>` / `订阅列表`
- 转发群：`直播转发 群号` / `取消转发 群号`
- 陪看评论：`dy评论 <视频链接>` 看视频后发一条像真人的短评，发完截图推群
- 主动刷视频：`dy主动 开/关` 定时刷一批视频（特别关心up优先）→ 打分 → 过线才评论 → 分享给主人
- 口味关键词：`dy口味 <词1,词2>` 打分时加权；特别关心：`dy特别关心 <抖音号>`
- 互动：`dy互动 开/关` 轮询私信新消息/我发的评论被回复，自动回或提醒主人
- 登录：`dy登录` / `dy_qr` / `dy_cookie` 三选一（服务器部署推荐 dy_qr 或 dy_cookie）
"""

from __future__ import annotations

import asyncio
import re
import time
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, StarTools
from astrbot.core.config.astrbot_config import AstrBotConfig

from .core.auth import is_logged_in, login_from_cookies, qr_login_bytes, wait_for_login
from .core.browser import DouyinBrowser
from .core.commenter import generate_comment
from .core.cooldown import CooldownStore
from .core.douyin import (
    DouyinVideoContent,
    cleanup,
    fetch_video,
    parse_aweme_id,
    send_comment,
)
from .core.interact import InteractState, get_bot_name, poll_comment_replies, poll_dm_new, send_dm
from .core.live_checker import DouyinLiveChecker, LiveStatus
from .core.proactive import SeenStore, fetch_feed_videos, fetch_user_videos
from .core.screenshot import compose_screenshot, compose_score_card
from .core.score import score_video
from .core.store import SubscriptionStore

PLUGIN_DIR_NAME = "astrbot_plugin_douyin_ai_bot"


def _cfg_get(cfg: AstrBotConfig | None, key: str, default: Any) -> Any:
    try:
        v = cfg.get(key) if cfg is not None else None
        if v is None:
            return default
        return v
    except Exception:
        return default


def _to_list(v: Any) -> list[str]:
    if isinstance(v, (list, tuple)):
        return [str(x).strip() for x in v if str(x).strip()]
    s = str(v or "")
    return [w.strip() for w in s.replace("，", ",").split(",") if w.strip()]


class DouyinAiBotPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.context = context
        self.cfg = config
        self._store: SubscriptionStore | None = None
        self._cooldown: CooldownStore | None = None
        self._checker: DouyinLiveChecker | None = None
        self._poll_task: asyncio.Task | None = None
        self._polling = False
        self._proactive_task: asyncio.Task | None = None
        self._proactive_running = False
        self._proactive_busy = False
        self._interact_task: asyncio.Task | None = None
        self._interact_running = False
        self._interact_busy = False
        self._seen: SeenStore | None = None
        self._interact_state: InteractState | None = None
        self._trigger_cd: dict[str, float] = {}
        self._platform_id: str = ""
        self._data_dir: Path | None = None
        self._comment_lock = asyncio.Lock()
        self._reload_runtime()

    # ================= 配置 =================

    def _c(self, key: str, default: Any = None) -> Any:
        return _cfg_get(self.cfg, key, default)

    def _reload_runtime(self) -> None:
        try:
            self._data_dir = Path(StarTools.get_data_dir(PLUGIN_DIR_NAME))
            self._data_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            logger.warning("[douyin_ai] 数据目录初始化失败: %s", e)
            self._data_dir = Path("data") / PLUGIN_DIR_NAME
            self._data_dir.mkdir(parents=True, exist_ok=True)
        self._store = SubscriptionStore(self._data_dir / "douyin_live.json")
        self._cooldown = CooldownStore(self._data_dir / "cooldown.json")
        self._seen = SeenStore(self._data_dir / "seen_videos.json")
        self._interact_state = InteractState(self._data_dir / "interact_state.json")
        self._checker = DouyinLiveChecker(
            ttwid=str(self._c("ttwid", "") or ""),
            browser_channel=str(self._c("browser_channel", "msedge") or ""),
        )

    def _profile_dir(self) -> Path:
        d = self._data_dir / "browser_profile"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _remember_platform(self, event: AstrMessageEvent):
        try:
            self._platform_id = str(event.get_platform_id() or "")
        except Exception:
            pass

    def _enabled(self) -> bool:
        return bool(self._c("enabled", True))

    def _trigger_words(self) -> list[str]:
        return _to_list(self._c("trigger_words", []))

    # ================= 生命周期 =================

    async def initialize(self):
        logger.info("[douyin_ai] 插件初始化")
        if self._store is None:
            self._reload_runtime()
        await self._sync_from_config()
        self._start_polling()
        if self._c("proactive_enabled", False):
            self._proactive_running = True
            self._start_proactive()
        if self._c("interact_enabled", False):
            self._interact_running = True
            self._start_interact()

    async def terminate(self):
        await self._stop_polling()
        await self._stop_proactive()
        await self._stop_interact()
        try:
            if self._checker:
                await self._checker.close()
        except Exception:
            pass
        try:
            inst = DouyinBrowser._instance
            if inst is not None:
                await inst.close()
        except Exception:
            pass
        logger.info("[douyin_ai] 插件已终止")

    async def _sync_from_config(self) -> None:
        """把配置面板的订阅抖音号 list / 转发群 list 同步进 store，自动解析。"""
        if self._store is None:
            return
        # 订阅抖音号（支持抖音号 / 链接 / 短链 / 整段分享文本）
        for item in _to_list(self._c("sub_short_ids", [])):
            short = re.search(r"https?://v\.douyin\.com/[\w-]+", item)
            if short:
                room_id = await DouyinLiveChecker.resolve_short_link(short.group(0))
                if room_id:
                    await self._store.upsert_sub(room_id, nickname=item)
                continue
            room_id = DouyinLiveChecker.extract_room_id(item)
            if room_id:
                await self._store.upsert_sub(room_id)
                continue
            if re.fullmatch(r"[A-Za-z0-9_.]+", item) and not item.isdigit():
                # 抖音号：无头浏览器锁定直播间
                rid, nick = await DouyinLiveChecker.lock_room_by_short_id(item)
                if rid:
                    await self._store.upsert_sub(rid, nickname=nick or item)
        # 转发群
        for gid in _to_list(self._c("forward_groups", [])):
            await self._store.add_group(gid)

    def _start_polling(self):
        if self._poll_task and not self._poll_task.done():
            return
        self._poll_task = asyncio.create_task(self._poll_loop())

    def _start_proactive(self):
        if self._proactive_task and not self._proactive_task.done():
            return
        self._proactive_task = asyncio.create_task(self._proactive_loop())

    async def _stop_proactive(self):
        self._proactive_running = False
        if self._proactive_task:
            self._proactive_task.cancel()
            try:
                await self._proactive_task
            except (asyncio.CancelledError, Exception):
                pass
            self._proactive_task = None

    def _start_interact(self):
        if self._interact_task and not self._interact_task.done():
            return
        self._interact_task = asyncio.create_task(self._interact_loop())

    async def _stop_interact(self):
        self._interact_running = False
        if self._interact_task:
            self._interact_task.cancel()
            try:
                await self._interact_task
            except (asyncio.CancelledError, Exception):
                pass
            self._interact_task = None

    async def _stop_polling(self):
        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except (asyncio.CancelledError, Exception):
                pass
            self._poll_task = None

    # ================= 直播轮询 =================

    async def _poll_loop(self):
        while True:
            try:
                await asyncio.sleep(max(60, int(self._c("poll_interval", 300) or 300)))
                if not self._enabled() or self._polling:
                    continue
                self._polling = True
                await self._poll_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.exception("[douyin_ai] 轮询异常: %s", e)
            finally:
                self._polling = False

    async def _poll_once(self):
        subs = self._store.all_subs()
        groups = self._store.all_groups()
        if not subs:
            return
        for key, sub in subs:
            room_id = sub.get("room_id") or ""
            if not room_id:
                room_id = DouyinLiveChecker.extract_room_id(key)
                if not room_id:
                    # 抖音号：补一次锁定
                    if re.fullmatch(r"[A-Za-z0-9_.]+", key) and not key.isdigit():
                        room_id, _ = await DouyinLiveChecker.lock_room_by_short_id(key)
                if room_id:
                    await self._store.set_room_id(key, room_id)
            if not room_id:
                continue
            try:
                status = await self._checker.check(room_id, expected_nickname=str(sub.get("nickname", "")))
            except Exception as e:
                logger.warning("[douyin_ai] 检测 %s 失败: %s", room_id, e)
                continue
            changed = await self._store.set_live_state(key, status.live)
            if status.live and changed:
                logger.info("[douyin_ai] %s 开播: %s", key, status.title[:30])
                if groups:
                    await self._broadcast_live(groups, status)

    async def _broadcast_live(self, groups: list[str], status: LiveStatus):
        for gid in groups:
            try:
                await self._send_to_group(gid, await self._build_live_result(status))
            except Exception as e:
                logger.warning("[douyin_ai] 推送到群 %s 失败: %s", gid, e)

    # ================= 发送工具 =================

    async def _send_to_group(self, gid: str, chain: MessageChain) -> None:
        pid = self._platform_id or "default"
        await self.context.send_message(f"{pid}:GroupMessage:{gid}", chain)

    async def _build_live_result(self, status: LiveStatus) -> MessageChain:
        reply = str(self._c("reply_text", "") or "").strip()
        text = (
            f"{status.nickname or '主播'} 开播啦！\n"
            f"标题：{status.title or '(未取到)'}\n"
            f"链接：{status.room_url}"
        )
        if reply:
            text = f"{reply}\n{text}"
        chain = MessageChain()
        if status.cover:
            try:
                chain.image(fix_type="online", url=status.cover)
            except Exception:
                pass
        chain.message(text)
        return chain

    async def _build_not_live(self, nickname: str) -> MessageChain:
        txt = str(self._c("not_live_text", "") or "").strip()
        if not txt:
            txt = "还没开播"
        return MessageChain().message(txt)

    # ================= 直播命令 =================

    @filter.command("订阅", alias={"抖音订阅", "dy订阅", "dy订"})
    async def cmd_sub(self, event: AstrMessageEvent, raw: str = ""):
        """订阅主播直播：填抖音号 / 直播间号 / live.douyin.com 链接 / 手机分享整段文本，开播时自动推转发群。"""
        self._remember_platform(event)
        msg = (raw or "").strip() or (event.get_message_str() or "").strip()
        if not msg:
            yield event.plain_result("用法：订阅 <抖音号 / 主页链接 / 直播间链接>")
            return
        # 短链 / 直播链接 → 解析房间号
        m = re.search(r"(https?://v\.douyin\.com/[\w-]+)", msg)
        if m:
            room_id = await DouyinLiveChecker.resolve_short_link(m.group(1))
            if room_id:
                await self._store.upsert_sub(room_id, nickname=msg)
                yield event.plain_result(f"已订阅：{room_id}")
                return
        room_id = DouyinLiveChecker.extract_room_id(msg)
        if room_id:
            await self._store.upsert_sub(room_id, nickname=msg)
            yield event.plain_result(f"已订阅：{room_id}")
            return
        if re.fullmatch(r"[A-Za-z0-9_.]+", msg) and not msg.isdigit():
            rid, nick = await DouyinLiveChecker.lock_room_by_short_id(msg)
            if rid:
                await self._store.upsert_sub(rid, nickname=nick or msg)
                yield event.plain_result(f"已订阅：{nick or msg}（{rid}）")
            else:
                yield event.plain_result("这个抖音号解析不到直播间，确认下抖音号对不对")
            return
        yield event.plain_result("没认出这是什么（要抖音号、主页链接或直播间链接）")

    @filter.command("取消订阅", alias={"dy取消订阅"})
    async def cmd_unsub(self, event: AstrMessageEvent, raw: str = ""):
        """取消订阅某个主播的直播。"""
        msg = (raw or "").strip() or (event.get_message_str() or "").strip()
        if not msg:
            yield event.plain_result("用法：取消订阅 <抖音号或房间号>")
            return
        removed = await self._store.remove_sub(msg)
        yield event.plain_result("已取消：" + msg if removed else "订阅列表里没有这个")

    @filter.command("订阅列表", alias={"dy订阅列表"})
    async def cmd_sublist(self, event: AstrMessageEvent):
        """查看当前全部直播订阅及转发群。"""
        subs = self._store.all_subs()
        if not subs:
            yield event.plain_result("还没有订阅")
            return
        lines = [f"{key}（{sub.get('nickname') or '未填昵称'}）" for key, sub in subs]
        groups = self._store.all_groups()
        yield event.plain_result("\n".join(lines) + f"\n转发群：{'、'.join(groups) if groups else '未配置'}")

    @filter.command("直播转发", alias={"dy转发"})
    async def cmd_fwd(self, event: AstrMessageEvent, raw: str = ""):
        """开播推送转发群：填 QQ 群号，主播开播时把封面+链接+配文推到该群（过线视频/互动提醒也推这里）。"""
        msg = (raw or "").strip() or (event.get_message_str() or "").strip()
        if not msg or not msg.isdigit():
            yield event.plain_result("用法：直播转发 <群号>")
            return
        await self._store.add_group(msg)
        yield event.plain_result(f"已加转发群：{msg}")

    @filter.command("取消转发", alias={"dy取消转发"})
    async def cmd_unfwd(self, event: AstrMessageEvent, raw: str = ""):
        """移除一个开播推送转发群。"""
        msg = (raw or "").strip() or (event.get_message_str() or "").strip()
        if not msg or not msg.isdigit():
            yield event.plain_result("用法：取消转发 <群号>")
            return
        removed = await self._store.remove_group(msg)
        yield event.plain_result("已删：" + msg if removed else "转发列表里没有这个群")

    # ================= 陪看评论 =================

    @filter.command("dy评论", alias={"dy评", "抖音评论", "抖音陪看", "抖音评", "陪看"})
    async def cmd_dy_comment(self, event: AstrMessageEvent, raw: str = ""):
        """看抖音视频并发一条像真人的短评，发完截图推群。"""
        self._remember_platform(event)
        if not self._c("comment_enabled", True):
            yield event.plain_result("陪看评论已停用")
            return
        msg = (raw or "").strip() or (event.get_message_str() or "").strip()
        aweme_id = parse_aweme_id(msg)
        if not aweme_id:
            yield event.plain_result("用法：dy评论 <视频链接 或 分享文本>\n例：dy评论 https://v.douyin.com/xxxx/")
            return

        # 冷却检查
        if self._cooldown:
            left = self._cooldown.in_video_cooldown(aweme_id, float(self._c("cooldown_hours", 0) or 0))
            if left > 0:
                yield event.plain_result(f"这个视频 {left / 3600:.1f} 小时后再评吧（同视频冷却）")
                return
            gleft = self._cooldown.in_global_cooldown(float(self._c("global_cooldown_minutes", 0) or 0))
            if gleft > 0:
                yield event.plain_result(f"刚评过，{gleft / 60:.0f} 分钟后再来（全局间隔）")
                return

        if self._comment_lock.locked():
            yield event.plain_result("正在看上一个视频，等一下再发")
            return

        async with self._comment_lock:
            yield event.plain_result(f"正在看这个视频，稍等…（下载+抽帧+理解，可能要一两分钟）")
            try:
                vc = await fetch_video(aweme_id, int(self._c("frame_count", 6) or 6))
            except Exception as e:
                yield event.plain_result(f"视频信息拿不到：{e}")
                return

            comment_text = ""
            try:
                comment_text = await generate_comment(
                    vc, str(self._c("comment_style", "") or ""), self.context, self.cfg
                )
            finally:
                frames_for_shot = list(vc.frames)
                cover, title = vc.cover, vc.title
                owner, stat = vc.owner, vc.stat
                top_cmts = list(vc.top_comments)

            if not comment_text:
                cleanup(vc)
                yield event.plain_result("评论没生成出来（视觉模型没返回？），这次不发了")
                return

            # 发评论（需要登录）
            sent = False
            err = ""
            try:
                if not await is_logged_in():
                    err = "还没登录（扫码一次就行），先 `dy登录`"
                else:
                    await send_comment(aweme_id, comment_text)
                    sent = True
            except Exception as e:
                err = f"发评论失败: {e}"

            if sent and self._cooldown:
                self._cooldown.mark(aweme_id)

            # 截图
            shot = await compose_screenshot(
                cover_url=cover,
                frames=frames_for_shot,
                title=title or "（标题没抓到）",
                owner=owner,
                bvid=aweme_id,
                comment=comment_text,
                stat=stat,
                top_comments=top_cmts,
                link=vc.page_url,
                meta_prefix="作者",
            )
            status = "已发出评论" if sent else f"评论未发出（{err}）"

            # 截图推转发群（和主动刷过线同一个出口）
            chain = MessageChain()
            if shot and Path(shot).exists():
                chain.image(fix_type="local", url=shot)
            chain.message(f"《{title}》\n{status}\n评论：{comment_text}\n{vc.page_url}")
            pushed = await self._share_to_master(chain, aweme_id)

            tail = f"截图已推送到群：{'、'.join(pushed)}" if pushed else "没配推送群（直播转发 <群号>）"
            reply_chain = MessageChain()
            reply_chain.message(f"《{title}》\n{status}\n评论：{comment_text}\n{tail}")
            yield reply_chain

            cleanup(vc)

    # ================= 登录 =================

    def _gui_available(self) -> bool:
        """当前环境能否弹出有界面的浏览器（服务器/无桌面=False）。"""
        import os
        import sys
        if sys.platform == "win32":
            return os.environ.get("SESSIONNAME", "Console") == "Console"
        # linux 服务器一般没桌面
        if os.name == "posix":
            return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
        return True

    @filter.command("dy登录", alias={"抖音登录", "dy扫码"})
    async def cmd_dy_login(self, event: AstrMessageEvent):
        """抖音登录：本机有桌面就拉浏览器扫码；服务器/无界面环境自动改推二维码图给你手机扫。"""
        self._remember_platform(event)
        if not self._gui_available():
            yield event.plain_result("这边没有浏览器窗口（服务器环境），自动改推二维码，手机抖音扫…")
            qr, ok = await qr_login_bytes(self._profile_dir(), channel=str(self._c("browser_channel", "msedge") or ""), timeout=150)
            if ok:
                yield event.plain_result("扫码成功，登录态已落盘")
                return
            if not qr:
                yield event.plain_result("二维码没截到，换 dy_cookie 贴浏览器 Cookie 也行")
                return
            shot = self._data_dir / "dy_login_qr.png"
            shot.write_bytes(qr)
            yield MessageChain().image(fix_type="local", url=str(shot)).message("扫这个码（150 秒有效）")
            return
        yield event.plain_result("拉起浏览器了，用手机抖音扫一下码（180 秒超时）…")
        ok = await wait_for_login(self._profile_dir(), channel=str(self._c("browser_channel", "msedge") or ""), timeout=180)
        yield event.plain_result("登录成功，cookie 已落盘，之后自动带登录态" if ok else "登录超时，再来一次（dy登录）")

    @filter.command("dy_cookie", alias={"抖音cookie", "dycookie", "dy导入cookie"})
    async def cmd_dy_cookie(self, event: AstrMessageEvent, raw: str = ""):
        """方式一：贴浏览器里的整段 Cookie（F12-应用程序-Cookie 全选复制）或发文件，直接导入。"""
        self._remember_platform(event)
        cookie_str = (raw or "").strip()
        # 用户也可能把 cookie 存成文件发过来（File 段路径在 file_ 属性）
        try:
            for seg in event.message:
                fp = getattr(seg, "file_", None)
                if fp and Path(fp).exists():
                    content = Path(fp).read_text(encoding="utf-8", errors="ignore")
                    if content.strip():
                        cookie_str = content.strip()
        except Exception:
            pass
        cookie_str = (cookie_str or "").strip()
        if not cookie_str:
            yield event.plain_result("用法：dy_cookie <整段Cookie 或发个txt文件>\n（浏览器登录抖音后 F12-应用程序-Cookie，全选复制值粘贴过来）")
            return
        has_sid, n = await login_from_cookies(cookie_str, self._profile_dir(), channel=str(self._c("browser_channel", "msedge") or ""))
        if n == 0:
            yield event.plain_result("没解析出任何 cookie，格式不对（要 a=1; b=2 这种整段）")
            return
        yield event.plain_result(
            f"导入 {n} 条 cookie，登录态：{'有效（sessionid 有了）' if has_sid else '没看到 sessionid（可能没登录或过期），评论/检测可能不生效，重新复制一下'}"
        )

    @filter.command("dy_qr", alias={"抖音二维码", "dy二维码"})
    async def cmd_dy_qr(self, event: AstrMessageEvent):
        """方式二：无头浏览器抓登录二维码推过来，手机抖音扫（服务器不用开浏览器窗口）。"""
        self._remember_platform(event)
        yield event.plain_result("正在生成登录二维码，稍等…")
        qr, ok = await qr_login_bytes(self._profile_dir(), channel=str(self._c("browser_channel", "msedge") or ""), timeout=150)
        if ok:
            yield event.plain_result("扫码成功，登录态已落盘")
            return
        if not qr:
            yield event.plain_result("二维码没截到，换个方式：dy登录（本机浏览器）或 dy_cookie 贴 cookie")
            return
        try:
            shot = self._data_dir / "dy_login_qr.png"
            shot.write_bytes(qr)
            yield MessageChain().image(fix_type="local", url=str(shot)).message("用抖音扫这个码（150 秒有效），扫完自动落盘。看不清就 dy登录 走本机浏览器")
        except Exception as e:
            yield event.plain_result(f"二维码发送失败：{e}，换 dy登录 吧")

    @filter.command("dy状态", alias={"抖音状态", "dy状态"})
    async def cmd_dy_status(self, event: AstrMessageEvent):
        """查看当前状态：订阅数 / 转发群 / 登录态 / 主动刷 / 互动开关。"""
        subs = self._store.all_subs()
        lines = [f"订阅 {len(subs)} 个，转发群 {'、'.join(self._store.all_groups()) or '未配置'}"]
        try:
            if await is_logged_in():
                lines.append("登录态：有效")
            else:
                lines.append("登录态：未登录（dy登录 / dy_qr / dy_cookie 三选一）")
        except Exception as e:
            lines.append(f"登录态检查失败：{e}")
        yield event.plain_result("\n".join(lines))

    # ================= 主动刷视频 =================

    def _taste_words(self) -> list[str]:
        return _to_list(self._c("taste_words", []))

    def _master_user_id(self) -> str:
        return str(self._c("master_user_id", "") or "").strip()

    async def _owner_ids(self) -> list[str]:
        out = []
        for key, sub in self._store.all_subs():
            if not key.isdigit() and re.fullmatch(r"[A-Za-z0-9_.]+", key):
                out.append(key)
        out.extend(self._store.all_special())
        return list(dict.fromkeys(out))

    async def _proactive_loop(self):
        while True:
            try:
                await asyncio.sleep(max(300, int(self._c("proactive_interval", 900) or 900)))
                if not self._proactive_running or self._proactive_busy or not self._enabled():
                    continue
                self._proactive_busy = True
                await self._proactive_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.exception("[douyin_ai] 主动刷异常: %s", e)
            finally:
                self._proactive_busy = False

    async def _proactive_once(self):
        from .core.browser import DouyinBrowser

        n = max(1, min(10, int(self._c("proactive_batch", 3) or 3)))
        min_score = float(self._c("score_threshold", 7) or 7)
        logger.info("[douyin_ai] 主动刷开始：取 %s 个，过线 %.0f 分才评", n, min_score)

        # 收集候选：特别关心 up 优先，其次订阅的 up，最后推荐流
        candidates: list[tuple[str, bool]] = []  # (aweme_id, is_special)
        seen_ids: set[str] = set()
        specials = self._store.all_special()
        browser = DouyinBrowser.get(profile_dir=self._profile_dir(), headless=True,
                                    channel=str(self._c("browser_channel", "msedge") or ""))
        try:
            for sid in specials:
                for aid in await fetch_user_videos(sid, 3, browser):
                    if aid not in seen_ids:
                        seen_ids.add(aid)
                        candidates.append((aid, True))
            owner_ids = [k for k in await self._owner_ids() if k not in specials]
            for sid in owner_ids[:5]:
                for aid in await fetch_user_videos(sid, 3, browser):
                    if aid not in seen_ids and len(candidates) < n * 3:
                        seen_ids.add(aid)
                        candidates.append((aid, False))
            if len(candidates) < n:
                for aid in await fetch_feed_videos(n * 2, browser):
                    if aid not in seen_ids and len(candidates) < n * 3:
                        seen_ids.add(aid)
                        candidates.append((aid, False))
        except Exception as e:
            logger.warning("[douyin_ai] 收集候选失败: %s", e)

        picked = 0
        round_entries: list[dict] = []  # 本轮打分记录，用于汇总卡片
        for aweme_id, is_special in candidates:
            if picked >= n:
                break
            if self._seen and (self._seen.is_seen(aweme_id) or self._seen.is_commented(aweme_id)):
                continue
            self._seen.mark_seen(aweme_id)
            picked += 1
            logger.info("[douyin_ai] 主动看视频 %s（特别关心=%s）", aweme_id, is_special)
            try:
                vc = await fetch_video(aweme_id, int(self._c("frame_count", 6) or 6))
            except Exception as e:
                logger.warning("[douyin_ai] 拉取 %s 失败: %s", aweme_id, e)
                continue
            score, reason = 0, ""
            try:
                score, reason = await score_video(vc, self._taste_words(), is_special, self.context, self.cfg)
            except Exception as e:
                logger.warning("[douyin_ai] 评分 %s 失败: %s", aweme_id, e)
            logger.info("[douyin_ai] %s 打分 %.0f（%s）", vc.title[:20], score, reason)
            round_entries.append({
                "aweme_id": aweme_id, "title": vc.title, "owner": vc.owner,
                "special": is_special, "score": score, "reason": reason,
                "liked": False, "link": vc.page_url, "threshold": min_score,
            })

            if score < min_score:
                from .core.douyin import cleanup
                cleanup(vc)
                continue

            # 过线：评论 + 分享
            comment_text = ""
            if self._c("proactive_comment", True):
                try:
                    comment_text = await generate_comment(vc, str(self._c("comment_style", "") or ""), self.context, self.cfg)
                except Exception:
                    pass
            sent = False
            if comment_text:
                try:
                    if await is_logged_in():
                        from .core.douyin import send_comment
                        gleft = self._cooldown.in_global_cooldown(float(self._c("global_cooldown_minutes", 0) or 0))
                        if gleft <= 0:
                            await send_comment(aweme_id, comment_text)
                            sent = True
                            self._cooldown.mark(aweme_id)
                            self._seen.mark_commented(aweme_id, score, comment_text)
                except Exception as e:
                    logger.warning("[douyin_ai] 主动评论失败: %s", e)

            # 截图 + 分享主人
            shot = await compose_screenshot(
                cover_url=vc.cover, frames=list(vc.frames), title=vc.title or "（标题没抓到）",
                owner=vc.owner, bvid=aweme_id, comment=comment_text, stat=vc.stat,
                top_comments=list(vc.top_comments), link=vc.page_url, meta_prefix="作者",
            )
            chain = MessageChain()
            if shot and Path(shot).exists():
                chain.image(fix_type="local", url=shot)
            chain.message(
                f"主动刷到《{vc.title or '视频'}》\n"
                f"打分 {score:.0f}/10（{reason}）\n"
                + (f"评论已发：{comment_text}\n" if sent else "没评论\n")
                + vc.page_url
            )
            await self._share_to_master(chain, aweme_id)
            from .core.douyin import cleanup
            cleanup(vc)

        # 本轮打分汇总：每轮刷完都推一张完整评分统计（过线的也在卡里标出来）
        if round_entries:
            try:
                shot = await compose_score_card(round_entries)
                if shot and Path(shot).exists():
                    top = max(e.get("score", 0) for e in round_entries)
                    passed_n = sum(1 for e in round_entries if e.get("score", 0) >= min_score)
                    chain = MessageChain()
                    chain.image(fix_type="local", url=shot)
                    tail = f"，{passed_n} 个过线" if passed_n else f"，没到 {min_score:.0f} 分线"
                    chain.message(f"本轮主动刷 {len(round_entries)} 个视频，最高 {top:.0f} 分{tail}")
                    await self._share_to_master(chain, "score_summary")
            except Exception as e:
                logger.warning("[douyin_ai] 本轮打分汇总推送失败: %s", e)

    async def _share_to_master(self, chain: MessageChain, aweme_id: str) -> list[str]:
        """推直播转发群（forward_groups）；没配再退回 notify_groups。返回推成功的群号列表。"""
        groups: list[str] = []
        for g in _to_list(self._c("forward_groups", [])):
            g = g.strip()
            if g.isdigit() and g not in groups:
                groups.append(g)
        for g in _to_list(self._c("notify_groups", [])):
            g = g.strip()
            if g.isdigit() and g not in groups:
                groups.append(g)
        pushed: list[str] = []
        for gid in groups:
            try:
                await self._send_to_group(gid, chain)
                pushed.append(gid)
            except Exception as e:
                logger.warning("[douyin_ai] 群 %s 推送失败: %s", gid, e)
        return pushed

    # ================= 互动轮询 =================

    async def _interact_loop(self):
        while True:
            try:
                await asyncio.sleep(max(120, int(self._c("interact_interval", 300) or 300)))
                if not self._interact_running or self._interact_busy or not self._enabled():
                    continue
                self._interact_busy = True
                await self._interact_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.exception("[douyin_ai] 互动轮询异常: %s", e)
            finally:
                self._interact_busy = False

    async def _interact_once(self):
        from .core.browser import DouyinBrowser

        try:
            browser = DouyinBrowser.get(profile_dir=self._profile_dir(), headless=True,
                                        channel=str(self._c("browser_channel", "msedge") or ""))
        except Exception as e:
            logger.warning("[douyin_ai] 互动开浏览器失败: %s", e)
            return
        if not await is_logged_in():
            logger.info("[douyin_ai] 未登录，跳过互动轮询")
            return

        # 0) 补自己昵称
        if not self._interact_state.data.get("bot_name"):
            try:
                name = await get_bot_name(browser)
                if name:
                    self._interact_state.data["bot_name"] = name
                    self._interact_state.save()
            except Exception:
                pass

        # 1) 私信
        if self._c("dm_notify", True):
            try:
                news = await poll_dm_new(browser, self._interact_state)
                for m in news:
                    chain = MessageChain().message(
                        f"私信提醒：{m['conv_name']}\n{m['text']}\n（dy互动 私信回复 <内容> 可回 ta）"
                    )
                    await self._share_to_master(chain, "dm")
            except Exception as e:
                logger.warning("[douyin_ai] 私信轮询失败: %s", e)

        # 2) 评论被回复
        if self._c("reply_notify", True) and self._seen:
            try:
                recent = [k for k, _ in self._seen.recent_commented(5)]
                replies = await poll_comment_replies(browser, recent, self._interact_state)
                for r in replies:
                    chain = MessageChain().message(
                        f"有人回复你的评论啦：{r['replier']} 说「{r['text']}」\n"
                        f"视频：{r['aweme_id']}"
                    )
                    await self._share_to_master(chain, "reply")
            except Exception as e:
                logger.warning("[douyin_ai] 评论回复轮询失败: %s", e)

    # ================= 触发词监听 =================

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_trigger(self, event: AstrMessageEvent):
        """群里说触发词时，立即检查所有订阅，把正在播的发回去。"""
        self._remember_platform(event)
        if not self._enabled():
            return
        words = self._trigger_words()
        if not words:
            return
        msg = (event.get_message_str() or "").strip()
        if not msg:
            return
        if not any(w and w in msg for w in words):
            return
        # 过滤掉命令本身
        for w in ("订阅", "取消", "直播转发", "dy", "抖音", "陪看"):
            if msg.startswith(w):
                return
        now = time.time()
        key = str(event.get_group_id() or "private")
        if now - self._trigger_cd.get(key, 0) < 60:
            return
        self._trigger_cd[key] = now
        subs = self._store.all_subs()
        if not subs:
            yield event.plain_result("还没订阅任何人呢（订阅 <抖音号>）")
            return
        live_ones = []
        for key2, sub in subs:
            room_id = sub.get("room_id") or DouyinLiveChecker.extract_room_id(key2)
            if not room_id and re.fullmatch(r"[A-Za-z0-9_.]+", key2) and not key2.isdigit():
                room_id, _ = await DouyinLiveChecker.lock_room_by_short_id(key2)
            if not room_id:
                continue
            try:
                status = await self._checker.check(room_id, expected_nickname=str(sub.get("nickname", "")))
            except Exception:
                continue
            await self._store.set_live_state(key2, status.live)
            if status.live:
                live_ones.append(status)
        if not live_ones:
            yield event.plain_result(str(self._c("not_live_text", "") or "都没在播呢（）"))
            return
        for status in live_ones:
            yield await self._build_live_result(status)

    @filter.command("添加触发词", alias={"dy添加触发词"})
    async def cmd_add_trigger(self, event: AstrMessageEvent, word: str = ""):
        """添加触发词（一个一个加，别用逗号）：群里出现这个词就即时检查订阅主播是否在播。"""
        self._remember_platform(event)
        word = (word or "").strip()
        if not word:
            yield event.plain_result("用法：添加触发词 <词>")
            return
        words = self._trigger_words()
        if word in words:
            yield event.plain_result(f"触发词「{word}」已存在")
            return
        words.append(word)
        try:
            self.cfg["trigger_words"] = words
            self.cfg.save_config()
        except Exception:
            pass
        yield event.plain_result(f"已添加触发词「{word}」，当前共 {len(words)} 个")

    @filter.command("删除触发词", alias={"dy删除触发词"})
    async def cmd_del_trigger(self, event: AstrMessageEvent, word: str = ""):
        """删除一个触发词。"""
        self._remember_platform(event)
        word = (word or "").strip()
        if not word:
            yield event.plain_result("用法：删除触发词 <词>")
            return
        words = self._trigger_words()
        if word not in words:
            yield event.plain_result(f"触发词「{word}」不存在")
            return
        words.remove(word)
        try:
            self.cfg["trigger_words"] = words
            self.cfg.save_config()
        except Exception:
            pass
        yield event.plain_result(f"已删除触发词「{word}」，剩余：{'、'.join(words) if words else '无'}")

    @filter.command("触发词列表", alias={"dy触发词"})
    async def cmd_trigger_list(self, event: AstrMessageEvent):
        """查看当前全部触发词。"""
        words = self._trigger_words()
        yield event.plain_result("触发词：" + "、".join(words) if words else "还没配触发词（添加触发词 <词>）")

    # ================= 主动刷视频命令 =================

    @filter.command("dy主动", alias={"抖音主动", "主动刷", "dy刷视频"})
    async def cmd_proactive(self, event: AstrMessageEvent, raw: str = ""):
        """主动刷视频：dy主动 开/关 控制定时刷；dy主动 刷 立即跑一轮（打分过线才评论，截图推转发群）。"""
        self._remember_platform(event)
        raw = (raw or "").strip()
        if raw in ("开", "启动", "on"):
            self._c_set("proactive_enabled", True)
            self._proactive_running = True
            self._start_proactive()
            yield event.plain_result(f"主动刷已开启：每 {int(self._c('proactive_interval', 900)) // 60} 分钟刷 {self._c('proactive_batch', 3)} 个，打分 ≥{self._c('score_threshold', 7)} 才评论，过线推给你")
            return
        if raw in ("关", "关闭", "off"):
            self._c_set("proactive_enabled", False)
            await self._stop_proactive()
            yield event.plain_result("主动刷已关闭")
            return
        if raw in ("刷", "现在", "now"):
            if self._proactive_busy:
                yield event.plain_result("正在刷，等一下")
                return
            if not self._c("comment_enabled", True):
                yield event.plain_result("陪看评论已停用")
                return
            yield event.plain_result("开刷了，稍等（下载+打分+评论，几分钟）…")
            self._proactive_busy = True
            try:
                await self._proactive_once()
                yield event.plain_result("刷完一轮了，过线的都推过了（没推就是都没过线）")
            finally:
                self._proactive_busy = False
            return
        yield event.plain_result("用法：dy主动 开 / 关 / 刷（刷=立即跑一轮）")

    def _c_set(self, key: str, value: Any):
        try:
            self.cfg[key] = value
            self.cfg.save_config()
        except Exception:
            pass

    @filter.command("dy口味", alias={"抖音口味", "口味"})
    async def cmd_taste(self, event: AstrMessageEvent, raw: str = ""):
        """设置打分口味关键词（逗号分隔，如 dy口味 美食,探店）：主动刷视频时内容匹配口味会加分。"""
        self._remember_platform(event)
        raw = (raw or "").strip()
        if not raw:
            cur = self._taste_words()
            yield event.plain_result("当前口味：" + ("、".join(cur) if cur else "没设（打分走通用标准）"))
            return
        words = [w.strip() for w in re.split(r"[,，、\s]+", raw) if w.strip()]
        self._c_set("taste_words", words)
        yield event.plain_result(f"口味已更新：{'、'.join(words)}（打分时匹配这些主题的会加分）")

    @filter.command("dy特别关心", alias={"抖音特别关心", "特别关心"})
    async def cmd_special(self, event: AstrMessageEvent, raw: str = ""):
        """管理特别关心的 up：dy特别关心 <抖音号> 添加（主动刷优先看 ta，打分 +1）；dy特别关心 删 <抖音号> 移除。"""
        self._remember_platform(event)
        raw = (raw or "").strip()
        if not raw:
            sp = self._store.all_special()
            yield event.plain_result("特别关心：" + ("、".join(sp) if sp else "没设") + "\n（用法：dy特别关心 <抖音号> / 删 <抖音号>）")
            return
        if raw.startswith("删"):
            target = raw[1:].strip()
            ok = await self._store.remove_special(target)
            yield event.plain_result(f"已移除 {target}" if ok else "特别关心里没有这个")
            return
        if not re.fullmatch(r"[A-Za-z0-9_.]+", raw) or raw.isdigit():
            yield event.plain_result("要填抖音号（如 Lianmin726），别填名字")
            return
        await self._store.add_special(raw)
        yield event.plain_result(f"已特别关心 {raw}：主动刷时优先看 ta 的新视频，打分 +1 保底，过线推给你")

    @filter.command("dy主人", alias={"抖音主人", "设主人"})
    async def cmd_master(self, event: AstrMessageEvent, raw: str = ""):
        """设置主人 QQ 号（备用标识；推送统一走转发群，不私聊）。"""
        self._remember_platform(event)
        raw = (raw or "").strip()
        if not raw:
            yield event.plain_result("当前主人 QQ：" + (self._master_user_id() or "没设") + "\n（用法：dy主人 <QQ号> 推送都走转发群）")
            return
        if not raw.isdigit():
            yield event.plain_result("要填 QQ 号数字")
            return
        self._c_set("master_user_id", raw)
        yield event.plain_result(f"主人设为 {raw}")

    # ================= 互动命令 =================

    @filter.command("dy互动", alias={"抖音互动", "互动开关"})
    async def cmd_interact(self, event: AstrMessageEvent, raw: str = ""):
        """互动提醒开关：dy互动 开/关 控制私信+评论回复轮询（推转发群）；dy互动 私信回复 <内容> 用抖音身份回最近私信。"""
        self._remember_platform(event)
        raw = (raw or "").strip()
        if raw in ("开", "启动", "on"):
            self._c_set("interact_enabled", True)
            self._interact_running = True
            self._start_interact()
            yield event.plain_result(f"互动已开启：每 {int(self._c('interact_interval', 300)) // 60} 分钟轮询一次，新私信/评论被回复都推给主人")
            return
        if raw in ("关", "关闭", "off"):
            self._c_set("interact_enabled", False)
            await self._stop_interact()
            yield event.plain_result("互动已关闭")
            return
        m = re.match(r"^私信回复[:：]?\s*(.+)$", raw)
        if m:
            text = m.group(1).strip()
            if not text:
                yield event.plain_result("要回什么内容？")
                return
            # 找到最近一条私信会话
            recent = sorted(self._interact_state.data.get("seen_conv_ts", {}).items(), key=lambda x: x[1], reverse=True)
            if not recent:
                yield event.plain_result("没看到私信会话记录，等互动轮过一次再用")
                return
            conv_name = recent[0][0]
            ok = await self._send_dm_to(conv_name, text)
            yield event.plain_result(f"已回 {conv_name}：{text}" if ok else "没发出去（可能没登录或会话找不到了）")
            return
        yield event.plain_result("用法：dy互动 开 / 关 / 私信回复 <内容>")

    async def _send_dm_to(self, conv_name: str, text: str) -> bool:
        from .core.browser import DouyinBrowser
        try:
            browser = DouyinBrowser.get(profile_dir=self._profile_dir(), headless=True,
                                        channel=str(self._c("browser_channel", "msedge") or ""))
            return await send_dm(browser, conv_name, text)
        except Exception as e:
            logger.warning("[douyin_ai] 发私信异常: %s", e)
            return False

    @filter.command("dy刷状态", alias={"刷状态"})
    async def cmd_proactive_status(self, event: AstrMessageEvent):
        """查看主动刷/互动/口味/特别关心/主人 配置一览。"""
        lines = [
            f"主动刷：{'开' if self._proactive_running else '关'}（间隔 {int(self._c('proactive_interval', 900)) // 60} 分钟，每轮 {self._c('proactive_batch', 3)} 个，过线 {self._c('score_threshold', 7)} 分）",
            f"口味：{'、'.join(self._taste_words()) or '无'}",
            f"特别关心：{'、'.join(self._store.all_special()) or '无'}",
            f"主人 QQ：{self._master_user_id() or '未设'}",
            f"互动：{'开' if self._interact_running else '关'}（间隔 {int(self._c('interact_interval', 300)) // 60} 分钟）",
        ]
        if self._seen:
            lines.append(f"历史：看过 {len(self._seen._data.get('seen', {}))} 个，评过 {len(self._seen._data.get('commented', {}))} 个")
        yield event.plain_result("\n".join(lines))
