"""知识中心 API（周期三）——唯一公共知识库的浏览 + 分类管理。

.. pattern:: 与 api_wiki 同纪律
   - 只读浏览：仅知识中心 space 的 live 词条（SQL 层过滤，非 if 判断兜底）
   - 分类管理：knowledge_base:category:manage 权限（人工兜底；AI 推送甄选可自主建分类）
   - 「按知识库」视图已移除（知识中心是唯一公共库）
"""
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
    KbCategory,
    KbWikiPage,
)
from src.plugins.builtin.knowledge_base.pipeline.hub import get_hub_space_id

router = APIRouter(prefix="/knowledge-base/hub", tags=["ApeCortex 知识中心"])

_MAX_NAME_LEN = 50


# ---------------------------------------------------------------- 分类管理

def _cat_out(c: KbCategory) -> dict:
    return {
        "id": c.id,
        "name": c.name,
        "parent_id": c.parent_id,
        "icon": c.icon,
        "sort": c.sort,
    }


@router.get("/categories")
async def get_categories(
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
    _perm: Annotated[User, Depends(_require_perm("knowledge_base:hub:list"))],
):
    """分类树（两级）+ 各分类词条数。"""
    cats = list((await db.execute(
        select(KbCategory).order_by(KbCategory.sort, KbCategory.id)
    )).scalars().all())
    hub_id = await get_hub_space_id()

    counts: dict[int, int] = {}
    if hub_id:
        pages = list((await db.execute(
            select(KbWikiPage.category_id).where(
                KbWikiPage.space_id == hub_id,
                KbWikiPage.status == "live",
                KbWikiPage.is_deleted.is_(False),
                KbWikiPage.category_id.is_not(None),
            )
        )).scalars().all())
        for cid in pages:
            counts[cid] = counts.get(cid, 0) + 1

    nodes = {c.id: _cat_out(c) for c in cats}
    roots: list[dict] = []
    for c in cats:
        node = nodes[c.id]
        node["page_count"] = counts.get(c.id, 0)
        if c.parent_id is None:
            roots.append(node)
        elif c.parent_id in nodes:
            nodes[c.parent_id].setdefault("children", []).append(node)
        else:  # 父分类已删（防御）：上提为一级
            roots.append(node)
    # 子分类词条数并入一级展示
    for c in cats:
        if c.parent_id is not None and c.parent_id in nodes:
            nodes[c.parent_id]["page_count"] = (
                nodes[c.parent_id].get("page_count", 0) + counts.get(c.id, 0)
            )
    return success_response(data={"items": roots})


@router.post("/categories")
async def create_category(
    body: dict,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
    _perm: Annotated[User, Depends(_require_perm("knowledge_base:category:manage"))],
):
    """新建分类（parent_id 空=一级；传值则挂二级，且父必须是一级）。"""
    name = str(body.get("name", "")).strip()[:_MAX_NAME_LEN]
    if not name:
        raise HTTPException(status_code=400, detail="分类名不能为空")
    parent_id = body.get("parent_id")
    if parent_id is not None:
        parent = (await db.execute(
            select(KbCategory).where(KbCategory.id == parent_id)
        )).scalars().first()
        if parent is None:
            raise HTTPException(status_code=400, detail="父分类不存在")
        if parent.parent_id is not None:
            raise HTTPException(status_code=400, detail="分类最多两级")

    cat = KbCategory(
        name=name,
        parent_id=parent_id,
        icon=str(body.get("icon") or "Folder")[:50],
        sort=int(body.get("sort") or 0),
    )
    db.add(cat)
    await db.commit()
    await db.refresh(cat)
    return success_response(data=_cat_out(cat), msg="分类已创建")


@router.put("/categories/{category_id}")
async def update_category(
    category_id: int,
    body: dict,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
    _perm: Annotated[User, Depends(_require_perm("knowledge_base:category:manage"))],
):
    """更新分类（改名/图标/排序；不允许改父级，避免循环）。"""
    cat = (await db.execute(
        select(KbCategory).where(KbCategory.id == category_id)
    )).scalars().first()
    if cat is None:
        raise HTTPException(status_code=404, detail="分类不存在")
    if "name" in body:
        name = str(body["name"] or "").strip()[:_MAX_NAME_LEN]
        if not name:
            raise HTTPException(status_code=400, detail="分类名不能为空")
        cat.name = name
    if "icon" in body:
        cat.icon = str(body["icon"] or "Folder")[:50]
    if "sort" in body:
        cat.sort = int(body["sort"] or 0)
    await db.commit()
    return success_response(msg="分类已更新")


