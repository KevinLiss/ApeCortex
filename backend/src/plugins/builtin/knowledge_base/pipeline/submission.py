"""推送管线（周期三）——AI 甄选自动入库 + 留痕复查 + 违规人工处置。

流程（用户决策 2026-11-26）：
    文档/词条 → 推送知识中心 → AI 甄选（1 次 LLM：
        分类建议（现有分类都不合适可给新分类名，管线自动创建）
        + 违规判定 + 拆解预览）
    ├─ 无违规 → 自动入库知识中心（快照→编译拆词条→词条挂分类）→ status=approved
    │   （reviewer_id=0 系统；留痕可复查登记，无审批环节——自审制衡随之消失）
    └─ 判违规 → violation 队列 → 人工处置（保留存证 / 清理内容快照）

支持推送词条：把私库词条正文作为内容快照推送（source_page_id 溯源）。

.. design:: 成本纪律
   - 每份推送恰好 1 次 LLM 甄选调用
   - 入库后的拆词条编译复用 wiki_ingest 任务（受 daily_job_limit 护栏）
   - AI 失败/限额不阻塞推送：内容直接入库（信任默认），AI 字段留空可事后补分析
"""
import hashlib
import json
from datetime import UTC, datetime

from loguru import logger
from sqlalchemy import select

from src.db import SessionLocal

_RISK_TYPES = ("none", "politics", "porn", "violence", "copyright", "ad", "privacy")
_RISK_LEVELS = ("none", "low", "medium", "high")

_ANALYZE_SYSTEM_PROMPT = """你是公共知识中心的推送甄选助手。对一份待推送内容做三项分析：

【任务】
1. 分类归类：从给定分类列表中选最贴切的一个（输出其 id）；
   如果所有分类都不合适，输出 new_category_name（一个简短的新分类名，将用它创建新分类）
2. 违规判定：判定内容是否含违规风险
   - 类型：none（无风险）/ politics（涉政有害）/ porn（色情低俗）/ violence（暴恐血腥）
     / copyright（侵权抄袭）/ ad（垃圾广告）/ privacy（隐私泄露，含个人敏感信息）
   - 等级：none / low / medium / high
   疑似即标出，交人工处置；不确定选低档并说明理由
3. 拆解预览：该内容适合拆出的知识词条标题列表（3~8 个，简短名词性标题）

【要求】只输出 JSON，不要任何其他文字：
{"category_id": 1, "new_category_name": "", "category_reason": "……",
 "risk_type": "none", "risk_level": "none", "risk_reason": "……",
 "split_titles": ["……", "……"]}
"""


def _analyze_user_prompt(categories: list[dict], title: str, content: str) -> str:
    cat_lines = "\n".join(f"- id={c['id']} {c['name']}" for c in categories) or "（暂无分类）"
    return f"""【分类列表】
{cat_lines}

【待甄选内容】
标题：{title}

{content[:12000]}

请按任务输出 JSON。"""


def parse_analyze_response(raw: str) -> dict:
    """解析 AI 甄选响应（宽容降级：任何字段缺失用默认值）。"""
    from src.plugins.builtin.knowledge_base.pipeline.memory import (
        _extract_outermost_json,
        _strip_code_fences,
    )

    data = None
    for attempt in (raw or "", _strip_code_fences(raw or ""), _extract_outermost_json(_strip_code_fences(raw or ""))):
        try:
            data = json.loads(attempt)
            break
        except (json.JSONDecodeError, ValueError):
            continue
    if not isinstance(data, dict):
        return {}

    risk_type = str(data.get("risk_type", "none")).lower()
    risk_level = str(data.get("risk_level", "none")).lower()
    split = data.get("split_titles")
    split_titles = [str(s).strip()[:100] for s in split if str(s).strip()][:8] if isinstance(split, list) else []
    new_cat = str(data.get("new_category_name", "") or "").strip()[:50]
    return {
        "category_id": data.get("category_id") if isinstance(data.get("category_id"), int) else None,
        "new_category_name": new_cat or None,
        "category_reason": str(data.get("category_reason", ""))[:500],
        "risk_type": risk_type if risk_type in _RISK_TYPES else "none",
        "risk_level": risk_level if risk_level in _RISK_LEVELS else "none",
        "risk_reason": str(data.get("risk_reason", ""))[:500],
        "split_titles": split_titles,
    }


