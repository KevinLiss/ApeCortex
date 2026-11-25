"""知识库 Wiki 层 REST API（P1）：空间/词条/晋升/任务。

.. pattern:: 与 P0 api.py 同纪律
   - 行级隔离：私有内容强制 owner 过滤
   - 公开库写入硬校验：非晋升流、非维护者的写操作一律 403
   - 响应解包（前端教训）：全部走统一信封
"""
import json
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.deps import get_current_user
from src.core.deps import require_permission as _require_perm
from src.core.exceptions import success_response
from src.db import get_db
from src.models import User
from src.plugins.builtin.knowledge_base.models import (
    KbPromotion,
    KbSpace,
    KbWikiPage,
    KbWikiRevision,
)
from src.plugins.builtin.knowledge_base.pipeline import jobs as jobs_mod
from src.plugins.builtin.knowledge_base.pipeline.compiler import (
    publish_page,
    review_promotion,
    submit_promotion,
)
from src.plugins.builtin.knowledge_base.pipeline.ingest_api import (
    create_space,
    ensure_owned_space,
    ingest_doc,
    list_spaces,
)

router = APIRouter(prefix="/knowledge-base", tags=["AI 知识库"])


# ---------------------------------------------------------------- 空间

@router.get("/spaces")
async def get_spaces(
    user: Annotated[User, Depends(get_current_user)],
    _perm: Annotated[User, Depends(_require_perm("knowledge_base:space:list"))],
):
    """空间列表：自己的 + 公开的。"""
    return success_response(data={"items": await list_spaces(user.id)})


@router.post("/spaces")
async def post_space(
    body: dict,
    user: Annotated[User, Depends(get_current_user)],
    _perm: Annotated[User, Depends(_require_perm("knowledge_base:space:create"))],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """创建私有空间。"""
    result = await create_space(
        user.id, body.get("name", ""), body.get("description", ""), "private"
    )
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("msg"))
    return success_response(data=result, msg="空间已创建")


@router.post("/spaces/{space_id}/docs")
async def post_doc(
    space_id: int,
    body: dict,
    user: Annotated[User, Depends(get_current_user)],
    _perm: Annotated[User, Depends(_require_perm("knowledge_base:space:create"))],
):
    """文档入库（私有空间 owner）→ 触发后台编译。"""
    result = await ingest_doc(
        user.id, space_id, body.get("title", ""), body.get("content", ""),
        body.get("source_type", "upload"), body.get("url"),
    )
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("msg"))
    return success_response(data=result)


# ---------------------------------------------------------------- 词条

