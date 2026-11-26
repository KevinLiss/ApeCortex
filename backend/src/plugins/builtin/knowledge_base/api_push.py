"""公开库推送 API（N2）——提交推送单 / 审核 / 违规处置。

.. pattern:: 与 api_wiki 同纪律
   - 提交：写权限（owner/writer）
   - 审核/处置：knowledge_base:submission:review / :dispose 权限（维护者）
   - 自审制衡：不能审核自己提交的推送（管线层校验）
"""
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.deps import get_current_user
from src.core.deps import require_permission as _require_perm
from src.core.exceptions import success_response
from src.db import get_db
from src.models import User
from src.plugins.builtin.knowledge_base.models import (
    KbCategory,
    KbPublicSubmission,
    KbSpace,
)
from src.plugins.builtin.knowledge_base.pipeline import submission as push

router = APIRouter(prefix="/knowledge-base", tags=["ApeCortex 公开库推送"])


# ---------------------------------------------------------------- 提交

@router.post("/spaces/{space_id}/docs/{doc_id}/submit-public")
async def submit_public(
    space_id: int,
    doc_id: int,
    body: dict,
    user: Annotated[User, Depends(get_current_user)],
    _perm: Annotated[User, Depends(_require_perm("knowledge_base:submission:submit"))],
):
    """把一份文档推送到公开库审核流（内容快照冻结 → AI 分析 → 人审）。"""
    target_space_id = body.get("target_space_id")
    result = await push.submit_public_doc(
        user.id, space_id, doc_id,
        target_space_id=int(target_space_id) if target_space_id else None,
    )
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("msg"))
    return success_response(data={"submission_id": result["submission_id"]}, msg=result.get("msg", "已提交"))


@router.get("/submissions/mine")
async def my_submissions(
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
    _perm: Annotated[User, Depends(_require_perm("knowledge_base:submission:submit"))],
):
    """我提交的推送单（全状态）。"""
    subs = list((await db.execute(
        select(KbPublicSubmission).where(KbPublicSubmission.submitted_by == user.id)
        .order_by(KbPublicSubmission.id.desc())
        .limit(50)
    )).scalars().all())
    cat_names = await _category_names(db, subs)
    return success_response(data={
        "items": [push.submission_out(s, cat_names) for s in subs],
    })


# ---------------------------------------------------------------- 审核队列

@router.get("/submissions")
async def list_submissions(
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
    _perm: Annotated[User, Depends(_require_perm("knowledge_base:submission:review"))],
    status: str = Query(default="pending", max_length=20),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=50),
):
    """审核/处置队列（pending=待审 / violation=违规处置 / approved/rejected/disposed=历史）。"""
    if status not in ("ai_reviewing", "pending", "approved", "rejected", "violation", "disposed", "all"):
        raise HTTPException(status_code=400, detail="无效的状态过滤")

    from sqlalchemy import func

    conds = []
    if status != "all":
        conds.append(KbPublicSubmission.status == status)
    base = select(KbPublicSubmission).where(*conds)
    total = (await db.execute(
        select(func.count()).select_from(base.subquery())
    )).scalar_one()
    subs = list((await db.execute(
        base.order_by(KbPublicSubmission.id.desc())
        .offset((page - 1) * page_size).limit(page_size)
    )).scalars().all())
    cat_names = await _category_names(db, subs)
    return success_response(data={
        "items": [push.submission_out(s, cat_names) for s in subs],
        "total": total, "page": page, "page_size": page_size,
    })


async def _category_names(db: AsyncSession, subs: list) -> dict[int, str]:
    ids = {s.ai_category_id for s in subs if s.ai_category_id}
    if not ids:
        return {}
    cats = list((await db.execute(
        select(KbCategory).where(KbCategory.id.in_(ids))
    )).scalars().all())
    return {c.id: c.name for c in cats}


