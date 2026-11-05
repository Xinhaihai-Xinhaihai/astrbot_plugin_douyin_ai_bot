"""视频打分：用视觉模型对视频打 0-10 分。

评分维度：
- 内容质量 / 趣味性
- 与口味关键词的匹配度
- 特别关心的 up 加权
返回 (score: float, reason: str)。
"""

from __future__ import annotations

import json
import logging

from astrbot.api import logger

from .douyin import DouyinVideoContent

logger = logging.getLogger(__name__)

_SCORE_SYSTEM = (
    "你是视频评分助手。根据视频截图、标题、描述、热门评论，给这个视频打分。\n"
    "评分标准（0-10）：\n"
    "- 内容质量：画面是否清晰、内容是否有趣/有信息量\n"
    "- 口味匹配：{taste_desc}\n"
    "- 互动潜力：热评质量、是否值得评论\n"
    "输出 JSON：{{\"score\": 0-10的整数, \"reason\": \"一句话理由（10字内）\"}}\n"
    "只输出 JSON，不要其他文字。"
)

_SCORE_USER = (
    "标题：{title}\n"
    "描述：{desc}\n"
    "作者：{owner}\n"
    "点赞：{like}\n"
    "热评：{top_comments}\n"
    "（附 {n} 张视频截图）\n"
    "请打分。"
)


def _build_score_prompt(vc: DouyinVideoContent, taste_keywords: list[str], special: bool) -> tuple[str, str]:
    taste_desc = (
        "重点关注这些主题：" + "、".join(taste_keywords)
        if taste_keywords
        else "通用标准，没有特别口味偏好"
    )
    if special:
        taste_desc += "。【特别注意】这是用户特别关注的 up，评分时加 1-2 分。"
    sys_p = _SCORE_SYSTEM.format(taste_desc=taste_desc)
    top = "；".join(f"{u}:{t}" for u, t, _ in vc.top_comments[:4]) or "（无）"
    user_p = _SCORE_USER.format(
        title=vc.title or "（无）",
        desc=(vc.desc[:200] or "（无）"),
        owner=vc.owner or "（未知）",
        like=vc.stat.get("like", 0),
        top_comments=top,
        n=len(vc.frames),
    )
    return sys_p, user_p


async def score_video(
    vc: DouyinVideoContent,
    taste_keywords: list[str],
    is_special: bool,
    context,
    cfg=None,
    min_score: int = 6,
) -> tuple[float, str]:
    """给视频打分，返回 (score, reason)。失败返回 (0, '评分失败')。"""

    def _g(key, default=None):
        try:
            return cfg.get(key, default) if cfg is not None else default
        except Exception:
            return default

    timeout = int(_g("vision_timeout_seconds", 120) or 120)
    main_pid = str(_g("vision_provider_id", "") or "").strip()
    main_model = str(_g("vision_model", "") or "").strip()
    fallbacks = _g("vision_fallback_provider_ids", []) or []
    if isinstance(fallbacks, str):
        fallbacks = [f.strip() for f in fallbacks.replace("，", ",").split(",") if f.strip()]

    sys_p, user_p = _build_score_prompt(vc, taste_keywords, is_special)
    image_urls = [str(p) for p in vc.frames] or None

    for pid, model in [(main_pid, main_model)] + [(f, "") for f in fallbacks] + [("", "")]:
        raw = await _call_vision(context, pid, model, user_p, sys_p, image_urls, timeout)
        if not raw:
            continue
        parsed = _parse_score(raw)
        if parsed is not None:
            s, reason = parsed
            if is_special:
                s = min(10, s + 1)  # 特别关心 +1 保底
            return s, reason
    return 0, "评分失败"


def _parse_score(raw: str) -> tuple[float, str] | None:
    raw = (raw or "").strip()
    # 尝试直接 JSON
    try:
        d = json.loads(raw)
        if isinstance(d, dict) and "score" in d:
            return float(d["score"]), str(d.get("reason", ""))[:30]
    except Exception:
        pass
    # 尝试从文本里抠 JSON
    import re
    m = re.search(r"\{[^{}]*\}", raw)
    if m:
        try:
            d = json.loads(m.group(0))
            if isinstance(d, dict) and "score" in d:
                return float(d["score"]), str(d.get("reason", ""))[:30]
        except Exception:
            pass
    # 尝试抠数字
    m = re.search(r"(\d{1,2})\s*/\s*10", raw)
    if m:
        return float(m.group(1)), raw[:30]
    return None


async def _call_vision(context, provider_id: str, model: str, user_prompt: str,
                       sys_prompt: str, image_urls: list[str] | None, timeout: int) -> str:
    try:
        import asyncio
        mgr = getattr(context, "provider_manager", None)
        if mgr is None:
            return ""
        if provider_id:
            prov = mgr.insts.get(provider_id) if hasattr(mgr, "insts") else None
            if prov is None:
                for p in mgr.insts.values():
                    if getattr(p, "name", "") == provider_id or str(getattr(p, "id", "")) == provider_id:
                        prov = p
                        break
            provider = prov
        else:
            from astrbot.core.provider.manager import ProviderType
            provider = await mgr.get_using_provider_async(ProviderType.CHAT_COMPLETION)
        if provider is None:
            return ""
        coro = provider.text_chat(
            prompt=user_prompt,
            system_prompt=sys_prompt,
            image_urls=image_urls,
            model=model or None,
        )
        resp = await asyncio.wait_for(coro, timeout=timeout)
        return str(getattr(resp, "completion_text", "") or "")
    except Exception as e:
        logger.debug(f"[douyin_ai] 评分调用失败: {e}")
        return ""