async def submit_public_content(
    user_id: int,
    space_id: int,
    doc_id: int | None = None,
    page_id: int | None = None,
) -> dict:
    """创建推送单（内容快照冻结）→ 触发 AI 甄选任务。

    doc_id 与 page_id 二选一：文档推送（原始料）或词条推送（成品知识）。
    """
    from src.plugins.builtin.knowledge_base.models import (
        KbDocSource,
        KbPublicSubmission,
        KbWikiPage,
    )
    from src.plugins.builtin.knowledge_base.pipeline.ingest_api import ensure_space_access
    from src.plugins.builtin.knowledge_base.pipeline.jobs import enqueue_job

    ok, msg = await ensure_space_access(user_id, space_id, need_write=True)
    if not ok:
        return {"ok": False, "msg": msg}

    async with SessionLocal() as db:
        title, content = "", ""
        if doc_id is not None:
            doc = (await db.execute(
                select(KbDocSource).where(
                    KbDocSource.id == doc_id, KbDocSource.space_id == space_id
                )
            )).scalars().first()
            if doc is None:
                return {"ok": False, "msg": "文档不存在"}
            title, content = doc.title, doc.content
        elif page_id is not None:
            page = (await db.execute(
                select(KbWikiPage).where(
                    KbWikiPage.id == page_id, KbWikiPage.space_id == space_id
                )
            )).scalars().first()
            if page is None:
                return {"ok": False, "msg": "词条不存在"}
            title, content = page.title, page.body_md
        else:
            return {"ok": False, "msg": "缺少推送对象（doc_id / page_id）"}

        chash = hashlib.blake2b(content.encode("utf-8"), digest_size=24).hexdigest()

        # 幂等：同内容未处置完/已入库的推送单不重复建
        dup = (await db.execute(
            select(KbPublicSubmission).where(
                KbPublicSubmission.content_hash == chash,
                KbPublicSubmission.status.in_(("ai_reviewing", "approved", "violation")),
            )
        )).scalars().first()
        if dup is not None:
            return {"ok": False, "msg": f"该内容已有在途/已入库推送单（#{dup.id}）"}

        sub = KbPublicSubmission(
            source_space_id=space_id,
            source_doc_id=doc_id or 0,
            source_page_id=page_id or 0,
            doc_title=title,
            doc_content=content,
            content_hash=chash,
            submitted_by=user_id,
            status="ai_reviewing",
        )
        db.add(sub)
        await db.commit()
        await db.refresh(sub)
        submission_id = sub.id

    try:
        await enqueue_job(user_id, "public_submission", {"submission_id": submission_id}, space_id=space_id)
    except RuntimeError as exc:
        # 限额：跳过 AI 甄选直接入库（信任默认，留痕可复查）
        logger.warning(f"[KnowledgeBase] 推送甄选未入队（当日限额）: {exc}")
        return await _auto_approve(submission_id, ai_skipped=True)
    return {"ok": True, "submission_id": submission_id, "msg": "已提交，AI 甄选中"}


async def analyze_submission(submission_id: int) -> dict:
    """AI 甄选一份推送单（1 次 LLM 调用）→ 无违规自动入库 / 判违规进处置队列。

    原子认领（CAS）：``ai_reviewing → analyzing``，防 job worker 与手动触发并发双跑。
    """
    from sqlalchemy import update

    from src.plugins.builtin.knowledge_base.models import KbPublicSubmission
    from src.plugins.builtin.knowledge_base.pipeline.memory import _call_llm_json

    # 原子认领：仅第一个执行方能推进，其余直接拒绝
    async with SessionLocal() as db:
        res = await db.execute(
            update(KbPublicSubmission)
            .where(
                KbPublicSubmission.id == submission_id,
                KbPublicSubmission.status == "ai_reviewing",
            )
            .values(status="analyzing")
        )
        await db.commit()
        if res.rowcount == 0:
            return {"ok": False, "msg": "推送单不存在或已在处理"}

    async with SessionLocal() as db:
        sub = (await db.execute(
            select(KbPublicSubmission).where(KbPublicSubmission.id == submission_id)
        )).scalars().first()
        if sub is None:
            return {"ok": False, "msg": "推送单不存在"}
        title, content = sub.doc_title, sub.doc_content
    categories = await _get_categories()
    raw = await _call_llm_json(
        _ANALYZE_SYSTEM_PROMPT,
        _analyze_user_prompt(categories, title, content),
        max_tokens=800,
        temperature=0.1,
    )
    result = parse_analyze_response(raw or "")

    if not result:
        # AI 失败：信任默认直接入库（留痕可复查）
        return await _auto_approve(submission_id, ai_skipped=True)

    async with SessionLocal() as db:
        sub = (await db.execute(
            select(KbPublicSubmission).where(KbPublicSubmission.id == submission_id)
        )).scalars().first()
        if sub is None:
            return {"ok": False, "msg": "推送单不存在"}
        sub.ai_category_reason = result["category_reason"]
        sub.ai_risk_type = result["risk_type"]
        sub.ai_risk_level = result["risk_level"]
        sub.ai_split_preview = json.dumps(result["split_titles"], ensure_ascii=False)
        if result.get("risk_reason"):
            sub.ai_category_reason = (
                f"{result['category_reason']}｜风险说明:{result['risk_reason']}"[:500]
            )
        await db.commit()

    is_violation = result["risk_type"] != "none" and result["risk_level"] != "none"
    if is_violation:
        async with SessionLocal() as db:
            sub = (await db.execute(
                select(KbPublicSubmission).where(KbPublicSubmission.id == submission_id)
            )).scalars().first()
            sub.status = "violation"
            await db.commit()
        logger.info(
            f"[KnowledgeBase] submission#{submission_id} AI 判违规: "
            f"{result['risk_type']}/{result['risk_level']} → 处置队列"
        )
        return {"ok": True, "status": "violation"}

    # 无违规 → AI 归类（可能建新分类）→ 自动入库
    category_id = await _resolve_category(result, {c["id"] for c in categories})
    return await _auto_approve(submission_id, category_id=category_id)


