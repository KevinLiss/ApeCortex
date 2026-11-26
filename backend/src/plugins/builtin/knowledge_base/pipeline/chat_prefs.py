"""聊天知识库偏好与自动注入（N3）。

偏好三项（存 KbSetting，user 维度，键 chat_*）：
- chat_recall_scope:  off / private / space / public（召回范围；默认 private）
- chat_recall_space_id: scope=space 时的目标空间
- chat_memory_enabled: 对话中是否注入记忆与画像（默认开）
- chat_auto_inject:   每轮自动注入相关记忆/词条（默认关；token 成本更高）

自动注入机制（零宿主源码侵入）：
- 插件 on_load 时对 ``src.ai.runner._build_context`` 做运行时包装：
  原函数产出 messages 后，若该用户开启 auto_inject，则把检索到的
  相关记忆/词条组装为一条 system 消息插入头部。检索为纯本地关键词
  匹配，零 LLM 调用（成本纪律）；插件卸载时还原原函数。
"""

from loguru import logger
from sqlalchemy import select

from src.db import SessionLocal

_PREF_KEYS = ("chat_recall_scope", "chat_recall_space_id", "chat_memory_enabled", "chat_auto_inject")
_SCOPES = ("off", "private", "space", "public")

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
    scope = values.get("chat_recall_scope", "private")
    return {
        "recall_scope": scope if scope in _SCOPES else "private",
        "recall_space_id": int(values.get("chat_recall_space_id") or 0),
        "memory_enabled": values.get("chat_memory_enabled", "1") == "1",
        "auto_inject": values.get("chat_auto_inject", "0") == "1",
    }


async def set_chat_prefs(user_id: int, prefs: dict) -> dict:
    """保存偏好（幂等 upsert）。"""
    from src.plugins.builtin.knowledge_base.models import KbSetting

    scope = str(prefs.get("recall_scope") or "private")
    if scope not in _SCOPES:
        return {"ok": False, "msg": "召回范围仅支持 off / private / space / public"}
    space_id = int(prefs.get("recall_space_id") or 0)
    memory_enabled = bool(prefs.get("memory_enabled", True))
    auto_inject = bool(prefs.get("auto_inject", False))

    new_values = {
        "chat_recall_scope": scope,
        "chat_recall_space_id": str(space_id),
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


# ---------------------------------------------------------------- 自动注入

async def build_context_injection(user_id: int, question: str) -> str | None:
    """按偏好组装一条 system 注入文本（纯本地检索，零 LLM 调用）。无内容返回 None。"""
    prefs = await get_chat_prefs(user_id)
    if not prefs["auto_inject"] or prefs["recall_scope"] == "off":
        return None

    from src.plugins.builtin.knowledge_base.pipeline.retrieval import search_all

    limit = _INJECT_MAX_ITEMS
    try:
        result = await search_all(user_id, question, limit)
    except Exception as exc:  # noqa: BLE001 — 注入失败绝不影响聊天
        logger.warning(f"[KnowledgeBase] 自动注入检索失败: {exc}")
        return None

    # 按召回范围过滤通道
    scope = prefs["recall_scope"]
    sections: list[str] = []

    if prefs["memory_enabled"]:
        profile = (result.get("profile_excerpt") or "").strip()
        memories = result.get("memories") or []
        if profile:
            sections.append(f"## 用户画像摘要\n{profile[:600]}")
        if memories:
            lines = [f"- {m.get('content', '')}" for m in memories[:limit]]
            sections.append("## 用户长期记忆（相关）\n" + "\n".join(lines))

    # 词条通道：scope 决定取哪些（space 范围横跨三通道按空间过滤）
    if scope == "private":
        pages = result.get("private_pages") or []
    elif scope == "public":
        pages = result.get("public_pages") or []
    elif scope == "space":
        pages = [
            p for p in (result.get("private_pages") or []) + (result.get("shared_pages") or [])
            if (p.get("space") or {}).get("id") == prefs["recall_space_id"]
        ]
    else:
        pages = []
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