@router.get("/spaces/{space_id}/pages")
async def get_pages(
    space_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
    _perm: Annotated[User, Depends(_require_perm("knowledge_base:page:list"))],
    status: str | None = Query(default=None, description="candidate/draft/live/archived"),
    q: str | None = Query(default=None, max_length=100),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    """空间词条列表（自己的空间全状态可见；公开空间只看 live/draft）。"""
    space = await db.get(KbSpace, space_id)
    if space is None or space.status != "active":
        raise HTTPException(status_code=404, detail="空间不存在")
    owned = space.owner_user_id == user.id
    if not owned and space.visibility != "public":
        raise HTTPException(status_code=403, detail="无权访问该空间")

    conditions = [KbWikiPage.space_id == space_id, KbWikiPage.is_deleted == False]  # noqa: E712
    if not owned:
        conditions.append(KbWikiPage.status.in_(("live", "draft")))
    if status:
        conditions.append(KbWikiPage.status == status)
    if q:
        conditions.append(KbWikiPage.title.ilike(f"%{q}%"))

    stmt = (
        select(KbWikiPage).where(*conditions)
        .order_by(KbWikiPage.source_count.desc(), KbWikiPage.updated_at.desc())
        .offset((page - 1) * page_size).limit(page_size)
    )
    items = (await db.execute(stmt)).scalars().all()
    total = (await db.execute(
        select(func.count()).select_from(KbWikiPage).where(*conditions)
    )).scalar_one()

    return success_response(data={
        "total": total, "page": page, "page_size": page_size,
        "items": [_page_brief(p) for p in items],
    })


def _page_brief(p) -> dict:
    return {
        "id": p.id, "slug": p.slug, "title": p.title,
        "status": p.status, "version": p.version,
        "confidence": p.confidence, "source_count": p.source_count,
        "in_links": json.loads(p.in_links or "[]"),
        "out_links": json.loads(p.out_links or "[]"),
        "origin_space_id": p.origin_space_id, "origin_page_id": p.origin_page_id,
        "origin_author_id": p.origin_author_id,
        "updated_at": p.updated_at.isoformat() if p.updated_at else None,
    }


@router.get("/pages/{page_id}")
async def get_page(
    page_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
    _perm: Annotated[User, Depends(_require_perm("knowledge_base:page:list"))],
):
    """词条详情：自己空间=全状态；公开空间=live/draft；他私库=404。"""
    p = await db.get(KbWikiPage, page_id)
    if p is None or p.is_deleted:
        raise HTTPException(status_code=404, detail="词条不存在")
    space = await db.get(KbSpace, p.space_id)
    owned = space is not None and space.owner_user_id == user.id
    if not owned:
        if space is None or space.visibility != "public":
            raise HTTPException(status_code=404, detail="词条不存在")
        if p.status not in ("live", "draft"):
            raise HTTPException(status_code=404, detail="词条不存在")
    return success_response(data={
        **_page_brief(p),
        "body_md": p.body_md,
        "source_refs": json.loads(p.source_refs or "[]"),
        "space": {"id": space.id, "name": space.name, "visibility": space.visibility},
    })


@router.put("/pages/{page_id}")
async def put_page(
    page_id: int,
    body: dict,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
    _perm: Annotated[User, Depends(_require_perm("knowledge_base:page:edit"))],
):
    """编辑词条（乐观锁：body.expected_version 与库不匹配返回 409）。

    公开库词条：仅该空间 owner（维护者）可编辑；私有：owner。
    人工编辑保护：last_edit_source=user，revision 记录人工修订（不参与机器裁剪）。
    """
    p = await db.get(KbWikiPage, page_id)
    if p is None or p.is_deleted:
        raise HTTPException(status_code=404, detail="词条不存在")
    space = await db.get(KbSpace, p.space_id)
    if space is None or space.owner_user_id != user.id:
        raise HTTPException(status_code=403, detail="仅空间所有者可编辑词条")

    expected = body.get("expected_version")
    if expected is not None and expected != p.version:
        raise HTTPException(status_code=409, detail="版本冲突，请刷新后重试")

    new_body = body.get("body_md")
    if new_body is not None and new_body.strip() and new_body.strip() != p.body_md:
        p.body_md = new_body.strip()
        p.version += 1
        p.last_edit_source = "user"
        db.add(KbWikiRevision(
            page_id=p.id, version=p.version, body_md=p.body_md,
            edit_source="user", editor_id=user.id,
            is_machine_snapshot=False, note="人工编辑",
        ))
    await db.commit()
    return success_response(data={"version": p.version}, msg="已保存")


@router.get("/pages/{page_id}/revisions")
async def get_revisions(
    page_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
    _perm: Annotated[User, Depends(_require_perm("knowledge_base:page:list"))],
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=50),
):
    """修订历史（可见性同词条详情）。"""
    p = await db.get(KbWikiPage, page_id)
    if p is None or p.is_deleted:
        raise HTTPException(status_code=404, detail="词条不存在")
    space = await db.get(KbSpace, p.space_id)
    if space is None or (space.owner_user_id != user.id and space.visibility != "public"):
        raise HTTPException(status_code=404, detail="词条不存在")

    rows = list((
        await db.execute(
            select(KbWikiRevision).where(KbWikiRevision.page_id == page_id)
            .order_by(KbWikiRevision.version.desc())
            .offset((page - 1) * page_size).limit(page_size)
        )
    ).scalars().all())
    return success_response(data={"items": [
        {
            "version": r.version, "edit_source": r.edit_source,
            "editor_id": r.editor_id, "is_machine_snapshot": r.is_machine_snapshot,
            "note": r.note, "body_md": r.body_md,
        } for r in rows
    ]})


