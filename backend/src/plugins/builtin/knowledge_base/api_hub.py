"""知识中心 API（N1）——公开库浏览 + 分类管理。

.. pattern:: 与 api_wiki 同纪律
   - 只读浏览：仅公开空间 live 词条（SQL 层过滤，非 if 判断兜底）
   - 分类管理：knowledge_base:category:manage 权限
   - 响应统一信封
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
    KbSpace,
    KbWikiPage,
)

router = APIRouter(prefix="/knowledge-base/hub", tags=["ApeCortex 知识中心"])

_MAX_NAME_LEN = 50


# ---------------------------------------------------------------- 分类管理

def _cat_out(c: KbCategory, children: list[dict] | None = None) -> dict:
    return {
        "id": c.id,
        "name": c.name,
        "parent_id": c.parent_id,
        "icon": c.icon,
        "sort": c.sort,
        "children": children or [],
    }


@router.get("/categories")
async def get_categories(
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
    _perm: Annotated[User, Depends(_require_perm("knowledge_base:hub:list"))],
):
    """分类树（两级）。附公开空间数量（知识中心「按分类」视图用）。"""
    cats = list((await db.execute(
        select(KbCategory).order_by(KbCategory.sort, KbCategory.id)
    )).scalars().all())

    # 各分类下公开空间数（一级分类聚合其子分类）
    space_counts: dict[int, int] = dict(
        (await db.execute(
            select(KbSpace.category_id, func.count(KbSpace.id)).where(
                KbSpace.visibility == "public", KbSpace.status == "active"
            ).group_by(KbSpace.category_id)
        )).all()
    )
    nodes = {c.id: _cat_out(c) for c in cats}
    roots: list[dict] = []
    for c in cats:
        node = nodes[c.id]
        node["space_count"] = space_counts.get(c.id, 0)
        if c.parent_id is None:
            roots.append(node)
        elif c.parent_id in nodes:
            nodes[c.parent_id]["children"].append(node)
        else:  # 父分类已删（防御）：上提为一级
            roots.append(node)
    # 子分类数量并入一级展示
    for c in cats:
        if c.parent_id is not None and c.parent_id in nodes:
            nodes[c.parent_id]["space_count"] = (
                nodes[c.parent_id].get("space_count", 0) + space_counts.get(c.id, 0)
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
    """删除分类：公开空间 category_id 置空（不删空间）；子分类上提一级。"""
    cat = (await db.execute(
        select(KbCategory).where(KbCategory.id == category_id)
    )).scalars().first()
    if cat is None:
        raise HTTPException(status_code=404, detail="分类不存在")

    from sqlalchemy import update

    children = list((await db.execute(
        select(KbCategory).where(KbCategory.parent_id == category_id)
    )).scalars().all())
    for child in children:
        child.parent_id = cat.parent_id  # 上提一级（删的是一级则变一级，删的是二级本来就无子）
    await db.execute(
        update(KbSpace).where(KbSpace.category_id == category_id).values(category_id=None)
    )
    await db.delete(cat)
    await db.commit()
    return success_response(msg="分类已删除（其下公开空间转为未分类）")


@router.put("/spaces/{space_id}/category")
async def assign_space_category(
    space_id: int,
    body: dict,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
    _perm: Annotated[User, Depends(_require_perm("knowledge_base:category:manage"))],
):
    """公开空间挂分类（维护者操作；category_id 传 null 清除）。"""
    space = (await db.execute(
        select(KbSpace).where(KbSpace.id == space_id)
    )).scalars().first()
    if space is None:
        raise HTTPException(status_code=404, detail="空间不存在")
    if space.visibility != "public":
        raise HTTPException(status_code=400, detail="仅公开空间支持挂分类")
    category_id = body.get("category_id")
    if category_id is not None:
        cat = (await db.execute(
            select(KbCategory).where(KbCategory.id == category_id)
        )).scalars().first()
        if cat is None:
            raise HTTPException(status_code=400, detail="分类不存在")
    space.category_id = category_id
    await db.commit()
    return success_response(msg="空间分类已更新")


# ---------------------------------------------------------------- 知识中心浏览

async def _live_page_count(db: AsyncSession, space_id: int) -> int:
    return (await db.execute(
        select(func.count()).select_from(KbWikiPage).where(
            KbWikiPage.space_id == space_id,
            KbWikiPage.status == "live",
            KbWikiPage.is_deleted.is_(False),
        )
    )).scalar_one()


@router.get("/spaces")
async def hub_spaces(
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
    _perm: Annotated[User, Depends(_require_perm("knowledge_base:hub:list"))],
    category_id: int | None = Query(default=None, description="按分类筛选（含子分类）"),
):
    """公开库列表（知识中心「按知识库」视图）。附 live 词条数与分类信息。
    category_id=0 表示「未分类」；缺省=全部。"""
    cat_ids: set[int] | None = None
    uncategorized = False
    if category_id == 0:
        uncategorized = True
    elif category_id is not None:
        cat_ids = {category_id}
        cat_ids.update(
            r[0] for r in (await db.execute(
                select(KbCategory.id).where(KbCategory.parent_id == category_id)
            )).all()
        )

    stmt = select(KbSpace).where(
        KbSpace.visibility == "public", KbSpace.status == "active"
    ).order_by(KbSpace.id.desc())
    spaces = list((await db.execute(stmt)).scalars().all())

    counts: dict[int, int] = dict((await db.execute(
        select(KbWikiPage.space_id, func.count()).where(
            KbWikiPage.space_id.in_([s.id for s in spaces]),
            KbWikiPage.status == "live",
            KbWikiPage.is_deleted.is_(False),
        ).group_by(KbWikiPage.space_id)
    )).all())

    items = [
        {
            "id": s.id,
            "name": s.name,
            "description": s.description,
            "category_id": s.category_id,
            "page_count": counts.get(s.id, 0),
            "created_at": s.created_at.isoformat() if s.created_at else None,
        }
        for s in spaces
        if (uncategorized and s.category_id is None)
        or (not uncategorized and (cat_ids is None or s.category_id in cat_ids))
    ]
    return success_response(data={"items": items})


@router.get("/pages")
async def hub_pages(
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
    _perm: Annotated[User, Depends(_require_perm("knowledge_base:hub:list"))],
    space_id: int | None = Query(default=None),
    category_id: int | None = Query(default=None),
    search: str = Query(default="", max_length=100),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=50),
):
    """公开库 live 词条列表（知识中心词条浏览，支持搜索/库/分类过滤；category_id=0=未分类）。"""
    conds = [
        KbSpace.visibility == "public",
        KbSpace.status == "active",
        KbWikiPage.status == "live",
        KbWikiPage.is_deleted.is_(False),
    ]
    if space_id is not None:
        conds.append(KbWikiPage.space_id == space_id)
    if category_id == 0:
        conds.append(KbSpace.category_id.is_(None))
    elif category_id is not None:
        cat_ids = {category_id} | {
            r[0] for r in (await db.execute(
                select(KbCategory.id).where(KbCategory.parent_id == category_id)
            )).all()
        }
        conds.append(KbSpace.category_id.in_(cat_ids))
    if search.strip():
        conds.append(KbWikiPage.title.contains(search.strip()))

    base = (
        select(KbWikiPage, KbSpace.name)
        .join(KbSpace, KbWikiPage.space_id == KbSpace.id)
        .where(*conds)
    )
    total = (await db.execute(
        select(func.count()).select_from(base.subquery())
    )).scalar_one()
    rows = list((await db.execute(
        base.order_by(KbWikiPage.updated_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )).all())

    items = [
        {
            "id": p.id,
            "slug": p.slug,
            "title": p.title,
            "space_id": p.space_id,
            "space_name": space_name,
            "updated_at": p.updated_at.isoformat() if p.updated_at else None,
            "confidence": p.confidence,
        }
        for p, space_name in rows
    ]
    return success_response(data={"items": items, "total": total, "page": page, "page_size": page_size})


@router.get("/pages/{page_id}")
async def hub_page_detail(
    page_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
    _perm: Annotated[User, Depends(_require_perm("knowledge_base:hub:list"))],
):
    """公开词条详情（阅读抽屉）。仅公开空间 live 词条可读。"""
    row = (await db.execute(
        select(KbWikiPage, KbSpace.name, KbSpace.description)
        .join(KbSpace, KbWikiPage.space_id == KbSpace.id)
        .where(
            KbWikiPage.id == page_id,
            KbSpace.visibility == "public",
            KbWikiPage.status == "live",
            KbWikiPage.is_deleted.is_(False),
        )
    )).first()
    if row is None:
        raise HTTPException(status_code=404, detail="词条不存在或未公开")

    p, space_name, space_desc = row
    import json as _json

    return success_response(data={
        "id": p.id,
        "slug": p.slug,
        "title": p.title,
        "body_md": p.body_md,
        "space_id": p.space_id,
        "space_name": space_name,
        "space_description": space_desc,
        "out_links": _json.loads(p.out_links or "[]"),
        "source_refs": _json.loads(p.source_refs or "[]"),
        "updated_at": p.updated_at.isoformat() if p.updated_at else None,
    })
