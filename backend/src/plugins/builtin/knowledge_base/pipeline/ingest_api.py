"""摄入 API（P1）：空间管理 + 文档入库 + 编译触发。

- 私有空间写入：仅 owner
- 共享空间（M3）：成员可读，writer 可写，owner 拥有全部权限
- 公开库写入：仅经晋升流或空间 owner（本模块不提供普通用户直写公开库的路径）
"""
import hashlib

from loguru import logger
from sqlalchemy import select

from src.db import SessionLocal


async def get_space_role(user_id: int, space_id: int) -> str | None:
    """当前用户在该空间的角色：owner / writer / reader / None（public 空间的非成员视为 reader）。"""
    from src.plugins.builtin.knowledge_base.models import KbSpace, KbSpaceMember

    async with SessionLocal() as db:
        space = await db.get(KbSpace, space_id)
        if space is None or space.status != "active":
            return None
        if space.owner_user_id == user_id:
            return "owner"
        member = (
            await db.execute(
                select(KbSpaceMember).where(
                    KbSpaceMember.space_id == space_id,
                    KbSpaceMember.user_id == user_id,
                )
            )
        ).scalars().first()
        if member is not None:
            return "writer" if member.role == "writer" else "reader"
        return "reader" if space.visibility == "public" else None


async def ensure_space_access(user_id: int, space_id: int, need_write: bool = False) -> tuple[bool, str]:
    """校验空间访问权限（M3 统一入口）。

    - 读：owner / 成员 / public 空间任意登录用户
    - 写：owner / writer 成员（public 空间同样仅 owner 或 writer）
    """
    role = await get_space_role(user_id, space_id)
    if role is None:
        return False, "无权访问该空间（非成员）"
    if need_write and role == "reader":
        return False, "仅空间所有者或 writer 成员可写入"
    return True, "ok"


async def ensure_owned_space(user_id: int, space_id: int, need_owner: bool = True) -> tuple[bool, str]:
    """校验空间归属。need_owner=True 要求用户是空间所有者。"""
    from src.plugins.builtin.knowledge_base.models import KbSpace

    async with SessionLocal() as db:
        space = await db.get(KbSpace, space_id)
    if space is None or space.status != "active":
        return False, "空间不存在或已归档"
    if need_owner and space.owner_user_id != user_id:
        return False, "仅空间所有者可操作"
    return True, "ok"


async def get_or_create_default_space(user_id: int) -> int:
    """取用户第一个私有空间；无则自动创建「我的知识库」。"""
    from src.plugins.builtin.knowledge_base.models import KbSpace

    async with SessionLocal() as db:
        row = (
            await db.execute(
                select(KbSpace)
                .where(KbSpace.owner_user_id == user_id, KbSpace.status == "active")
                .order_by(KbSpace.id.asc())
                .limit(1)
            )
        ).scalars().first()
        if row is not None:
            return row.id
        space = KbSpace(
            owner_user_id=user_id,
            name="我的知识库",
            description="默认私有空间",
            visibility="private",
        )
        db.add(space)
        await db.commit()
        await db.refresh(space)
        logger.info(f"[KB] user={user_id} 创建默认空间 {space.id}")
        return space.id


async def ingest_doc(
    user_id: int,
    space_id: int,
    title: str,
    content: str,
    source_type: str = "chat",
    url: str | None = None,
    compile_now: bool = True,
) -> dict:
    """文档入库（私有空间）→（可选）触发后台编译任务。

    返回 {ok, doc_id, space_id, job_id?}。
    """
    from datetime import datetime, timezone

    from src.plugins.builtin.knowledge_base.models import KbDocSource

    title = title.strip()[:200]
    content = content.strip()
    if len(title) < 2:
        return {"ok": False, "msg": "标题过短"}
    if len(content) < 40:
        return {"ok": False, "msg": "内容过短（至少40字）"}

    if not space_id:
        space_id = await get_or_create_default_space(user_id)
    ok, msg = await ensure_space_access(user_id, space_id, need_write=True)
    if not ok:
        return {"ok": False, "msg": msg}

    # 同空间同 hash 幂等：内容未变的重复入库直接返回已有 doc
    content_hash = hashlib.blake2b(content.encode("utf-8"), digest_size=24).hexdigest()
    async with SessionLocal() as db:
        dup = (
            await db.execute(
                select(KbDocSource).where(
                    KbDocSource.space_id == space_id,
                    KbDocSource.content_hash == content_hash,
                )
            )
        ).scalars().first()
        if dup is not None:
            return {"ok": True, "doc_id": dup.id, "space_id": space_id, "msg": "内容未变化，已存在"}

        doc = KbDocSource(
            space_id=space_id,
            title=title,
            content=content,
            content_hash=content_hash,
            url=url or None,
            source_type=source_type if source_type in ("upload", "chat", "research", "web", "paper") else "upload",
            char_count=len(content),
        )
        db.add(doc)
        await db.commit()
        await db.refresh(doc)
        doc_id = doc.id

    result = {"ok": True, "doc_id": doc_id, "space_id": space_id}
    if compile_now:
        from src.plugins.builtin.knowledge_base.pipeline.jobs import enqueue_job

        job_id = await enqueue_job(
            user_id, "wiki_ingest", {"space_id": space_id, "doc_ids": [doc_id]}, space_id=space_id
        )
        result["job_id"] = job_id
        result["msg"] = "已入库，编译任务已入队"
    return result


async def create_space(user_id: int, name: str, description: str = "", visibility: str = "private") -> dict:
    """创建空间。private 人人可建；public 由路由层做维护者鉴权后传入。"""
    from src.plugins.builtin.knowledge_base.models import KbSpace

    if visibility not in ("private", "shared", "public"):
        return {"ok": False, "msg": "可见性仅支持 private / shared / public"}
    name = name.strip()[:100]
    if len(name) < 1:
        return {"ok": False, "msg": "空间名不能为空"}
    async with SessionLocal() as db:
        space = KbSpace(
            owner_user_id=user_id, name=name,
            description=(description or "")[:2000], visibility=visibility,
        )
        db.add(space)
        await db.commit()
        await db.refresh(space)
        return {"ok": True, "space_id": space.id}


async def list_spaces(user_id: int) -> list[dict]:
    """用户视角的空间列表：自己的全部 + 自己参与的共享空间 + 公开空间。"""
    from src.plugins.builtin.knowledge_base.models import KbSpace, KbSpaceMember

    async with SessionLocal() as db:
        rows = list((
            await db.execute(
                select(KbSpace)
                .where(KbSpace.status == "active")
                .order_by(KbSpace.visibility.desc(), KbSpace.id.asc())
            )
        ).scalars().all())
        memberships = {
            m.space_id: m.role
            for m in (
                await db.execute(
                    select(KbSpaceMember).where(KbSpaceMember.user_id == user_id)
                )
            ).scalars().all()
        }
    out = []
    for s in rows:
        owned = s.owner_user_id == user_id
        role = "owner" if owned else memberships.get(s.id)
        if not owned and s.visibility == "public":
            role = role or "reader"
        elif not owned and role is None:
            continue  # 私有/共享：非成员不可见
        out.append({
            "id": s.id, "name": s.name, "description": s.description,
            "visibility": s.visibility, "page_count": s.page_count,
            "owned": owned, "my_role": role,
        })
    return out