@router.post("/pages/{page_id}/publish")
async def post_publish(
    page_id: int,
    body: dict,
    user: Annotated[User, Depends(get_current_user)],
    _perm: Annotated[User, Depends(_require_perm("knowledge_base:page:publish"))],
):
    """词条状态流转（发布 live/下线 archived/转草稿 draft）。"""
    result = await publish_page(page_id, user.id, body.get("status", "live"))
    if not result.get("ok"):
        raise HTTPException(status_code=403, detail=result.get("msg"))
    return success_response(data=result, msg="状态已更新")


# ---------------------------------------------------------------- 晋升流

@router.post("/promotions")
async def post_promotion(
    body: dict,
    user: Annotated[User, Depends(get_current_user)],
    _perm: Annotated[User, Depends(_require_perm("knowledge_base:promotion:submit"))],
):
    """提交晋升申请：私库词条 → 公开空间（冻结快照）。"""
    result = await submit_promotion(
        body.get("page_id", 0), user.id,
        body.get("target_space_id", 0), body.get("submit_note", ""),
    )
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("msg"))
    return success_response(data=result, msg="晋升申请已提交")


@router.get("/promotions")
async def get_promotions(
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
    _perm: Annotated[User, Depends(_require_perm("knowledge_base:promotion:review"))],
    status: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    """晋升申请列表（审核视角）。"""
    conditions = []
    if status:
        conditions.append(KbPromotion.status == status)
    stmt = (
        select(KbPromotion)
        .where(*conditions)
        .order_by(KbPromotion.id.desc())
        .offset((page - 1) * page_size).limit(page_size)
    )
    items = (await db.execute(stmt)).scalars().all()
    total = (await db.execute(
        select(func.count()).select_from(KbPromotion).where(*conditions)
    )).scalar_one()
    return success_response(data={
        "total": total,
        "items": [{
            "id": r.id, "page_id": r.page_id, "source_space_id": r.source_space_id,
            "target_space_id": r.target_space_id, "status": r.status,
            "snapshot_slug": r.snapshot_slug, "snapshot_title": r.snapshot_title,
            "snapshot_md": r.snapshot_md, "submit_note": r.submit_note,
            "submitted_by": r.submitted_by, "review_comment": r.review_comment,
            "published_page_id": r.published_page_id,
        } for r in items],
    })


@router.get("/promotions/mine")
async def get_my_promotions(
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
    _perm: Annotated[User, Depends(_require_perm("knowledge_base:promotion:submit"))],
):
    """我提交的晋升申请（提交人视角）。"""
    rows = list((
        await db.execute(
            select(KbPromotion).where(KbPromotion.submitted_by == user.id)
            .order_by(KbPromotion.id.desc()).limit(50)
        )
    ).scalars().all())
    return success_response(data={"items": [{
        "id": r.id, "page_id": r.page_id, "target_space_id": r.target_space_id,
        "status": r.status, "submit_note": r.submit_note,
        "review_comment": r.review_comment, "published_page_id": r.published_page_id,
    } for r in rows]})


@router.post("/promotions/{promotion_id}/review")
async def post_review(
    promotion_id: int,
    body: dict,
    user: Annotated[User, Depends(get_current_user)],
    _perm: Annotated[User, Depends(_require_perm("knowledge_base:promotion:review"))],
):
    """审核晋升申请（approve=true 批准发布，false 拒绝）。"""
    result = await review_promotion(
        promotion_id, user.id, bool(body.get("approve")), body.get("comment", "")
    )
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("msg"))
    return success_response(data=result, msg="审核完成")


# ---------------------------------------------------------------- 任务

