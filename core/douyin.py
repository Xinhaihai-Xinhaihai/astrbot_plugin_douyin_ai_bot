"""抖音视频内容获取 + 发评论.

- 用共享浏览器（持久化登录态）打开视频页，从 DOM 抓标题/作者/时长/统计
- 拦截网络响应拿 mp4 直链，下载后用 ffmpeg 抽帧喂视觉模型
- 评论区用 UI 自动化发送（避免自己实现 a_bogus 签名）
- 不点赞、不收藏，只读内容 + 发评论
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from pathlib import Path

from astrbot.api import logger

from .browser import USER_AGENT, DouyinBrowser

_MP4_HINT = re.compile(r"(douyinvod|\.mp4|amemv|byteeffecttos)")


@dataclass
class DouyinVideoContent:
    aweme_id: str
    title: str = ""
    desc: str = ""
    duration: float = 0.0
    owner: str = ""
    cover: str = ""
    stat: dict = field(default_factory=dict)
    share_url: str = ""
    video_path: str = ""
    frames: list[str] = field(default_factory=list)
    top_comments: list[tuple[str, str, int]] = field(default_factory=list)

    @property
    def page_url(self) -> str:
        return f"https://www.douyin.com/video/{self.aweme_id}"


def parse_aweme_id(raw: str) -> str | None:
    """从整段分享文本 / 链接 / 纯数字里抠出视频 id."""
    raw = (raw or "").strip()
    if raw.isdigit() and len(raw) >= 15:
        return raw
    m = re.search(r"(?:douyin\.com/video/|ies\.douyin\.com/share/video/|v\.douyin\.com/[A-Za-z0-9_-]+/?)(\d{15,})", raw)
    if m:
        return m.group(1)
    # 兜底：任意 19 位数字
    m = re.search(r"(?<!\d)(\d{19})(?!\d)", raw)
    if m:
        return m.group(1)
    return None


async def fetch_video(aweme_id: str, frame_count: int = 6) -> DouyinVideoContent:
    """打开视频页，抓元数据 + 抓 mp4 直链下载抽帧 + 抓热评."""
    vc = DouyinVideoContent(aweme_id=aweme_id)
    page = None
    mp4_url = ""
    try:
        browser = DouyinBrowser.get(
            profile_dir=_profile_dir(),
            headless=True,
            channel="msedge",
        )
        captured: list[str] = []

        async def _on_resp(resp):
            try:
                u = resp.url
                if _MP4_HINT.search(u):
                    captured.append(u)
            except Exception:
                pass

        page = await browser.new_page()
        page.on("response", lambda r: asyncio.ensure_future(_on_resp(r)))
        await page.goto(vc.page_url, wait_until="domcontentloaded", timeout=30000)
        try:
            await page.wait_for_selector("video", timeout=15000)
        except Exception:
            pass
        await page.wait_for_timeout(2500)

        # ---- DOM 元数据 ----
        info = await page.evaluate(
            r"""() => {
              const t = (el) => el ? (el.innerText||'').trim() : '';
              const desc = t(document.querySelector('[data-e2e="video-desc"]'));
              const nick = t(document.querySelector('.account-name, [data-e2e="feed-video-nickname"]'));
              const v = document.querySelector('video');
              let cover = '';
              const imgs = [...document.querySelectorAll('img')]
                .map(i => ({src: i.src||'', w: i.naturalWidth, h: i.naturalHeight}))
                .filter(i => i.w*i.h >= 60000 && i.src && !i.src.startsWith('data:'))
                .sort((a,b)=>b.w*b.h - a.w*a.h);
              if (imgs.length) cover = imgs[0].src;
              // 统计：找 video-player-* 里的数字（键名对齐截图渲染：like/reply/favorite）
              const stat = {};
              for (const [key, sel] of [['like','[data-e2e="video-player-digg"]'],
                                        ['reply','[data-e2e="video-player-comment"]'],
                                        ['favorite','[data-e2e="video-player-collect"]']]) {
                const el = document.querySelector(sel);
                if (el) stat[key] = t(el);
              }
              return {desc, nick, duration: v ? v.duration : 0, cover, stat, share_url: location.href};
            }"""
        )
        vc.desc = (info.get("desc") or "")[:300]
        vc.title = vc.desc.split("\n")[0][:120] if vc.desc else ""
        vc.owner = (info.get("nick") or "").lstrip("@").strip()
        vc.duration = float(info.get("duration") or 0)
        vc.cover = info.get("cover") or ""
        vc.share_url = vc.page_url
        vc.stat = _parse_stat(info.get("stat") or {})

        # ---- mp4 直链 ----
        for u in dict.fromkeys(captured):
            if "douyinvod" in u or ".mp4" in u:
                mp4_url = u
                break
        if mp4_url:
            mp4 = await _download_mp4(mp4_url, browser)
            if mp4:
                vc.video_path = str(mp4)
                vc.frames = await _extract_frames(str(mp4), vc.duration, frame_count)
        if not vc.frames:
            logger.warning("[douyin_ai] 没拿到 mp4，只用标题/热评")

        # ---- 热评（滚到评论区，抓 DOM） ----
        try:
            for _ in range(6):
                await page.evaluate("window.scrollBy(0, 700)")
                await page.wait_for_timeout(600)
        except Exception:
            pass
        try:
            cmts = await page.evaluate(
                r"""() => {
                  const out = [];
                  const seen = new Set();
                  // 通用：找含"回复"操作的评论卡
                  const nodes = [...document.querySelectorAll('div, li')];
                  for (const el of nodes) {
                    const txt = (el.innerText||'').trim();
                    if (txt.length < 6 || txt.length > 240) continue;
                    if (!/回复/.test(txt)) continue;
                    const key = txt.slice(0, 40);
                    if (seen.has(key)) continue;
                    seen.add(key);
                    out.push(txt);
                    if (out.length >= 6) break;
                  }
                  return out;
                }"""
            )
            for raw in cmts:
                parsed = _parse_comment(raw)
                if parsed:
                    vc.top_comments.append(parsed)
        except Exception as e:
            logger.debug(f"[douyin_ai] 抓热评失败: {e}")

    except Exception as e:
        logger.warning(f"[douyin_ai] 拉取视频 {aweme_id} 失败: {e}")
        raise
    finally:
        if page is not None:
            try:
                await DouyinBrowser._instance.close_page(page)
            except Exception:
                pass

    return vc


def _profile_dir() -> Path:
    from astrbot.api.star import StarTools

    d = Path(StarTools.get_data_dir("astrbot_plugin_douyin_ai_bot")) / "browser_profile"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _work_dir() -> Path:
    from astrbot.api.star import StarTools
    import tempfile

    d = Path(StarTools.get_data_dir("astrbot_plugin_douyin_ai_bot")) / "tmp"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _parse_stat(stat: dict) -> dict:
    out = {}
    for k, v in stat.items():
        s = str(v).replace(" ", "")
        out[k] = _to_int(s)
    return out


def _to_int(s: str) -> int:
    s = (s or "").strip()
    if not s:
        return 0
    m = re.match(r"^([\d.]+)\s*([万亿]?)$", s)
    if not m:
        try:
            return int(s)
        except Exception:
            return 0
    num, unit = m.group(1), m.group(2)
    try:
        val = float(num)
    except Exception:
        return 0
    if unit == "万":
        val *= 10000
    elif unit == "亿":
        val *= 100000000
    return int(val)


def _parse_comment(raw: str) -> tuple[str, str, int] | None:
    """把一条评论的 innerText 拆成 (用户, 内容, 点赞数)."""
    lines = [l.strip() for l in raw.split("\n") if l.strip()]
    if len(lines) < 2:
        return None
    user = ""
    body = ""
    like = 0
    for i, l in enumerate(lines):
        if re.match(r"^@?.{1,20}$", l) and not user and i == 0:
            user = l.lstrip("@")
            continue
        m = re.match(r"^(\d+)$", l)
        if m and i == len(lines) - 1:
            like = int(m.group(1))
            continue
        if len(l) > 4 and body == "":
            body = l[:120]
    if body:
        return user, body, like
    return None


async def _download_mp4(url: str, browser: DouyinBrowser) -> Path | None:
    import aiohttp

    cookies = await browser.cookies()
    header = "; ".join(f"{c['name']}={c['value']}" for c in cookies if "douyin" in (c.get("domain") or ""))
    dest = _work_dir() / f"dy_{abs(hash(url))}.mp4"
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(
                url,
                headers={"User-Agent": USER_AGENT, "Cookie": header, "Referer": "https://www.douyin.com/"},
                timeout=aiohttp.ClientTimeout(total=180),
            ) as r:
                if r.status != 200:
                    logger.warning(f"[douyin_ai] mp4 下载 status={r.status}")
                    return None
                dest.write_bytes(await r.read())
        return dest
    except Exception as e:
        logger.warning(f"[douyin_ai] mp4 下载失败: {e}")
        return None


async def _extract_frames(video_path: str, duration: float, frame_count: int) -> list[str]:
    import time

    try:
        import imageio_ffmpeg
    except Exception:
        logger.warning("[douyin_ai] 缺 imageio-ffmpeg，无法抽帧")
        return []
    duration = float(duration or 0)
    if duration <= 0 or not video_path:
        return []
    work = Path(video_path).parent
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    tag = f"frame_{int(time.time())}"
    frames: list[str] = []
    for i in range(frame_count):
        frac = (i + 1) / (frame_count + 1)
        t = duration * frac
        out = work / f"{tag}_{i}.jpg"
        cmd = [
            ffmpeg, "-y",
            "-ss", f"{t:.2f}",
            "-i", video_path,
            "-frames:v", "1",
            "-vf", "scale=480:-2",
            "-q:v", "4", str(out),
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
            )
            await asyncio.wait_for(proc.wait(), timeout=60)
        except Exception:
            continue
        if out.exists() and out.stat().st_size > 0:
            frames.append(str(out))
    return frames


async def send_comment(aweme_id: str, text: str) -> bool:
    """UI 自动化发评论：点开评论面板→输入→发布。登录态由共享浏览器保证。"""
    page = None
    try:
        browser = DouyinBrowser.get(
            profile_dir=_profile_dir(),
            headless=True,
            channel="msedge",
        )
        page = await browser.new_page()
        await page.goto(
            f"https://www.douyin.com/video/{aweme_id}",
            wait_until="domcontentloaded",
            timeout=30000,
        )
        await page.wait_for_timeout(3000)

        # 打开评论面板
        try:
            await page.click('[data-e2e="feed-comment-icon"]', timeout=8000)
        except Exception as e:
            logger.warning(f"[douyin_ai] 点不开评论区: {e}")
            return False
        await page.wait_for_timeout(2500)

        # 输入框：优先 placeholder 含"评论"/"说点什么"
        box = None
        for sel in (
            '[placeholder*="评论"]',
            '[data-slate-editor]',
            '.rich-input div[contenteditable="true"]',
            'div[contenteditable="true"]',
            'textarea',
        ):
            try:
                loc = page.locator(sel).first
                if await loc.count() and await loc.is_visible():
                    box = loc
                    break
            except Exception:
                continue
        if box is None:
            logger.warning("[douyin_ai] 找不到评论输入框")
            return False

        await box.click()
        await page.wait_for_timeout(300)
        # 输入过滤掉换行（评论框对换行敏感）
        safe = (text or "").replace("\n", " ").strip()
        if not safe:
            return False
        await box.type(safe, delay=40)
        await page.wait_for_timeout(400)

        # 提交：优先文字"发布"
        clicked = False
        for sel in ('text="发布"', 'button:has-text("发布")', 'text="发送"'):
            try:
                btn = page.locator(sel).first
                if await btn.count() and await btn.is_visible():
                    await btn.click()
                    clicked = True
                    break
            except Exception:
                continue
        if not clicked:
            # 兜底 Ctrl+Enter
            try:
                await box.press("Control+Enter")
                clicked = True
            except Exception:
                pass
        if not clicked:
            return False

        await page.wait_for_timeout(2500)
        # 简单验证：输入框清空或页面出现刚发的文本
        try:
            val = await page.evaluate(
                """() => {
                  const eds = [...document.querySelectorAll('div[contenteditable="true"], textarea, [data-slate-editor]')];
                  return eds.map(e => e.innerText || e.value || '').join('|');
                }"""
            )
            if safe[:6] and safe[:6] in (val or ""):
                logger.warning("[douyin_ai] 评论似乎没发出去（输入框还在）")
                return False
        except Exception:
            pass
        logger.info(f"[douyin_ai] 评论已发出 {aweme_id}")
        return True

    except Exception as e:
        logger.warning(f"[douyin_ai] 发评论失败 {aweme_id}: {e}")
        return False
    finally:
        if page is not None:
            try:
                await DouyinBrowser._instance.close_page(page)
            except Exception:
                pass


def cleanup(vc: DouyinVideoContent) -> None:
    """删掉下载的视频和抽帧文件."""
    try:
        seen: set[Path] = set()
        paths: list[Path] = []
        if vc.video_path:
            paths.append(Path(vc.video_path))
        paths.extend(Path(p) for p in vc.frames)
        for p in paths:
            if p in seen or not p.exists():
                continue
            seen.add(p)
            try:
                p.unlink()
            except Exception:
                pass
    except Exception:
        pass