@router.get("/submissions/targets")
async def list_push_targets(
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
    _perm: Annotated[User, Depends(_require_perm("knowledge_base:submission:submit"))],
):
    """可选目标公开库列表（提交推送时的下拉数据）。

    附 can_create_public：有维护者权限的提交人可在推送弹窗内直接新建公开库
    （无公开库时空态引导的关键交互——修复「推送没得选」）。
    """
    spaces = list((await db.execute(
        select(KbSpace).where(
            KbSpace.visibility == "public", KbSpace.status == "active"
        ).order_by(KbSpace.id)
    )).scalars().all())
    from src.core.deps import get_user_permissions

    perms = get_user_permissions(user)
    return success_response(data={
        "items": [{"id": s.id, "name": s.name, "category_id": s.category_id} for s in spaces],
        "can_create_public": "*" in perms or "knowledge_base:promotion:review" in perms,
    })


@router.post("/submissions/targets")
async def create_push_target(
    body: dict,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
    _perm: Annotated[User, Depends(_require_perm("knowledge_base:promotion:review"))],
):
    """推送弹窗内快速新建公开库（维护者；权限与创建公开空间同源 promotion:review）。"""
    from src.plugins.builtin.knowledge_base.pipeline.ingest_api import create_space

    result = await create_space(
        user.id,
        str(body.get("name") or ""),
        str(body.get("description") or ""),
        visibility="public",
    )
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("msg"))
    space_id = result["space_id"]
    if body.get("category_id"):
        cat = (await db.execute(
            select(KbCategory).where(KbCategory.id == int(body["category_id"]))
        )).scalars().first()
        if cat is None:
            raise HTTPException(status_code=400, detail="分类不存在")
        space = (await db.execute(
            select(KbSpace).where(KbSpace.id == space_id)
        )).scalars().first()
        space.category_id = int(body["category_id"])
        await db.commit()
    return success_response(
        data={"id": space_id, "name": str(body.get("name")), "category_id": body.get("category_id")},
        msg="公开库已创建",
    )


@router.get("/submissions/{submission_id}")
async def get_submission(
    submission_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
    _perm: Annotated[User, Depends(_require_perm("knowledge_base:submission:review"))],
):
    """推送单详情（含全文，供审核员细读）。"""
    sub = (await db.execute(
        select(KbPublicSubmission).where(KbPublicSubmission.id == submission_id)
    )).scalars().first()
    if sub is None:
        raise HTTPException(status_code=404, detail="推送单不存在")
    if sub.status == "disposed" and sub.disposal_action == "deleted":
        content = ""
    else:
        content = sub.doc_content
    out = push.submission_out(sub, await _category_names(db, [sub]))
    out["doc_content"] = content
    return success_response(data=out)


@router.post("/submissions/{submission_id}/review")
async def review_push(
    submission_id: int,
    body: dict,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
    _perm: Annotated[User, Depends(_require_perm("knowledge_base:submission:review"))],
):
    """人审推送单：approve=true 通过（可改分类/目标库）→ 入公开库拆词条；false 拒绝。"""
    result = await push.review_submission(
        user.id, submission_id,
        approve=bool(body.get("approve")),
        category_id=body.get("category_id"),
        target_space_id=body.get("target_space_id"),
        comment=str(body.get("comment") or ""),
    )
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("msg"))
    msg = "已通过并入库" if result.get("status") == "approved" else "已拒绝"
    return success_response(data=result, msg=msg + (f"：{result['msg']}" if result.get("msg") else ""))


@router.post("/submissions/{submission_id}/dispose")
async def dispose_push(
    submission_id: int,
    body: dict,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
    _perm: Annotated[User, Depends(_require_perm("knowledge_base:submission:dispose"))],
):
    """违规处置：deleted=清理内容快照 / kept_evidence=保留存证。"""
    result = await push.dispose_violation(
        user.id, submission_id,
        action=str(body.get("action") or ""),
        comment=str(body.get("comment") or ""),
    )
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("msg"))
    return success_response(data=result, msg="处置完成")
