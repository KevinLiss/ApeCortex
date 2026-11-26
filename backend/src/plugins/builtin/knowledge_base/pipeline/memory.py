"""记忆层管线——聊天完成钩子触发的后台提取 worker。

.. design:: 设计依据（开源对标精读结论）
   - mem0 V3 管线（mem0/memory/main.py:920-1207）：**单次 LLM ADD-only 提取**
     （不再做"LLM 返回 ID 的 UPDATE/DELETE 决策"——官方已退役该路线，脆弱），
     MD5 hash 双重去重（库内 + 批内），命中旧记忆即"证据 +1"强化。
   - mem0 双层 JSON 降级解析（main.py:974-986）：json.loads 失败 → 剥代码围栏 →
     截首尾大括号重试 → 仍失败记日志返回空（绝不抛进聊天流）。
   - EverOS 双粒度（docs/how-memory-works.md）：episode 粗存 + profile 单文件整体重写，
     画像不追加、每次全量重写，避免碎片累积。
   - graphiti 双时序：旧事实不删、打 invalid_at 软失效标记（模型字段已支持，v1 仅写入）。

成本纪律：一次聊天完成 = 恰好 1 次 LLM 调用（提取）；画像重写按阈值触发另算。
全程 try/except 包裹，任何失败只记日志，绝不影响聊天主流程。
"""
import asyncio
import hashlib
import json
import re
from datetime import UTC, datetime

from loguru import logger
from sqlalchemy import func, select

from src.db import SessionLocal

# ---------------------------------------------------------------- 常量

# 单次提取送入 LLM 的最近消息条数（提取上下文，非聊天上下文）
_EXTRACT_CONTEXT_MESSAGES = 12
# 提取 prompt 中附带的历史记忆条数（去重参照，mem0 惯例：仅供参考禁照抄）
_DEDUP_REFERENCE_LIMIT = 30
# 单次提取上限（护栏：防 LLM 超量输出）
_MAX_ITEMS_PER_RUN = 10
# 画像自动重写触发阈值：每新增 N 条有效记忆触发一次
_AUTO_REWRITE_EVERY = 20
# 记忆内容长度约束
_MIN_CONTENT_LEN = 4
_MAX_CONTENT_LEN = 200

# 会话级去抖：同会话并发完成时只跑一个提取任务（结果靠 hash 去重兜底）
_running_sessions: set[int] = set()
_running_lock = asyncio.Lock()

# ---------------------------------------------------------------- Prompt

_EXTRACT_SYSTEM_PROMPT = """你是一个记忆提取引擎，负责从对话中提取值得长期记住的用户知识。

【提取范围】只提取满足以下任一条件的信息：
- fact：稳定的客观事实（如"用户的公司名"、"用户的项目技术栈"）
- preference：稳定偏好（如"用户偏好中文回复"、"不喜欢长篇大论"）
- habit：重复出现的习惯（如"总是在深夜工作"）
- event：重要事件（如"用户的产品发布了 v2.0"）

【必须跳过】闲聊寒暄、临时性问答（如"这个代码怎么写"的一次性求助）、
对话中 AI 的输出内容、与你已掌握记忆重复或矛盾程度相同的信息。

【绝对禁止提取】以下不是用户知识，一律丢弃（哪怕看起来像事实）：
- 系统 / 接口状态：权限不足、调用失败、接口不存在、服务未启动、HTTP 错误码（404/403 等）
- 工具与插件的运行结果、报错、能力限制（如"kb_query 调用失败""当前账号缺少权限"）
- 对 AI 的指令或对 AI 自身状态的描述（如"请开启联网开关""我无法访问网络"）
- 一次性的操作步骤、故障排查过程、页面上的临时提示

【已知记忆列表】仅供你去重参照——不要重复提取已知内容；
若新信息与已知记忆明显更新/演化，照常提取（后续由系统合并）。

【输出格式】只输出 JSON，不要任何其他文字：
{"memories": [{"content": "……", "type": "fact|preference|habit|event"}]}

【质量规则】
- 每条 content 为 15~80 字的自包含陈述（不看上下文也能懂）
- content 用中文；客观、可验证；禁止臆测
- 本次最多提取 10 条；没有值得提取的就输出空数组
"""