@router.delete("/categories/{category_id}")
async def delete_category(
    category_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
    _perm: Annotated[User, Depends(_require_perm("knowledge_base:category:manage"))],
):
    """删除分类：知识中心词条 category_id 置空（不删词条）；子分类上提一级。"""
    cat = (await db.execute(
        select(KbCategory).where(KbCategory.id == category_id)
    )).scalars().first()
    if cat is None:
        raise HTTPException(status_code=404, detail="分类不存在")

    children = list((await db.execute(
        select(KbCategory).where(KbCategory.parent_id == category_id)
    )).scalars().all())
    for child in children:
        child.parent_id = cat.parent_id

    from sqlalchemy import update

    hub_id = await get_hub_space_id()
    if hub_id:
        await db.execute(
            update(KbWikiPage).where(
                KbWikiPage.space_id == hub_id, KbWikiPage.category_id == category_id
            ).values(category_id=None)
        )
    await db.delete(cat)
    await db.commit()
    return success_response(msg="分类已删除（其下词条转为未分类）")


# ---------------------------------------------------------------- 知识中心浏览

@router.get("/pages")
async def hub_pages(
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
    _perm: Annotated[User, Depends(_require_perm("knowledge_base:hub:list"))],
    category_id: int | None = Query(default=None, description="按分类筛选（0=未分类）"),
    search: str = Query(default="", max_length=100),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=50),
):
    """知识中心词条列表（唯一公共库的 live 词条，支持搜索/分类过滤）。"""
    hub_id = await get_hub_space_id()
    conds = [
        KbWikiPage.space_id == hub_id,
        KbWikiPage.status == "live",
        KbWikiPage.is_deleted.is_(False),
    ]
    if category_id == 0:
        conds.append(KbWikiPage.category_id.is_(None))
    elif category_id is not None:
        cat_ids = {category_id} | {
            r[0] for r in (await db.execute(
                select(KbCategory.id).where(KbCategory.parent_id == category_id)
            )).all()
        }
        conds.append(KbWikiPage.category_id.in_(cat_ids))
    if search.strip():
        conds.append(KbWikiPage.title.contains(search.strip()))

    base = select(KbWikiPage).where(*conds)
    total = (await db.execute(
        select(func.count()).select_from(base.subquery())
    )).scalar_one()
    pages = list((await db.execute(
        base.order_by(KbWikiPage.updated_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )).scalars().all())

    items = [
        {
            "id": p.id,
            "slug": p.slug,
            "title": p.title,
            "category_id": p.category_id,
            "updated_at": p.updated_at.isoformat() if p.updated_at else None,
            "confidence": p.confidence,
        }
        for p in pages
    ]
    return success_response(data={"items": items, "total": total, "page": page, "page_size": page_size})


@router.get("/pages/{page_id}")
async def hub_page_detail(
    page_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
    _perm: Annotated[User, Depends(_require_perm("knowledge_base:hub:list"))],
):
    """知识中心词条详情（阅读抽屉）：仅知识中心 space 的 live 词条可读。"""
    hub_id = await get_hub_space_id()
    p = (await db.execute(
        select(KbWikiPage).where(
            KbWikiPage.id == page_id,
            KbWikiPage.space_id == hub_id,
            KbWikiPage.status == "live",
            KbWikiPage.is_deleted.is_(False),
        )
    )).scalars().first()
    if p is None:
        raise HTTPException(status_code=404, detail="词条不存在或未公开")

    import json as _json

    return success_response(data={
        "id": p.id,
        "slug": p.slug,
        "title": p.title,
        "body_md": p.body_md,
        "category_id": p.category_id,
        "out_links": _json.loads(p.out_links or "[]"),
        "source_refs": _json.loads(p.source_refs or "[]"),
        "updated_at": p.updated_at.isoformat() if p.updated_at else None,
    })
