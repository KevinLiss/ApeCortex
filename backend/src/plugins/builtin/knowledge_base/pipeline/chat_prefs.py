"""聊天知识库偏好与自动注入（周期三：启用集合模型）。

偏好（存 KbSetting，user 维度）：
- chat_include_hub:    启用知识中心公共知识库（默认开）
- chat_enabled_spaces: 启用的个人空间 id 列表（JSON 数组，默认空=全部个人空间）
- chat_memory_enabled: 注入记忆与画像（默认开）
- chat_auto_inject:    每轮自动注入（默认关；token 成本更高）

检索语义：include_hub=False 且 enabled_spaces=[] 时视为「未启用」，不注入不限定；
kb_query 同样按该集合过滤（聊天页配置直接影响工具召回范围）。

自动注入机制：运行时包装宿主 runner._build_context（零宿主源码侵入），
检索为纯本地关键词匹配，零 LLM 调用（成本纪律）；插件卸载时还原。
"""
import json

from loguru import logger
from sqlalchemy import select

from src.db import SessionLocal

_PREF_KEYS = ("chat_include_hub", "chat_enabled_spaces", "chat_memory_enabled", "chat_auto_inject")

_INJECT_MAX_ITEMS = 6          # 注入的记忆/词条条数上限
_INJECT_MAX_CHARS = 2600       # 注入文本总长上限（token 护栏）


# ---------------------------------------------------------------- 偏好读写

async def get_chat_prefs(user_id: int) -> dict:
    from src.plugins.builtin.knowledge_base.models import KbSetting

    async with SessionLocal() as db:
        rows = list((await db.execute(
            select(KbSetting).where(
                KbSetting.user_id == user_id, KbSetting.key.in_(_PREF_KEYS)
            )
        )).scalars().all())
    values = {r.key: r.value for r in rows}
    try:
        spaces = json.loads(values.get("chat_enabled_spaces") or "[]")
        if not isinstance(spaces, list):
            spaces = []
        spaces = [int(x) for x in spaces if str(x).isdigit()][:50]
    except (json.JSONDecodeError, ValueError, TypeError):
        spaces = []
    return {
        "include_hub": values.get("chat_include_hub", "1") == "1",
        "enabled_spaces": spaces,
        "memory_enabled": values.get("chat_memory_enabled", "1") == "1",
        "auto_inject": values.get("chat_auto_inject", "0") == "1",
    }


async def set_chat_prefs(user_id: int, prefs: dict) -> dict:
    """保存偏好（幂等 upsert）。"""
    from src.plugins.builtin.knowledge_base.models import KbSetting

    include_hub = bool(prefs.get("include_hub", True))
    spaces = prefs.get("enabled_spaces") or []
    if not isinstance(spaces, list):
        spaces = []
    spaces = [int(x) for x in spaces if str(x).isdigit()][:50]
    memory_enabled = bool(prefs.get("memory_enabled", True))
    auto_inject = bool(prefs.get("auto_inject", False))

    new_values = {
        "chat_include_hub": "1" if include_hub else "0",
        "chat_enabled_spaces": json.dumps(spaces),
        "chat_memory_enabled": "1" if memory_enabled else "0",
        "chat_auto_inject": "1" if auto_inject else "0",
    }
    async with SessionLocal() as db:
        existing = {
            r.key: r
            for r in (await db.execute(
                select(KbSetting).where(
                    KbSetting.user_id == user_id, KbSetting.key.in_(_PREF_KEYS)
                )
            )).scalars().all()
        }
        for key, value in new_values.items():
            row = existing.get(key)
            if row is not None:
                row.value = value
            else:
                db.add(KbSetting(user_id=user_id, key=key, value=value))
        await db.commit()
    return {"ok": True}


def _is_enabled(prefs: dict) -> bool:
    """启用判定：开了 hub 或显式选了空间（空列表=未限定但 hub 关=未启用）。"""
    return prefs["include_hub"] or bool(prefs["enabled_spaces"])


