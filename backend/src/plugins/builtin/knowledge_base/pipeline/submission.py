"""公开库推送管线（N2）——AI 分类/违规初筛 + 人审 + 入库拆词条。

.. design:: 成本纪律
   - 每份推送恰好 1 次 LLM 分析调用（分类建议 + 违规判定 + 拆解预览合并产出）
   - 任务入队受 jobs.daily_job_limit 护栏管控
   - AI 失败不阻塞人审：状态落回 pending（ai_* 字段为空），审核员可照常处理

.. design:: 安全纪律（用户决策 D2：违规留存待人工处置）
   - AI 判违规 → status=violation，内容与判定依据留存，进违规处置队列
   - 处置动作：deleted（清理内容快照，保留记录行）/ kept_evidence（保留存证）
   - 内容快照与源文档解耦：源文档删除后审核/处置照常可读
"""
import hashlib
import json
from datetime import UTC, datetime

from loguru import logger
from sqlalchemy import select

from src.db import SessionLocal

_RISK_TYPES = ("none", "politics", "porn", "violence", "copyright", "ad", "privacy")
_RISK_LEVELS = ("none", "low", "medium", "high")

_ANALYZE_SYSTEM_PROMPT = """你是公开知识库的推送审核助手。对一份待推送文档做三项分析：

【任务】
1. 分类建议：从给定分类列表中选最贴切的一个（输出其 id；都不合适输出 null）
2. 违规判定：判定内容是否含违规风险
   - 类型：none（无风险）/ politics（涉政有害）/ porn（色情低俗）/ violence（暴恐血腥）
     / copyright（侵权抄袭）/ ad（垃圾广告）/ privacy（隐私泄露，含个人敏感信息）
   - 等级：none / low（轻微，可人工复核）/ medium（较明显）/ high（严重）
   疑似即标出，最终由人工处置；不确定选低档并说明理由
3. 拆解预览：该文档适合拆出的知识词条标题列表（3~8 个，简短名词性标题）

【要求】只输出 JSON，不要任何其他文字：
{"category_id": 1, "category_reason": "……", "risk_type": "none", "risk_level": "none",
 "risk_reason": "……", "split_titles": ["……", "……"]}
"""


def _analyze_user_prompt(categories: list[dict], title: str, content: str) -> str:
    cat_lines = "\n".join(f"- id={c['id']} {c['name']}" for c in categories) or "（暂无分类）"
    return f"""【分类列表】
{cat_lines}

【待审文档】
标题：{title}

{content[:12000]}

请按任务输出 JSON。"""


def parse_analyze_response(raw: str) -> dict:
    """解析 AI 分析响应（宽容降级：任何字段缺失用默认值）。"""
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
    return {
        "category_id": data.get("category_id") if isinstance(data.get("category_id"), int) else None,
        "category_reason": str(data.get("category_reason", ""))[:500],
        "risk_type": risk_type if risk_type in _RISK_TYPES else "none",
        "risk_level": risk_level if risk_level in _RISK_LEVELS else "none",
        "risk_reason": str(data.get("risk_reason", ""))[:500],
        "split_titles": split_titles,
    }


async def _get_categories() -> list[dict]:
    from src.plugins.builtin.knowledge_base.models import KbCategory

    async with SessionLocal() as db:
        cats = list((await db.execute(
            select(KbCategory).order_by(KbCategory.sort, KbCategory.id)
        )).scalars().all())
        return [{"id": c.id, "name": c.name} for c in cats]


