"""推送 API（周期三）——推送知识中心 + 留痕复查 + 违规处置。

.. pattern:: 与 api_wiki 同纪律
   - 提交：空间 owner（个人库）
   - 复查登记/违规处置：knowledge_base:submission:review / :dispose 权限（维护者）
   - 无审批环节（用户决策：AI 甄选过的直接入库，留痕可复查）
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
)
from src.plugins.builtin.knowledge_base.pipeline import submission as push

router = APIRouter(prefix="/knowledge-base", tags=["ApeCortex 推送"])


# ---------------------------------------------------------------- 提交

@router.post("/spaces/{space_id}/docs/{doc_id}/submit-public")
async def submit_doc_public(
    space_id: int,
    doc_id: int,
    user: Annotated[User, Depends(get_current_user)],
    _perm: Annotated[User, Depends(_require_perm("knowledge_base:submission:submit"))],
):
    """把一份文档推送到知识中心（AI 甄选 → 无违规自动入库）。"""
    result = await push.submit_public_content(user.id, space_id, doc_id=doc_id)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("msg"))
    return success_response(data={"submission_id": result["submission_id"]}, msg=result.get("msg", "已提交"))


@router.post("/spaces/{space_id}/pages/{page_id}/submit-public")
async def submit_page_public(
    space_id: int,
    page_id: int,
    user: Annotated[User, Depends(get_current_user)],
    _perm: Annotated[User, Depends(_require_perm("knowledge_base:submission:submit"))],
):
    """把一个词条推送到知识中心（正文快照 → AI 甄选 → 自动入库）。"""
    result = await push.submit_public_content(user.id, space_id, page_id=page_id)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("msg"))
    return success_response(data={"submission_id": result["submission_id"]}, msg=result.get("msg", "已提交"))


@router.get("/submissions/mine")
async def my_submissions(
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
    _perm: Annotated[User, Depends(_require_perm("knowledge_base:submission:submit"))],
):
    """我提交的推送记录（全状态留痕）。"""
    subs = list((await db.execute(
        select(KbPublicSubmission).where(KbPublicSubmission.submitted_by == user.id)
        .order_by(KbPublicSubmission.id.desc())
        .limit(50)
    )).scalars().all())
    cat_names = await _category_names(db, subs)
    return success_response(data={
        "items": [push.submission_out(s, cat_names) for s in subs],
    })


# ---------------------------------------------------------------- 推送记录（留痕）

@router.get("/submissions")
async def list_submissions(
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
    _perm: Annotated[User, Depends(_require_perm("knowledge_base:submission:review"))],
    status: str = Query(default="all", max_length=20),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=50),
):
    """推送记录（留痕）：全状态流水；violation=待处置违规队列。"""
    if status not in ("ai_reviewing", "approved", "violation", "disposed", "all"):
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


@router.get("/submissions/{submission_id}")
async def get_submission(
    submission_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
    _perm: Annotated[User, Depends(_require_perm("knowledge_base:submission:review"))],
):
    """推送单详情（含全文，供复查细读）。"""
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


@router.post("/submissions/{submission_id}/check")
async def check_submission(
    submission_id: int,
    body: dict,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
    _perm: Annotated[User, Depends(_require_perm("knowledge_base:submission:review"))],
):
    """复查登记（留痕）：登记复查意见，不改推送结果。"""
    result = await push.register_check(
        user.id, submission_id, str(body.get("comment") or ""),
    )
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("msg"))
    return success_response(msg="复查意见已登记")


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
