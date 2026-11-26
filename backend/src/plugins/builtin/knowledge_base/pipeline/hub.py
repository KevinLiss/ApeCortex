"""知识中心管线（周期三）——唯一公共知识库的容器与分类维护。

知识中心 = 全系统唯一 hub space（owner_user_id=0 系统所有），对外展示名「知识中心」。
- 任何人可读（hub API 只暴露 live 词条）
- 写入仅经推送流（submission 管线：AI 甄选 → 自动入库）
- 分类由 AI 自主维护（推送甄选时可创建新分类）+ 人工兜底（分类管理弹窗）

.. note:: visibility 列已废弃；hub space 用 owner_user_id == 0 + 固定名识别，
   避免依赖废弃列的语义。幂等：不存在则创建。
"""
from loguru import logger
from sqlalchemy import select

from src.db import SessionLocal

HUB_SPACE_NAME = "ApeCortex 知识中心"
_HUB_SPACE_ID_CACHE: int | None = None


async def get_hub_space_id() -> int:
    """取（或创建）知识中心 space id（模块级缓存，进程内只查一次）。"""
    global _HUB_SPACE_ID_CACHE
    if _HUB_SPACE_ID_CACHE:
        return _HUB_SPACE_ID_CACHE
    from src.plugins.builtin.knowledge_base.models import KbSpace

    async with SessionLocal() as db:
        space = (
            await db.execute(
                select(KbSpace).where(
                    KbSpace.owner_user_id == 0, KbSpace.name == HUB_SPACE_NAME
                )
            )
        ).scalars().first()
        if space is None:
            space = KbSpace(
                owner_user_id=0,
                name=HUB_SPACE_NAME,
                description="知识中心——推送内容经 AI 甄选自动归类的公共知识库",
                visibility="public",  # 历史语义列，仅供旧版识别
            )
            db.add(space)
            await db.commit()
            await db.refresh(space)
            logger.info(f"[KnowledgeBase] 知识中心 space 已创建: id={space.id}")
        _HUB_SPACE_ID_CACHE = space.id
        return space.id


def reset_hub_cache() -> None:
    """测试用：清缓存。"""
    global _HUB_SPACE_ID_CACHE
    _HUB_SPACE_ID_CACHE = None


async def ai_ensure_category(name: str, parent_id: int | None = None) -> int | None:
    """AI 自主维护分类：名称去重后创建（推送甄选时 AI 觉得现有分类都不合适则建新分类）。

    返回新分类 id；空名/重复名返回已有 id 或 None。
    """
    from src.plugins.builtin.knowledge_base.models import KbCategory

    name = (name or "").strip()[:50]
    if not name:
        return None
    async with SessionLocal() as db:
        existing = (
            await db.execute(
                select(KbCategory).where(KbCategory.name == name, KbCategory.parent_id.is_(None))
            )
        ).scalars().first()
        if existing is not None:
            return existing.id
        cat = KbCategory(name=name, icon="Folder", sort=99)
        db.add(cat)
        await db.commit()
        await db.refresh(cat)
        logger.info(f"[KnowledgeBase] AI 创建新分类: {name} (id={cat.id})")
        return cat.id