async def submit_public_doc(
    user_id: int, space_id: int, doc_id: int, target_space_id: int | None = None,
) -> dict:
    """创建推送单（内容快照冻结）→ 触发 AI 分析任务。"""
    from src.plugins.builtin.knowledge_base.models import KbDocSource, KbPublicSubmission, KbSpace
    from src.plugins.builtin.knowledge_base.pipeline.ingest_api import ensure_space_access
    from src.plugins.builtin.knowledge_base.pipeline.jobs import enqueue_job

    ok, msg = await ensure_space_access(user_id, space_id, need_write=True)
    if not ok:
        return {"ok": False, "msg": msg}

    async with SessionLocal() as db:
        doc = (await db.execute(
            select(KbDocSource).where(
                KbDocSource.id == doc_id, KbDocSource.space_id == space_id
            )
        )).scalars().first()
        if doc is None:
            return {"ok": False, "msg": "文档不存在"}
        chash = hashlib.blake2b(doc.content.encode("utf-8"), digest_size=24).hexdigest()

        # 目标库校验：必须是公开库
        if target_space_id is not None:
            target = (await db.execute(
                select(KbSpace).where(KbSpace.id == target_space_id)
            )).scalars().first()
            if target is None or target.visibility != "public":
                return {"ok": False, "msg": "目标库必须是公开知识库"}

        # 幂等：同内容未处置完的推送单不重复建
        dup = (await db.execute(
            select(KbPublicSubmission).where(
                KbPublicSubmission.content_hash == chash,
                KbPublicSubmission.status.in_(("ai_reviewing", "pending", "violation")),
            )
        )).scalars().first()
        if dup is not None:
            return {"ok": False, "msg": f"该内容已有在途推送单（#{dup.id}），请等待处置完成"}

        sub = KbPublicSubmission(
            source_space_id=space_id,
            source_doc_id=doc_id,
            doc_title=doc.title,
            doc_content=doc.content,
            content_hash=chash,
            target_space_id=target_space_id,
            submitted_by=user_id,
            status="ai_reviewing",
        )
        db.add(sub)
        await db.commit()
        await db.refresh(sub)
        submission_id = sub.id

    # 触发 AI 分析任务（护栏超限时单子已建，仍可人审）
    try:
        await enqueue_job(user_id, "public_submission", {"submission_id": submission_id}, space_id=space_id)
    except RuntimeError as exc:
        logger.warning(f"[KnowledgeBase] 推送分析未入队（当日限额）: {exc}")
        async with SessionLocal() as db:
            sub = (await db.execute(
                select(KbPublicSubmission).where(KbPublicSubmission.id == submission_id)
            )).scalars().first()
            if sub is not None and sub.status == "ai_reviewing":
                sub.status = "pending"  # 限额：跳过 AI 直接入人审
                await db.commit()
        return {"ok": True, "submission_id": submission_id, "msg": "已提交（当日任务限额，直接进入人工审核）"}
    return {"ok": True, "submission_id": submission_id, "msg": "已提交，AI 分析中"}


async def analyze_submission(submission_id: int) -> dict:
    """AI 分析一份推送单：分类建议 + 违规判定 + 拆解预览（1 次 LLM 调用）。"""
    from src.plugins.builtin.knowledge_base.models import KbPublicSubmission
    from src.plugins.builtin.knowledge_base.pipeline.memory import _call_llm_json

    async with SessionLocal() as db:
        sub = (await db.execute(
            select(KbPublicSubmission).where(KbPublicSubmission.id == submission_id)
        )).scalars().first()
        if sub is None or sub.status != "ai_reviewing":
            return {"ok": False, "msg": "推送单不存在或非待分析状态"}

    categories = await _get_categories()
    raw = await _call_llm_json(
        _ANALYZE_SYSTEM_PROMPT,
        _analyze_user_prompt(categories, sub.doc_title, sub.doc_content),
        max_tokens=800,
        temperature=0.1,
    )
    result = parse_analyze_response(raw or "")
    valid_cat_ids = {c["id"] for c in categories}

    async with SessionLocal() as db:
        sub = (await db.execute(
            select(KbPublicSubmission).where(KbPublicSubmission.id == submission_id)
        )).scalars().first()
        if sub is None:
            return {"ok": False, "msg": "推送单不存在"}
        if result:
            sub.ai_category_id = (
                result["category_id"] if result["category_id"] in valid_cat_ids else None
            )
            sub.ai_category_reason = result["category_reason"]
            sub.ai_risk_type = result["risk_type"]
            sub.ai_risk_level = result["risk_level"]
            sub.ai_split_preview = json.dumps(result["split_titles"], ensure_ascii=False)
            if result.get("risk_reason"):
                sub.ai_category_reason = (
                    f"{result['category_reason']}｜风险说明:{result['risk_reason']}"[:500]
                )
            sub.status = (
                "violation"
                if result["risk_type"] != "none" and result["risk_level"] != "none"
                else "pending"
            )
        else:
            sub.status = "pending"  # AI 失败 → 人工兜底
        await db.commit()
        logger.info(
            f"[KnowledgeBase] submission#{submission_id} AI 分析完成: "
            f"cat={sub.ai_category_id} risk={sub.ai_risk_type}/{sub.ai_risk_level} status={sub.status}"
        )
        return {"ok": True, "status": sub.status}