def _build_extract_user_prompt(transcript: str, known_memories: list[str]) -> str:
    known = "\n".join(f"- {m}" for m in known_memories) or "（暂无已知记忆）"
    return f"""【已知记忆】
{known}

【对话记录】（最近 {_EXTRACT_CONTEXT_MESSAGES} 条，最后一条为刚完成的回复）
{transcript}

请提取其中值得长期记住的新知识。只输出 JSON。"""


_PROFILE_SYSTEM_PROMPT = """你是用户画像生成器。基于记忆条目库生成（或更新）一份用户画像。

【要求】
- 输出纯 Markdown（不要代码围栏），使用以下分节结构（无内容的节可省略）：
  ## 基本信息 / ## 偏好 / ## 习惯 / ## 长期事实 / ## 近期关注
- 每条结论后用 `(置信度: 高/中/低)` 标注
- 优先采纳置信度高、证据多的记忆；失效（invalid）记忆视为历史，仅当演化脉络有价值时一句话提及
- 与现有画像冲突时，以新记忆为准（演化）
- 60~120 行以内；客观陈述，禁止臆测
"""


# ---------------------------------------------------------------- 工具函数

def _normalize_for_hash(content: str) -> str:
    """归一化内容用于 MD5 去重：去空白、拉丁转小写（中文不受影响）。"""
    collapsed = "".join(content.split())
    return collapsed.lower()


def _content_hash(content: str) -> str:
    return hashlib.md5(_normalize_for_hash(content).encode("utf-8")).hexdigest()


def _strip_code_fences(text: str) -> str:
    """剥掉 ```json ... ``` 围栏（mem0 utils.extract_json 第一层降级）。"""
    t = text.strip()
    if t.startswith("```"):
        first_newline = t.find("\n")
        if first_newline != -1:
            t = t[first_newline + 1:]
        if t.rstrip().endswith("```"):
            t = t.rstrip()[:-3]
    return t.strip()


def _extract_outermost_json(text: str) -> str:
    """截取首个 { 到最后一个 } 之间的内容（mem0 第二层降级）。"""
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end != -1 and end > start:
        return text[start:end + 1]
    return text


# 系统状态 / 工具报错类噪声（与 prompt 的【绝对禁止提取】配套的后置防线）
_NOISE_PATTERNS = (
    re.compile(r"(权限不足|无法调用|调用失败|检索失败|抓取失败|执行失败|接口不存在|服务未启动|未启动|未更新|"
               r"未开启|请开启|需开启|Request failed|status code|HTTP\s*\d{3}|\b4\d{2}\b)"),
)


def is_system_noise(content: str) -> bool:
    """判断一条候选是否属于“系统状态/工具报错”，不应作为用户知识沉淀。"""
    return any(pat.search(content or "") for pat in _NOISE_PATTERNS)


# 冲突演化（M4）：同类型 + 同主体前缀 + 高相似但不同值 → 旧事实软失效（graphiti 双时序）
_CONFLICT_SIM = 0.45
_CONFLICT_SUBJECT_LEN = 4
_CONFLICT_TYPES = ("fact", "preference")


def _shingles(text: str) -> set[str]:
    """中文 2-gram + 英文/数字词（用于轻量相似度）。"""
    tokens: set[str] = set()
    for w in re.findall(r"[A-Za-z0-9_]+", text.lower()):
        tokens.add(w)
    cn = re.sub(r"[^\u4e00-\u9fff]", "", text)
    for i in range(len(cn) - 1):
        tokens.add(cn[i:i + 2])
    return tokens


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


async def _invalidate_conflicts(db, user_id: int, content: str, mem_type: str, now) -> int:
    """新旧事实并存时，把旧条目打 invalid_at（不删除，保留演化史）。"""
    from src.plugins.builtin.knowledge_base.models import KbMemoryItem

    if mem_type not in _CONFLICT_TYPES:
        return 0
    rows = list((await db.execute(
        select(KbMemoryItem).where(
            KbMemoryItem.user_id == user_id,
            KbMemoryItem.mem_type == mem_type,
            KbMemoryItem.invalid_at.is_(None),
            KbMemoryItem.expired_at.is_(None),
        )
    )).scalars().all())
    new_tokens = _shingles(content)
    invalidated = 0
    for old in rows:
        if old.content[: _CONFLICT_SUBJECT_LEN] != content[: _CONFLICT_SUBJECT_LEN]:
            continue  # 主体不同，不算冲突
        if _jaccard(new_tokens, _shingles(old.content)) >= _CONFLICT_SIM:
            old.invalid_at = now
            invalidated += 1
            logger.info(f"[KnowledgeBase] 记忆演化失效: {old.content[:40]} ← {content[:40]}")
    return invalidated