async def _resolve_category(result: dict, valid_ids: set[int]) -> int | None:
    """AI 归类：优先用建议分类 id；无合适时用新分类名创建（AI 自主维护分类）。"""
    from src.plugins.builtin.knowledge_base.pipeline.hub import ai_ensure_category

    if result.get("category_id") in valid_ids:
        return result["category_id"]
    new_name = result.get("new_category_name")
    if new_name:
        return await ai_ensure_category(new_name)
    return None


async def _auto_approve(
    submission_id: int,
    category_id: int | None = None,
    ai_skipped: bool = False,
) -> dict:
    """自动入库知识中心（无审批环节）：快照写入 hub space → 触发编译拆词条 → 留痕。"""
    from src.plugins.builtin.knowledge_base.models import (
        KbDocSource,
        KbPublicSubmission,
    )
    from src.plugins.builtin.knowledge_base.pipeline.hub import get_hub_space_id

    async with SessionLocal() as db:
        sub = (await db.execute(
            select(KbPublicSubmission).where(KbPublicSubmission.id == submission_id)
        )).scalars().first()
        if sub is None:
            return {"ok": False, "msg": "推送单不存在"}
        if sub.status not in ("ai_reviewing", "analyzing"):  # ai_reviewing=限额直入；analyzing=CAS 认领后
            return {"ok": False, "msg": f"推送单状态为 {sub.status}，不可入库"}
        if category_id is not None:
            sub.ai_category_id = category_id
        sub.status = "approved"
        sub.reviewer_id = 0  # 系统自动（无人工审批）
        sub.review_comment = "AI 甄选通过，自动入库" if not ai_skipped else "AI 甄选跳过（限额），直接入库"
        sub.reviewed_at = datetime.now(UTC)
        doc_title, doc_content = sub.doc_title, sub.doc_content
        await db.commit()

    hub_id = await get_hub_space_id()
    chash = hashlib.blake2b(doc_content.encode("utf-8"), digest_size=24).hexdigest()

    # 快照写入知识中心（同内容幂等）
    async with SessionLocal() as db:
        dup = (await db.execute(
            select(KbDocSource).where(
                KbDocSource.space_id == hub_id, KbDocSource.content_hash == chash
            )
        )).scalars().first()
        if dup is None:
            doc = KbDocSource(
                space_id=hub_id,
                title=doc_title,
                content=doc_content,
                content_hash=chash,
                source_type="pushed",
                char_count=len(doc_content),
            )
            db.add(doc)
            await db.commit()
            await db.refresh(doc)
            new_doc_id = doc.id
        else:
            new_doc_id = dup.id

    # 触发知识中心编译拆词条（受 daily_job_limit 护栏；超限不回滚入库，留痕提示）
    job_msg = ""
    from src.plugins.builtin.knowledge_base.pipeline.jobs import enqueue_job

    try:
        job_payload = {"space_id": hub_id, "doc_ids": [new_doc_id], "category_id": category_id}
        await enqueue_job(0, "wiki_ingest", job_payload, space_id=hub_id)
    except RuntimeError as exc:
        job_msg = f"（当日任务限额，稍后可在知识中心手动重编: {exc}）"
    logger.info(f"[KnowledgeBase] submission#{submission_id} 自动入库 hub doc={new_doc_id} {job_msg}")
    return {"ok": True, "status": "approved", "doc_id": new_doc_id, "msg": job_msg.strip("（）") if job_msg else ""}