async def review_submission(
    reviewer_id: int, submission_id: int, approve: bool,
    category_id: int | None = None, target_space_id: int | None = None,
    comment: str = "",
) -> dict:
    """人审（pending 单）：通过→快照文档拷入目标公开库并编译拆词条；拒绝→退回。"""
    from src.plugins.builtin.knowledge_base.models import (
        KbCategory,
        KbDocSource,
        KbPublicSubmission,
        KbSpace,
    )

    async with SessionLocal() as db:
        sub = (await db.execute(
            select(KbPublicSubmission).where(KbPublicSubmission.id == submission_id)
        )).scalars().first()
        if sub is None:
            return {"ok": False, "msg": "推送单不存在"}
        if sub.status != "pending":
            return {"ok": False, "msg": f"推送单状态为 {sub.status}，仅待审核单可处理"}
        if sub.submitted_by == reviewer_id:
            return {"ok": False, "msg": "不能审核自己提交的推送"}

        if not approve:
            sub.status = "rejected"
            sub.reviewer_id = reviewer_id
            sub.review_comment = (comment or "不通过")[:500]
            sub.reviewed_at = datetime.now(UTC)
            await db.commit()
            return {"ok": True, "status": "rejected"}

        # 通过：确定目标公开库（审核员可改；缺省用单上目标或第一个公开库）
        target_id = target_space_id or sub.target_space_id
        if target_id is None:
            first_public = (await db.execute(
                select(KbSpace).where(KbSpace.visibility == "public", KbSpace.status == "active")
                .order_by(KbSpace.id)
            )).scalars().first()
            target_id = first_public.id if first_public else None
        if target_id is None:
            return {"ok": False, "msg": "没有可用的公开知识库，请先创建"}
        target = (await db.execute(
            select(KbSpace).where(KbSpace.id == target_id)
        )).scalars().first()
        if target is None or target.visibility != "public":
            return {"ok": False, "msg": "目标库必须是公开知识库"}

        # 审核员指定的分类挂到目标库
        if category_id is not None:
            cat = (await db.execute(
                select(KbCategory).where(KbCategory.id == category_id)
            )).scalars().first()
            if cat is None:
                return {"ok": False, "msg": "分类不存在"}
            target.category_id = category_id
        elif sub.ai_category_id:
            target.category_id = sub.ai_category_id

        doc_title = sub.doc_title
        doc_content = sub.doc_content
        sub.status = "approved"
        sub.reviewer_id = reviewer_id
        sub.review_comment = (comment or "通过")[:500]
        sub.reviewed_at = datetime.now(UTC)
        sub.target_space_id = target_id
        await db.commit()

    # 文档快照直接写入目标公开库（审核流写入通道，与晋升流同纪律：不经
    # ensure_space_access，公开库写入只允许晋升/审核流——本函数即审核流本身）
    async with SessionLocal() as db:
        dup = (await db.execute(
            select(KbDocSource).where(
                KbDocSource.space_id == target_id,
                KbDocSource.content_hash == hashlib.blake2b(doc_content.encode("utf-8"), digest_size=24).hexdigest(),
            )
        )).scalars().first()
        if dup is not None:
            return {"ok": True, "status": "approved", "doc_id": dup.id, "msg": "目标库已有相同内容，跳过重复入库"}
        doc = KbDocSource(
            space_id=target_id,
            title=doc_title,
            content=doc_content,
            content_hash=hashlib.blake2b(doc_content.encode("utf-8"), digest_size=24).hexdigest(),
            source_type="pushed",
            char_count=len(doc_content),
        )
        db.add(doc)
        await db.commit()
        await db.refresh(doc)
        new_doc_id = doc.id

    # 触发公开库拆词条编译（受 daily_job_limit 护栏；超限不回滚审核，仅提示稍后重编）
    from src.plugins.builtin.knowledge_base.pipeline.jobs import enqueue_job

    job_msg = ""
    try:
        job_payload = {"space_id": target_id, "doc_ids": [new_doc_id]}
        await enqueue_job(reviewer_id, "wiki_ingest", job_payload, space_id=target_id)
    except RuntimeError as exc:
        job_msg = f"（当日任务限额，稍后可在公开库手动重新编译: {exc}）"
    logger.info(f"[KnowledgeBase] submission#{submission_id} 已入库 target={target_id} doc={new_doc_id} {job_msg}")
    return {"ok": True, "status": "approved", "doc_id": new_doc_id, "msg": job_msg.strip("（）") if job_msg else ""}


async def dispose_violation(
    reviewer_id: int, submission_id: int, action: str, comment: str = "",
) -> dict:
    """违规处置（violation 单）：deleted=清理内容快照 / kept_evidence=保留存证。"""
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
    """推送单序列化。"""
    names = category_names or {}
    try:
        split = json.loads(sub.ai_split_preview or "[]")
    except (json.JSONDecodeError, TypeError):
        split = []
    return {
        "id": sub.id,
        "source_space_id": sub.source_space_id,
        "source_doc_id": sub.source_doc_id,
        "doc_title": sub.doc_title,
        "char_count": len(sub.doc_content or ""),
        "target_space_id": sub.target_space_id,
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
        "reviewed_at": sub.reviewed_at.isoformat() if sub.reviewed_at else None,
        "disposal_action": sub.disposal_action,
        "disposed_at": sub.disposed_at.isoformat() if sub.disposed_at else None,
        "created_at": sub.created_at.isoformat() if sub.created_at else None,
        "content_preview": (sub.doc_content or "")[:300],
    }