def parse_extraction_response(raw: str) -> list[dict]:
    """解析提取响应为记忆条目列表（双层降级，永不抛异常）。"""
    if not raw or not raw.strip():
        return []
    text = raw.strip()
    data = None
    for attempt in (text, _strip_code_fences(text), _extract_outermost_json(_strip_code_fences(text))):
        try:
            data = json.loads(attempt)
            break
        except (json.JSONDecodeError, ValueError):
            continue
    if not isinstance(data, dict):
        return []
    memories = data.get("memories")
    if not isinstance(memories, list):
        return []

    items: list[dict] = []
    for m in memories[:_MAX_ITEMS_PER_RUN]:
        if not isinstance(m, dict):
            continue
        content = str(m.get("content", "")).strip()
        mem_type = str(m.get("type", "fact")).strip().lower()
        if len(content) < _MIN_CONTENT_LEN or len(content) > _MAX_CONTENT_LEN:
            continue
        if mem_type not in ("fact", "preference", "habit", "event"):
            mem_type = "fact"
        items.append({"content": content, "mem_type": mem_type})
    return items


async def _get_llm_config(db):
    """取第一个启用的 provider，返回 (api_key, base_url, model) 或 None。"""
    from src.ai.agent import _get_provider_config
    from src.core.crypto import decrypt_api_key
    from src.crud.ai import crud_ai_provider
    from src.models.ai import AiProvider

    provider = await crud_ai_provider.get_first_enabled(db)
    if provider is None or not isinstance(provider, AiProvider):
        return None
    config = _get_provider_config(provider)
    return {
        "api_key": decrypt_api_key(provider.api_key_enc),
        "base_url": config["base_url"],
        "model": config["default_model"],
    }


async def _call_llm_json(system: str, user: str, max_tokens: int, temperature: float) -> str | None:
    """单次 LLM 调用（无工具循环），返回原始文本。失败返回 None。"""
    from src.ai.agent import _call_llm  # v1 复用内部接口；后续可在 agent.py 暴露公共封装

    async with SessionLocal() as db:
        cfg = await _get_llm_config(db)
    if cfg is None:
        logger.warning("[KnowledgeBase] 无可用 AI provider，跳过记忆提取")
        return None
    try:
        resp = await _call_llm(
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            api_key=cfg["api_key"],
            base_url=cfg["base_url"],
            model=cfg["model"],
            max_tokens=max_tokens,
            temperature=temperature,
            tools=None,
        )
        return resp["choices"][0]["message"]["content"]
    except Exception as exc:
        logger.error(f"[KnowledgeBase] LLM 调用失败: {exc}")
        return None


# ---------------------------------------------------------------- 主管线

