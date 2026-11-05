"""抖音 AI 插件：直播开播订阅检测 + 陪看评论（发视频链接→看视频→发像真人的短评）.

- 直播订阅：`订阅 <抖音号/链接>` / `取消订阅 <key>` / `订阅列表`
- 转发群：`直播转发 群号` / `取消转发 群号`
- 每 N 秒轮询全部订阅，开播时推「封面+链接+配文」到转发群
- 群里说触发词时即时检查并把正在播的主播发回去
- 陪看评论：`dy评论 <视频链接>` 看视频后发一条像真人的短评，发完截图推群
- 登录三选一：`dy登录`（本机浏览器扫码）/ `dy_qr`（推二维码图，手机扫）/ `dy_cookie`（贴浏览器整段 Cookie 导入）
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
from .core.live_checker import DouyinLiveChecker, LiveStatus
from .core.screenshot import compose_screenshot
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

    async def terminate(self):
        await self._stop_polling()
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
        msg = (raw or "").strip() or (event.get_message_str() or "").strip()
        if not msg:
            yield event.plain_result("用法：取消订阅 <抖音号或房间号>")
            return
        removed = await self._store.remove_sub(msg)
        yield event.plain_result("已取消：" + msg if removed else "订阅列表里没有这个")

    @filter.command("订阅列表", alias={"dy订阅列表"})
    async def cmd_sublist(self, event: AstrMessageEvent):
        subs = self._store.all_subs()
        if not subs:
            yield event.plain_result("还没有订阅")
            return
        lines = [f"{key}（{sub.get('nickname') or '未填昵称'}）" for key, sub in subs]
        groups = self._store.all_groups()
        yield event.plain_result("\n".join(lines) + f"\n转发群：{'、'.join(groups) if groups else '未配置'}")

    @filter.command("直播转发", alias={"dy转发"})
    async def cmd_fwd(self, event: AstrMessageEvent, raw: str = ""):
        msg = (raw or "").strip() or (event.get_message_str() or "").strip()
        if not msg or not msg.isdigit():
            yield event.plain_result("用法：直播转发 <群号>")
            return
        await self._store.add_group(msg)
        yield event.plain_result(f"已加转发群：{msg}")

    @filter.command("取消转发", alias={"dy取消转发"})
    async def cmd_unfwd(self, event: AstrMessageEvent, raw: str = ""):
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

            # 推送到 notify_groups 群
            groups = [g for g in _to_list(self._c("notify_groups", [])) if g.isdigit()]
            pushed = []
            for gid in groups:
                try:
                    chain = MessageChain()
                    if shot and Path(shot).exists():
                        chain.image(fix_type="local", url=shot)
                    chain.message(f"《{title}》\n{status}\n评论：{comment_text}\n{vc.page_url}")
                    await self._send_to_group(gid, chain)
                    pushed.append(gid)
                except Exception as e:
                    logger.warning("[douyin_ai] 推送群 %s 失败: %s", gid, e)

            tail = f"截图已推送到群：{'、'.join(pushed)}" if pushed else "没配推送群（notify_groups）"
            reply_chain = MessageChain()
            if shot and Path(shot).exists():
                reply_chain.image(fix_type="local", url=shot)
            reply_chain.message(f"《{title}》\n{status}\n评论：{comment_text}\n{tail}")
            yield reply_chain

            cleanup(vc)

    # ================= 登录 =================

    @filter.command("dy登录", alias={"抖音登录", "dy扫码"})
    async def cmd_dy_login(self, event: AstrMessageEvent):
        self._remember_platform(event)
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
        words = self._trigger_words()
        yield event.plain_result("触发词：" + "、".join(words) if words else "还没配触发词（添加触发词 <词>）")