def _space_filter(prefs: dict) -> list[int] | None:
    """space_ids 传参：None=全部个人空间；[] 是不可能的（此时未启用）。"""
    if prefs["enabled_spaces"]:
        return prefs["enabled_spaces"]
    return None  # 未显式选 = 全部自己的空间


# ---------------------------------------------------------------- 自动注入

async def build_context_injection(user_id: int, question: str) -> str | None:
    """按偏好组装一条 system 注入文本（纯本地检索，零 LLM 调用）。无内容返回 None。"""
    prefs = await get_chat_prefs(user_id)
    if not prefs["auto_inject"] or not _is_enabled(prefs):
        return None

    from src.plugins.builtin.knowledge_base.pipeline.retrieval import search_all

    limit = _INJECT_MAX_ITEMS
    try:
        result = await search_all(
            user_id, question, limit,
            space_ids=_space_filter(prefs),
            include_hub=prefs["include_hub"],
        )
    except Exception as exc:  # noqa: BLE001 — 注入失败绝不影响聊天
        logger.warning(f"[KnowledgeBase] 自动注入检索失败: {exc}")
        return None

    sections: list[str] = []

    if prefs["memory_enabled"]:
        profile = (result.get("profile_excerpt") or "").strip()
        memories = result.get("memories") or []
        if profile:
            sections.append(f"## 用户画像摘要\n{profile[:600]}")
        if memories:
            lines = [f"- {m.get('content', '')}" for m in memories[:limit]]
            sections.append("## 用户长期记忆（相关）\n" + "\n".join(lines))

    pages = (result.get("private_pages") or []) + (result.get("hub_pages") or [])
    if pages:
        lines = [
            f"- {p.get('title', '')}：{p.get('snippet', '')[:160]}"
            for p in pages[:limit]
        ]
        sections.append("## 相关知识词条\n" + "\n".join(lines))

    if not sections:
        return None
    text = "\n\n".join(sections)[:_INJECT_MAX_CHARS]
    return (
        "【ApeCortex 知识库上下文（用户开启自动注入）】以下是与本轮问题相关的用户记忆与知识，"
        "回答时可参考但不要逐条复述；若无帮助可忽略：\n\n" + text
    )


# ---------------------------------------------------------------- runner patch

_original_build_context = None


def patch_runner_context() -> None:
    """运行时包装宿主 runner._build_context（幂等；零宿主源码改动）。"""
    global _original_build_context
    import src.ai.runner as runner_mod

    if getattr(runner_mod, "_kb_inject_patched", False):
        return
    _original_build_context = runner_mod._build_context

    async def _build_with_inject(db, session_id: int, question: str):
        messages = await _original_build_context(db, session_id, question)
        try:
            from src.models.ai import ChatSession

            session = (await db.execute(
                select(ChatSession).where(ChatSession.id == session_id)
            )).scalars().first()
            if session is not None:
                injection = await build_context_injection(session.user_id, question)
                if injection:
                    messages = [{"role": "system", "content": injection}, *messages]
        except Exception as exc:  # noqa: BLE001 — 注入失败绝不影响聊天主流程
            logger.warning(f"[KnowledgeBase] 自动注入失败（忽略）: {exc}")
        return messages

    runner_mod._build_context = _build_with_inject
    runner_mod._kb_inject_patched = True
    logger.info("[KnowledgeBase] runner._build_context 已挂自动注入补丁")


def unpatch_runner_context() -> None:
    """还原宿主 _build_context（插件卸载/禁用时调用）。"""
    global _original_build_context
    import src.ai.runner as runner_mod

    if not getattr(runner_mod, "_kb_inject_patched", False):
        return
    if _original_build_context is not None:
        runner_mod._build_context = _original_build_context
    delattr(runner_mod, "_kb_inject_patched")
    _original_build_context = None
    logger.info("[KnowledgeBase] runner._build_context 补丁已还原")