async def extract_and_store(
    user_id: int,
    session_id: int,
    run_id: int | None = None,
    transcript_override: str | None = None,
) -> int:
    """从会话提取记忆并入库（ADD-only + 去重强化）。返回新增条数。"""
    from src.models.ai import ChatMessageModel
    from src.plugins.builtin.knowledge_base.models import KbMemoryItem

    async with SessionLocal() as db:
        # 1. 组装对话记录（服务端事实源——runner 持久化后的最近消息，含刚存入的 assistant 回复）
        if transcript_override:
            transcript = transcript_override
        else:
            stmt = (
                select(ChatMessageModel)
                .where(ChatMessageModel.session_id == session_id)
                .order_by(ChatMessageModel.id.desc())
                .limit(_EXTRACT_CONTEXT_MESSAGES)
            )
            rows = list((await db.execute(stmt)).scalars().all())
            rows.reverse()
            transcript = "\n".join(
                f"{'用户' if m.role == 'user' else '助手'}: {m.content}" for m in rows if m.content.strip()
            )
        if len(transcript.strip()) < 20:
            return 0  # 太短不值得提取（省一次 LLM 调用）

        # 2. 已知记忆（去重参照）
        known_stmt = (
            select(KbMemoryItem.content)
            .where(
                KbMemoryItem.user_id == user_id,
                KbMemoryItem.expired_at.is_(None),
                KbMemoryItem.invalid_at.is_(None),
            )
            .order_by(KbMemoryItem.updated_at.desc())
            .limit(_DEDUP_REFERENCE_LIMIT)
        )
        known = [row[0] for row in (await db.execute(known_stmt)).all()]

    # 3. 单次 LLM 提取（ADD-only）
    raw = await _call_llm_json(
        _EXTRACT_SYSTEM_PROMPT,
        _build_extract_user_prompt(transcript, known),
        max_tokens=900,
        temperature=0.2,
    )
    if raw is None:
        return 0
    items = parse_extraction_response(raw)
    if not items:
        return 0

    # 4. 入库：批内去重 + 库内去重（命中=强化）
    inserted = 0
    reinforced = 0
    now = datetime.now(UTC)
    async with SessionLocal() as db:
        seen_hashes: set[str] = set()
        for item in items:
            if is_system_noise(item["content"]):
                logger.info(f"[KnowledgeBase] 丢弃系统状态类记忆: {item['content'][:40]}")
                continue
            chash = _content_hash(item["content"])
            if chash in seen_hashes:
                continue  # 批内去重
            seen_hashes.add(chash)

            existing = (
                await db.execute(
                    select(KbMemoryItem).where(
                        KbMemoryItem.user_id == user_id,
                        KbMemoryItem.content_hash == chash,
                    )
                )
            ).scalars().first()
            if existing is not None:
                # 命中旧记忆：证据 +1（mem0 哲学：重复出现=强化，不做 LLM 更新决策）
                existing.source_count += 1
                existing.confidence = min(0.99, existing.confidence + 0.05)
                reinforced += 1
                continue

            await _invalidate_conflicts(db, user_id, item["content"], item["mem_type"], now)

            db.add(
                KbMemoryItem(
                    user_id=user_id,
                    mem_type=item["mem_type"],
                    content=item["content"],
                    content_hash=chash,
                    confidence=0.6,
                    source_count=1,
                    session_id=session_id,
                    source_run_id=run_id,
                    valid_at=now,
                )
            )
            inserted += 1
        if inserted or reinforced:
            await db.commit()
            logger.info(
                f"[KnowledgeBase] user={user_id} session={session_id} "
                f"新增 {inserted} 条 / 强化 {reinforced} 条"
            )

        # 5. 画像自动重写触发（阈值触发，成本后置）
        total = (
            await db.execute(
                select(func.count()).select_from(KbMemoryItem).where(
                    KbMemoryItem.user_id == user_id,
                    KbMemoryItem.expired_at.is_(None),
                )
            )
        ).scalar_one()
        if total > 0 and total % _AUTO_REWRITE_EVERY < inserted:
            await _rewrite_profile_in_session(db, user_id)

    return inserted


async def _rewrite_profile_in_session(db, user_id: int) -> bool:
    """在给定 session 内重写画像（调用方负责 commit）。返回是否成功。"""
    from src.plugins.builtin.knowledge_base.models import KbMemoryItem, KbProfile

    mem_stmt = (
        select(KbMemoryItem)
        .where(
            KbMemoryItem.user_id == user_id,
            KbMemoryItem.expired_at.is_(None),
        )
        .order_by(KbMemoryItem.source_count.desc(), KbMemoryItem.confidence.desc())
        .limit(80)
    )
    memories = list((await db.execute(mem_stmt)).scalars().all())
    if not memories:
        return False

    profile = (
        await db.execute(select(KbProfile).where(KbProfile.user_id == user_id))
    ).scalars().first()

    mem_lines = "\n".join(
        f"- [{m.mem_type}] {m.content} (置信度{m.confidence:.2f}, 证据{m.source_count}"
        + (", 已失效" if m.invalid_at is not None else "")
        + ")"
        for m in memories
    )
    existing = f"【现有画像 v{profile.version}】\n{profile.profile_md}" if profile else "（首次生成）"

    raw = await _call_llm_json(
        _PROFILE_SYSTEM_PROMPT,
        f"{existing}\n\n【记忆条目库（按证据与置信度排序，前80条）】\n{mem_lines}\n\n请生成/更新画像。",
        max_tokens=1400,
        temperature=0.4,
    )
    if raw is None:
        return False
    md = _strip_code_fences(raw)
    if profile is None:
        profile = KbProfile(user_id=user_id, profile_md=md, version=1)
        db.add(profile)
    else:
        profile.profile_md = md
        profile.version += 1
    logger.info(f"[KnowledgeBase] user={user_id} 画像已重写")
    return True


async def rewrite_profile(user_id: int) -> bool:
    """独立重写入口（API/MCP 调用）。"""
    async with SessionLocal() as db:
        ok = await _rewrite_profile_in_session(db, user_id)
        if ok:
            await db.commit()
        return ok


