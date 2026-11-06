"""主动刷视频：定时拉一批视频 → 打分 → 过线的评论+分享.

视频发现源（按优先级）：
1) 特别关心 up 的最新作品（抖音号主页抓最新 N 个 aweme_id）
2) 已订阅抖音号的作品
3) 抖音推荐流（登录态 feed 抓取，可选）

流程：collect_candidates → 去重（cooldown/历史）→ 逐条 fetch_video → score →
score >= min_score 才发评论 → 过线的按配置推给主人/分享群。
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path

from astrbot.api import logger

from .douyin import DouyinVideoContent, fetch_video, parse_aweme_id

_AWEME_RE = re.compile(r"/video/(\d{15,21})")


# 兜底：抖音视频 ID 在 HTML 正文里，但不一定挂在 <a href> 上（首页推荐流卡片就是）
_HTML_ID_JS = r"""() => {
  const html = document.documentElement.innerHTML || "";
  const out = new Set();
  const re = /\/video\/(\d{15,21})/g;
  let m;
  while ((m = re.exec(html)) !== null) out.add(m[1]);
  return [...out];
}"""


async def fetch_user_videos(short_id: str, count: int = 5, browser=None) -> list[str]:
    """抓某个抖音号主页最新 N 个视频 aweme_id. browser 必须传入（保证 channel 正确）。"""
    if browser is None:
        return []
    if not re.fullmatch(r"[A-Za-z0-9_.\-]+", short_id or ""):
        logger.warning("[douyin_ai] 订阅键 %r 不是抖音号（直播间号抓不到主页），跳过", short_id)
        return []
    b = browser
    page = None
    try:
        page = await b.new_page(home=False)
        await page.goto(f"https://www.douyin.com/user/{short_id}", wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(4000)
        for _ in range(3):
            await page.evaluate("window.scrollBy(0, 600)")
            await page.wait_for_timeout(800)
        ids = await page.evaluate(
            r"""() => {
              const out = [];
              for (const a of document.querySelectorAll('a[href*="/video/"]')) {
                const m = a.href.match(/\/video\/(\d{15,21})/);
                if (m && !out.includes(m[1])) out.push(m[1]);
              }
              return out;
            }"""
        )
        if not ids:
            # 链接选择器抓不到时，从 HTML 正文兜底（只取正文，排除 nav 里的无关链接）
            ids = await page.evaluate(_HTML_ID_JS)
        return list(ids[:count])
    except Exception as e:
        logger.warning(f"[douyin_ai] 抓 {short_id} 作品失败: {e}")
        return []
    finally:
        if page is not None:
            try:
                await b.close_page(page)
            except Exception:
                pass


async def fetch_feed_videos(count: int = 5, browser=None) -> list[str]:
    """登录态刷推荐流，抓 N 个视频 aweme_id. browser 必须传入（保证 channel 正确）。"""
    if browser is None:
        return []
    b = browser
    page = None
    try:
        page = await b.new_page(home=True)
        await page.wait_for_timeout(3000)
        seen: list[str] = []
        for _ in range(count // 2 + 2):
            await page.evaluate("window.scrollBy(0, 1500)")
            await page.wait_for_timeout(2000)
            ids = await page.evaluate(_HTML_ID_JS)
            for i in ids:
                if i not in seen:
                    seen.append(i)
                if len(seen) >= count:
                    break
            if len(seen) >= count:
                break
        return seen[:count]
    except Exception as e:
        logger.warning(f"[douyin_ai] 抓推荐流失败: {e}")
        return []
    finally:
        if page is not None:
            try:
                await b.close_page(page)
            except Exception:
                pass


class SeenStore:
    """记录刷过/评过的 aweme_id，防止重复看。"""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._data: dict = {"seen": {}, "commented": {}}
        self._load()

    def _load(self):
        try:
            if self.path.exists():
                d = json.loads(self.path.read_text(encoding="utf-8"))
                self._data = d
                self._data.setdefault("seen", {})
                self._data.setdefault("commented", {})
        except Exception:
            pass

    def _save(self):
        # 裁剪：seen 只留 30 天，commented 留 365 天
        # 注意：seen 的值是时间戳 float；commented 的值是 dict（{"ts","score","comment"}），取 ts 才能减
        now = time.time()
        seen_clean = {}
        for k, v in self._data["seen"].items():
            if isinstance(v, (int, float)) and now - v < 30 * 86400:
                seen_clean[k] = v
        self._data["seen"] = seen_clean
        commented_clean = {}
        for k, v in self._data["commented"].items():
            ts = v.get("ts") if isinstance(v, dict) else v
            if isinstance(ts, (int, float)) and now - ts < 365 * 86400:
                commented_clean[k] = v
        self._data["commented"] = commented_clean
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

    def is_seen(self, aweme_id: str) -> bool:
        return aweme_id in self._data["seen"]

    def mark_seen(self, aweme_id: str):
        self._data["seen"][aweme_id] = time.time()
        self._save()

    def is_commented(self, aweme_id: str) -> bool:
        return aweme_id in self._data["commented"]

    def mark_commented(self, aweme_id: str, score: float = 0, comment: str = ""):
        self._data["commented"][aweme_id] = {"ts": time.time(), "score": score, "comment": comment}
        self._save()

    def recent_commented(self, limit: int = 20) -> list[tuple[str, dict]]:
        items = sorted(self._data["commented"].items(), key=lambda x: x[1].get("ts", 0), reverse=True)
        return items[:limit]