async def _get_categories() -> list[dict]:
    from src.plugins.builtin.knowledge_base.models import KbCategory

    async with SessionLocal() as db:
        cats = list((await db.execute(
            select(KbCategory).order_by(KbCategory.sort, KbCategory.id)
        )).scalars().all())
        return [{"id": c.id, "name": c.name} for c in cats]


async def register_check(
    reviewer_id: int, submission_id: int, comment: str,
) -> dict:
    """复查登记（留痕）：对已定论单子登记人工复查意见，不改结果。"""
    from src.plugins.builtin.knowledge_base.models import KbPublicSubmission

    async with SessionLocal() as db:
        sub = (await db.execute(
            select(KbPublicSubmission).where(KbPublicSubmission.id == submission_id)
        )).scalars().first()
        if sub is None:
            return {"ok": False, "msg": "推送单不存在"}
        if sub.status in ("ai_reviewing",):
            return {"ok": False, "msg": "该单尚未定论，无需复查登记"}
        sub.check_comment = (comment or "")[:500]
        sub.checked_by = reviewer_id
        sub.checked_at = datetime.now(UTC)
        await db.commit()
        return {"ok": True}


async def dispose_violation(
    reviewer_id: int, submission_id: int, action: str, comment: str = "",
) -> dict:
    """违规处置：deleted=清理内容快照（记录行保留） / kept_evidence=保留存证。"""
    from src.plugins.builtin.knowledge_base.models import KbPublicSubmission

    if action not in ("deleted", "kept_evidence"):
        return {"ok": False, "msg": "处置动作仅支持 deleted / kept_evidence"}

    async with SessionLocal() as db:
        sub = (await db.execute(
            select(KbPublicSubmission).where(KbPublicSubmission.id == submission_id)
        )).scalars().first()
        if sub is None:
            return {"ok": False, "msg": "推送单不存在"}
        if sub.status != "violation":
            return {"ok": False, "msg": f"推送单状态为 {sub.status}，仅违规单可处置"}

        sub.status = "disposed"
        sub.reviewer_id = reviewer_id
        sub.disposal_action = action
        sub.review_comment = (comment or ("已清理" if action == "deleted" else "保留存证"))[:500]
        sub.disposed_at = datetime.now(UTC)
        if action == "deleted":
            sub.doc_content = ""  # 清理内容快照，仅保留记录行
        await db.commit()
        return {"ok": True, "action": action}


def submission_out(sub, category_names: dict[int, str] | None = None) -> dict:
    """推送单序列化（留痕记录）。"""
    names = category_names or {}
    try:
        split = json.loads(sub.ai_split_preview or "[]")
    except (json.JSONDecodeError, TypeError):
        split = []
    return {
        "id": sub.id,
        "source_space_id": sub.source_space_id,
        "source_doc_id": sub.source_doc_id,
        "source_page_id": getattr(sub, "source_page_id", 0),
        "doc_title": sub.doc_title,
        "char_count": len(sub.doc_content or ""),
        "status": sub.status,
        "ai_category_id": sub.ai_category_id,
        "ai_category_name": names.get(sub.ai_category_id, ""),
        "ai_category_reason": sub.ai_category_reason,
        "ai_risk_type": sub.ai_risk_type,
        "ai_risk_level": sub.ai_risk_level,
        "ai_split_preview": split,
        "submitted_by": sub.submitted_by,
        "reviewer_id": sub.reviewer_id,
        "review_comment": sub.review_comment,
        "check_comment": getattr(sub, "check_comment", "") or "",
        "checked_by": getattr(sub, "checked_by", None),
        "reviewed_at": sub.reviewed_at.isoformat() if sub.reviewed_at else None,
        "disposal_action": sub.disposal_action,
        "disposed_at": sub.disposed_at.isoformat() if sub.disposed_at else None,
        "created_at": sub.created_at.isoformat() if sub.created_at else None,
        "content_preview": (sub.doc_content or "")[:300],
    }