async def save_explicit(user_id: int, content: str, mem_type: str = "fact") -> dict:
    """显式保存一条记忆（kb_save MCP 工具入口，走同一去重管线）。"""
    from src.plugins.builtin.knowledge_base.models import KbMemoryItem

    content = content.strip()
    if len(content) < _MIN_CONTENT_LEN or len(content) > _MAX_CONTENT_LEN:
        return {"ok": False, "msg": "content 长度需在 4~200 字之间"}
    if mem_type not in ("fact", "preference", "habit", "event"):
        mem_type = "fact"
    if is_system_noise(content):
        return {"ok": False, "msg": "系统状态/工具报错不属于用户知识，未保存"}

    chash = _content_hash(content)
    async with SessionLocal() as db:
        existing = (
            await db.execute(
                select(KbMemoryItem).where(
                    KbMemoryItem.user_id == user_id,
                    KbMemoryItem.content_hash == chash,
                )
            )
        ).scalars().first()
        if existing is not None:
            existing.source_count += 1
            existing.confidence = min(0.99, existing.confidence + 0.05)
            await db.commit()
            return {"ok": True, "action": "reinforced", "id": existing.id, "msg": "已存在，证据 +1"}

        await _invalidate_conflicts(db, user_id, content, mem_type, datetime.now(UTC))

        item = KbMemoryItem(
            user_id=user_id,
            mem_type=mem_type,
            content=content,
            content_hash=chash,
            confidence=0.8,  # 显式保存起点置信度更高
            source_count=1,
            valid_at=datetime.now(UTC),
        )
        db.add(item)
        await db.commit()
        await db.refresh(item)
        return {"ok": True, "action": "created", "id": item.id}


async def search_memories(user_id: int, query: str, limit: int = 5) -> tuple[list[dict], str]:
    """按关键词检索当前有效记忆（v1 关键词匹配；向量检索后续版本）。"""
    from src.plugins.builtin.knowledge_base.models import KbMemoryItem, KbProfile

    query = query.strip()
    limit = max(1, min(limit, 20))
    async with SessionLocal() as db:
        stmt = (
            select(KbMemoryItem)
            .where(
                KbMemoryItem.user_id == user_id,
                KbMemoryItem.expired_at.is_(None),
                KbMemoryItem.invalid_at.is_(None),
            )
            .order_by(KbMemoryItem.source_count.desc(), KbMemoryItem.updated_at.desc())
            .limit(200)
        )
        candidates = list((await db.execute(stmt)).scalars().all())
        coverage: dict[int, float] = {}
        if query:
            words = [w for w in query.split() if w] or [query]
            hits = []
            for m in candidates:
                hit = sum(1 for w in words if w in m.content)
                if hit == 0:
                    continue  # 零命中不返回（M1 相关性纪律）
                coverage[id(m)] = hit / len(words)
                hits.append(m)
            hits.sort(key=lambda m: (-coverage[id(m)], -m.source_count))
        else:
            hits = candidates

        profile = (
            await db.execute(select(KbProfile).where(KbProfile.user_id == user_id))
        ).scalars().first()

        return [
            {
                "content": m.content,
                "type": m.mem_type,
                "confidence": round(m.confidence, 2),
                "source_count": m.source_count,
                "relevance": round(coverage.get(id(m), 1.0), 2),
            }
            for m in hits[:limit]
        ], (profile.profile_md[:600] if profile else "")


# ---------------------------------------------------------------- 事件入口

async def on_chat_message_complete(**kwargs) -> None:
    """聊天完成钩子（宿主 runner._finish_run 发射的事件）。

    处理器必须快速返回（emit 内联等待）——实际提取 spawn 后台任务执行。
    """
    user_id = kwargs.get("user_id")
    session_id = kwargs.get("session_id")
    if not user_id or not session_id:
        return

    async def _job() -> None:
        async with _running_lock:
            if session_id in _running_sessions:
                return  # 同会话去抖：并发完成只跑一个
            _running_sessions.add(session_id)
        try:
            await extract_and_store(user_id, session_id, kwargs.get("run_id"))
        except Exception as exc:
            logger.error(f"[KnowledgeBase] 记忆提取任务失败: {exc}")
        finally:
            async with _running_lock:
                _running_sessions.discard(session_id)

    asyncio.create_task(_job())
