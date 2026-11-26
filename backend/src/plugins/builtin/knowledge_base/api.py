"""API 路由——记忆画像管理页。

.. pattern:: 路由注册模式
   - 路由前缀：``/knowledge-base``（插件名下划线转连字符）
   - 权限标识：``knowledge_base:profile:*``
   - 行级隔离：所有查询强制 ``user_id == 当前用户``（仿 crud/ai.py get_owned 模式），
     管理员也只能看自己的记忆（隐私合规：记忆是用户私有数据）。
"""
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.deps import get_current_user
from src.core.deps import require_permission as _require_perm
from src.core.exceptions import success_response
from src.db import get_db
from src.models import User
from src.plugins.builtin.knowledge_base.models import KbMemoryItem, KbProfile
from src.plugins.builtin.knowledge_base.pipeline import chat_prefs as chat_prefs_pipeline
from src.plugins.builtin.knowledge_base.pipeline import memory as memory_pipeline
from src.plugins.builtin.knowledge_base.schemas import MemoryOut, ProfileOut

router = APIRouter(prefix="/knowledge-base", tags=["AI 知识库"])


# ---------------------------------------------------------------- 聊天偏好（N3）

@router.get("/chat-prefs")
async def get_my_chat_prefs(
    user: Annotated[User, Depends(get_current_user)],
    _perm: Annotated[User, Depends(_require_perm("knowledge_base:profile:list"))],
):
    """当前用户的聊天知识库偏好（召回范围/记忆开关/自动注入）。"""
    return success_response(data=await chat_prefs_pipeline.get_chat_prefs(user.id))


@router.put("/chat-prefs")
async def put_chat_prefs(
    body: dict,
    user: Annotated[User, Depends(get_current_user)],
    _perm: Annotated[User, Depends(_require_perm("knowledge_base:profile:list"))],
):
    """保存聊天知识库偏好。"""
    result = await chat_prefs_pipeline.set_chat_prefs(user.id, body or {})
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("msg"))
    return success_response(data=await chat_prefs_pipeline.get_chat_prefs(user.id), msg="偏好已保存")


@router.get("/memories")
async def list_memories(
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
    _perm: Annotated[User, Depends(_require_perm("knowledge_base:profile:list"))],
    mem_type: str | None = Query(default=None, description="类型过滤: fact/preference/habit/event"),
    q: str | None = Query(default=None, max_length=100, description="关键词过滤"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    """分页查询当前用户的记忆条目。"""
    conditions = [KbMemoryItem.user_id == user.id]
    if mem_type:
        conditions.append(KbMemoryItem.mem_type == mem_type)
    if q:
        conditions.append(KbMemoryItem.content.ilike(f"%{q}%"))

    stmt = (
        select(KbMemoryItem)
        .where(*conditions)
        .order_by(KbMemoryItem.updated_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    items = (await db.execute(stmt)).scalars().all()

    count_stmt = select(func.count()).select_from(KbMemoryItem).where(*conditions)
    total = (await db.execute(count_stmt)).scalar_one()

    return success_response(data={
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": [MemoryOut.model_validate(i) for i in items],
    })


@router.delete("/memories/{item_id}")
async def delete_memory(
    item_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
    _perm: Annotated[User, Depends(_require_perm("knowledge_base:profile:delete"))],
):
    """删除一条自己的记忆（隐私：仅本人，物理删除）。"""
    stmt = select(KbMemoryItem).where(
        KbMemoryItem.id == item_id, KbMemoryItem.user_id == user.id
    )
    item = (await db.execute(stmt)).scalars().first()
    if item is None:
        raise HTTPException(status_code=404, detail="记忆不存在")

    await db.execute(delete(KbMemoryItem).where(KbMemoryItem.id == item_id))
    await db.commit()
    return success_response(msg="删除成功")


@router.get("/profile")
async def get_profile(
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
    _perm: Annotated[User, Depends(_require_perm("knowledge_base:profile:list"))],
):
    """获取当前用户画像（不存在则返回空画像占位，不落库）。"""
    profile = (
        await db.execute(select(KbProfile).where(KbProfile.user_id == user.id))
    ).scalars().first()
    if profile is None:
        # 未生成过画像：返回占位响应（不写库，避免空行污染）
        return success_response(data={
            "user_id": user.id,
            "profile_md": "",
            "version": 0,
            "updated_at": None,
        })
    return success_response(data=ProfileOut.model_validate(profile).model_dump())


@router.post("/profile/rewrite")
async def rewrite_profile_now(
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
    _perm: Annotated[User, Depends(_require_perm("knowledge_base:profile:rewrite"))],
):
    """立即重写画像（一次 LLM 调用，同步等待，前端显示 loading）。"""
    ok = await memory_pipeline.rewrite_profile(user.id)
    if not ok:
        raise HTTPException(status_code=400, detail="重写失败：无可用模型或暂无记忆条目")
    profile = (
        await db.execute(select(KbProfile).where(KbProfile.user_id == user.id))
    ).scalars().first()
    return success_response(
        data=ProfileOut.model_validate(profile).model_dump(), msg="画像已重写"
    )


@router.get("/stats")
async def stats(
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
    _perm: Annotated[User, Depends(_require_perm("knowledge_base:profile:list"))],
):
    """记忆统计（页面头部卡片）。"""
    base = [KbMemoryItem.user_id == user.id]
    total = (await db.execute(
        select(func.count()).select_from(KbMemoryItem).where(*base)
    )).scalar_one()
    by_type_rows = (await db.execute(
        select(KbMemoryItem.mem_type, func.count())
        .where(*base).group_by(KbMemoryItem.mem_type)
    )).all()
    by_type = {row[0]: row[1] for row in by_type_rows}

    profile = (
        await db.execute(select(KbProfile).where(KbProfile.user_id == user.id))
    ).scalars().first()
    return success_response(data={
        "total": total,
        "by_type": by_type,
        "last_rewrite_at": profile.updated_at if profile else None,
    })
