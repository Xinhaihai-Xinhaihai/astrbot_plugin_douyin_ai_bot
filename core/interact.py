"""抖音互动：私信新消息提醒 + 我发的评论被回复提醒.

全部走共享浏览器 DOM 自动化（登录态复用 browser_profile），不自己实现签名接口。
状态持久化到 interact_state.json，重启不重复提醒。
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

from astrbot.api import logger

class InteractState:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.data = self._load()

    def _load(self) -> dict:
        d: dict[str, Any] = {
            "initialized": False,
            "seen_msgs": {},      # 私信: conv_id -> {msg_id: ts}
            "seen_conv_ts": {},   # 私信: conv_id -> last ts
            "seen_replies": {},   # 评论回复: aweme_id+replier -> ts
            "last_dm_poll": 0.0,
            "last_reply_poll": 0.0,
            "bot_name": "",
        }
        try:
            if self.path.exists():
                saved = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(saved, dict):
                    d.update(saved)
                    for k in d:
                        d.setdefault(k, None)
        except Exception as e:
            logger.warning("[douyin_ai] 读互动状态失败: %s", e)
        return d

    def save(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # 裁剪
            for m in ("seen_msgs", "seen_replies", "seen_conv_ts"):
                if isinstance(self.data.get(m), dict):
                    self.data[m] = {k: v for k, v in self.data[m].items() if time.time() - (v if isinstance(v, (int, float)) else 0) < 30 * 86400}
            self.path.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass


async def get_bot_name(browser) -> str:
    """抓自己抖音号昵称（用于识别"我评论被回复"和私信）。"""
    page = None
    try:
        page = await browser.new_page(home=True)
        await page.wait_for_timeout(3000)
        name = await page.evaluate(
            r"""() => {
              const el = document.querySelector('[data-e2e="user-name"], .user-name, [class*="userInfo"] [class*="name"]');
              return el ? (el.innerText||'').trim() : '';
            }"""
        )
        return (name or "").strip()
    except Exception:
        return ""
    finally:
        try:
            if page:
                await browser.close_page(page)
        except Exception:
            pass


async def poll_dm_new(browser, state: InteractState) -> list[dict]:
    """抓消息中心，返回新私信 [{conv_name, conv_ts, text, ts}].

    首次运行只记水位（不补历史），后续按会话时间戳判断新消息。
    """
    out: list[dict] = []
    page = None
    try:
        page = await browser.new_page(home=True)
        await page.wait_for_timeout(2500)
        # 点消息入口（头像旁的消息 icon 或 data-e2e）
        clicked = False
        for sel in ('[data-e2e="message-list"]', 'a[href*="/message"]', '[class*="msgEntry"]', 'div[class*="message-entry"]'):
            try:
                loc = page.locator(sel).first
                if await loc.count() and await loc.is_visible():
                    await loc.click()
                    clicked = True
                    break
            except Exception:
                continue
        if not clicked:
            try:
                await page.goto("https://www.douyin.com/webcast/inbox", wait_until="domcontentloaded", timeout=20000)
            except Exception:
                pass
        await page.wait_for_timeout(3500)

        convs = await page.evaluate(
            r"""() => {
              const out = [];
              // 会话列表：一般是左栏一排，含昵称+最新一条+时间
              const nodes = [...document.querySelectorAll('div, li')];
              for (const el of nodes) {
                const t = (el.innerText||'').trim();
                if (t.length < 4 || t.length > 200) continue;
                if (!/\n/.test(t)) continue;
                // 会话特征：昵称一行 + 内容一行 + 可能时间一行
                const lines = t.split('\n').map(s=>s.trim()).filter(Boolean);
                if (lines.length < 2) continue;
                const nick = lines[0].slice(0, 20);
                const body = lines[1].slice(0, 120);
                const tsRaw = lines[lines.length - 1];
                // 时间特征：含 分钟前/小时前/昨天/数字+点
                if (!/分钟前|小时前|天前|昨天|前天|\d{1,2}:\d{2}|今天|昨天/.test(tsRaw)) continue;
                const key = nick;
                if (!out.some(o => o.nick === key)) out.push({nick, body, time: tsRaw});
              }
              return out.slice(0, 20);
            }"""
        )
        if state.data.get("bot_name"):
            bot = state.data["bot_name"]
            convs = [c for c in convs if c.get("nick") and c["nick"] != bot]

        if not state.data.get("initialized"):
            state.data["initialized"] = True
            for c in convs:
                state.data["seen_conv_ts"][c["nick"]] = time.time()
            state.save()
            return []

        now = time.time()
        for c in convs:
            name = c["nick"]
            last_ts = state.data["seen_conv_ts"].get(name, 0)
            # 会话时间戳是相对文案，无法精确比较：用"上次轮询后出现且文案里时间更近"简化处理
            # 这里采用保守策略：每个会话每 30 分钟最多提醒一次，避免骚扰
            if now - last_ts < 1800:
                continue
            state.data["seen_conv_ts"][name] = now
            out.append({"conv_name": name, "text": c.get("body", ""), "ts": now})
        state.save()
        return out
    except Exception as e:
        logger.warning("[douyin_ai] 私信轮询异常: %s", e)
        return []
    finally:
        try:
            if page:
                await browser.close_page(page)
        except Exception:
            pass


async def poll_comment_replies(browser, aweme_ids: list[str], state: InteractState) -> list[dict]:
    """检查我发过评论的视频，看评论区有没有新回复我的.

    识别方式：评论文本里带 "@我的昵称"。返回 [{aweme_id, replier, text, ts}].
    """
    out: list[dict] = []
    bot_name = state.data.get("bot_name") or ""
    if not bot_name:
        return out
    page = None
    try:
        page = await browser.new_page(home=False)
        for aweme_id in aweme_ids[:5]:  # 每轮最多查 5 个视频
            try:
                await page.goto(f"https://www.douyin.com/video/{aweme_id}", wait_until="domcontentloaded", timeout=25000)
                await page.wait_for_timeout(3000)
                cmts = await page.evaluate(
                    r"""() => {
                      const out = [];
                      const seen = new Set();
                      for (const el of document.querySelectorAll('div, li')) {
                        const t = (el.innerText||'').trim();
                        if (t.length < 6 || t.length > 200 || !/回复/.test(t)) continue;
                        const k = t.slice(0, 30);
                        if (seen.has(k)) continue;
                        seen.add(k);
                        out.push(t);
                        if (out.length >= 30) break;
                      }
                      return out;
                    }"""
                )
                for raw in cmts:
                    if f"@{bot_name}" not in raw and f"@{bot_name} " not in raw:
                        continue
                    lines = [l.strip() for l in raw.split("\n") if l.strip()]
                    replier = lines[0].lstrip("@")[:20] if lines else ""
                    text = lines[1][:100] if len(lines) > 1 else ""
                    key = f"{aweme_id}:{replier}"
                    if key in state.data["seen_replies"]:
                        continue
                    state.data["seen_replies"][key] = time.time()
                    out.append({"aweme_id": aweme_id, "replier": replier, "text": text, "ts": time.time()})
            except Exception as e:
                logger.debug("[douyin_ai] 查 %s 回复失败: %s", aweme_id, e)
                continue
        if out:
            state.save()
        return out
    finally:
        try:
            if page:
                await browser.close_page(page)
        except Exception:
            pass


async def send_dm(browser, conv_name: str, text: str) -> bool:
    """进指定会话发私信（DOM 自动化）。"""
    page = None
    try:
        page = await browser.new_page(home=True)
        await page.wait_for_timeout(2500)
        for sel in ('[data-e2e="message-list"]', 'a[href*="/message"]'):
            try:
                loc = page.locator(sel).first
                if await loc.count() and await loc.is_visible():
                    await loc.click()
                    break
            except Exception:
                continue
        await page.wait_for_timeout(3000)
        # 点会话
        try:
            conv = page.locator(f'text="{conv_name}"').first
            if await conv.count():
                await conv.click()
                await page.wait_for_timeout(2500)
            else:
                return False
        except Exception:
            return False
        box = None
        for sel in ('div[contenteditable="true"]', 'textarea', '[placeholder*="输入"]'):
            try:
                loc = page.locator(sel).last
                if await loc.count() and await loc.is_visible():
                    box = loc
                    break
            except Exception:
                continue
        if box is None:
            return False
        await box.click()
        safe = (text or "").replace("\n", " ").strip()
        if not safe:
            return False
        await box.type(safe, delay=30)
        await page.wait_for_timeout(300)
        for sel in ('text="发送"', 'button:has-text("发送")'):
            try:
                btn = page.locator(sel).first
                if await btn.count() and await btn.is_visible():
                    await btn.click()
                    return True
            except Exception:
                continue
        try:
            await box.press("Enter")
            return True
        except Exception:
            return False
    except Exception as e:
        logger.warning("[douyin_ai] 发私信失败: %s", e)
        return False
    finally:
        try:
            if page:
                await browser.close_page(page)
        except Exception:
            pass