@router.get("/jobs")
async def get_jobs(
    user: Annotated[User, Depends(get_current_user)],
    _perm: Annotated[User, Depends(_require_perm("knowledge_base:task:list"))],
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    """任务列表（当前用户）。"""
    data = await jobs_mod.list_jobs(user.id, page, page_size)
    return success_response(data=data)


@router.get("/jobs/{job_id}")
async def get_job_detail(
    job_id: int,
    user: Annotated[User, Depends(get_current_user)],
    _perm: Annotated[User, Depends(_require_perm("knowledge_base:task:list"))],
):
    """任务详情（行级隔离）。"""
    job = await jobs_mod.get_job(job_id, user.id)
    if job is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return success_response(data=job)


@router.post("/jobs/{job_id}/retry")
async def retry_job(
    job_id: int,
    user: Annotated[User, Depends(get_current_user)],
    _perm: Annotated[User, Depends(_require_perm("knowledge_base:task:retry"))],
):
    """失败任务重试（复用 payload 重新入队）。"""
    detail = await jobs_mod.get_job(job_id, user.id)
    if detail is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    if detail["status"] not in ("failed",):
        raise HTTPException(status_code=400, detail="仅失败任务可重试")
    from src.plugins.builtin.knowledge_base.pipeline.jobs import enqueue_job

    # 重新入队同类型任务（用原 payload 反解）
    from src.plugins.builtin.knowledge_base.models import KbJob

    from src.db import SessionLocal
    async with SessionLocal() as db:
        orig = await db.get(KbJob, job_id)
        payload = json.loads(orig.payload or "{}") if orig else {}
    new_id = await enqueue_job(user.id, detail["job_type"], payload, detail.get("space_id"))
    return success_response(data={"job_id": new_id}, msg="已重新入队")


# ---------------------------------------------------------------- 设置（P2：联网开关）

@router.get("/settings/web")
async def get_web_settings(
    user: Annotated[User, Depends(get_current_user)],
    _perm: Annotated[User, Depends(_require_perm("knowledge_base:setting:read"))],
):
    """联网采集设置（开关 + 域名白名单）。"""
    from src.plugins.builtin.knowledge_base.collector.web import (
        get_domain_allowlist,
        get_web_enabled,
    )
    return success_response(data={
        "web_enabled": await get_web_enabled(user.id),
        "domain_allow": await get_domain_allowlist(user.id),
    })


@router.put("/settings/web")
async def put_web_settings(
    body: dict,
    user: Annotated[User, Depends(get_current_user)],
    _perm: Annotated[User, Depends(_require_perm("knowledge_base:setting:update"))],
):
    """更新联网设置。

    .. warning:: 开启联网=数据外发。body: {web_enabled: bool, domain_allow: [str]}
    """
    from src.plugins.builtin.knowledge_base.collector.web import (
        get_domain_allowlist,
        set_web_enabled,
    )
    from src.plugins.builtin.knowledge_base.models import KbSetting
    from src.db import SessionLocal
    import json

    enable = bool(body.get("web_enabled"))
    if enable and not body.get("confirm"):  # 开启需显式确认（数据外发）
        raise HTTPException(status_code=400, detail="开启联网会外发数据，请传 confirm=true 显式确认")
    await set_web_enabled(user.id, enable)

    allow = body.get("domain_allow")
    if isinstance(allow, list):
        cleaned = [str(a).strip().lower()[:100] for a in allow if str(a).strip()][:50]
        async with SessionLocal() as db:
            from sqlalchemy import select
            row = (
                await db.execute(
                    select(KbSetting).where(
                        KbSetting.user_id == user.id, KbSetting.key == "web_domain_allow"
                    )
                )
            ).scalars().first()
            value = json.dumps(cleaned, ensure_ascii=False)
            if row is None:
                db.add(KbSetting(user_id=user.id, key="web_domain_allow", value=value))
            else:
                row.value = value
            await db.commit()

    return success_response(data={
        "web_enabled": enable,
        "domain_allow": await get_domain_allowlist(user.id),
    })
