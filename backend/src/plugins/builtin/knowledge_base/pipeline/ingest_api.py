"""摄入 API（P1）：空间管理 + 文档入库 + 编译触发。

周期三简化（2026-11-26 用户决策）：
- 空间无类型概念：所有空间都是个人知识库，仅 owner 可读写
- 知识中心（hub）是唯一公共库，由 submission 管线写入（见 pipeline/submission.py）
- M3 的共享空间/成员管理已下线
"""
import hashlib

from loguru import logger
from sqlalchemy import select

from src.db import SessionLocal


async def get_space_role(user_id: int, space_id: int) -> str | None:
    """当前用户在该空间的角色：owner / None（空间为个人库，仅 owner 可访问）。"""
    from src.plugins.builtin.knowledge_base.models import KbSpace

    async with SessionLocal() as db:
        space = await db.get(KbSpace, space_id)
        if space is None or space.status != "active":
            return None
        return "owner" if space.owner_user_id == user_id else None


async def ensure_space_access(user_id: int, space_id: int, need_write: bool = False) -> tuple[bool, str]:
    """校验空间访问权限（周期三：个人库，读写均仅 owner）。"""
    role = await get_space_role(user_id, space_id)
    if role is None:
        return False, "无权访问该空间（非所有者）"
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
    file_ext: str | None = None,
) -> dict:
    """文档入库（私有空间）→（可选）触发后台编译任务。

    返回 {ok, doc_id, space_id, job_id?}。
    """

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
            file_ext=file_ext or None,
            char_count=len(content),
        )
        db.add(doc)
        await db.commit()
        await db.refresh(doc)
        doc_id = doc.id

    result = {"ok": True, "doc_id": doc_id, "space_id": space_id}
    if compile_now:
        from src.plugins.builtin.knowledge_base.pipeline.jobs import enqueue_job

        try:
            job_id = await enqueue_job(
                user_id, "wiki_ingest", {"space_id": space_id, "doc_ids": [doc_id]}, space_id=space_id
            )
            result["job_id"] = job_id
            result["msg"] = "已入库，编译任务已入队"
        except RuntimeError as exc:  # 成本护栏：当日任务超限
            result["msg"] = f"已入库，但编译未入队：{exc}"
    return result


async def create_space(user_id: int, name: str, description: str = "", visibility: str = "") -> dict:
    """创建空间（周期三：个人知识库，无类型概念；visibility 参数兼容旧调用并被忽略）。"""
    from src.plugins.builtin.knowledge_base.models import KbSpace

    name = name.strip()[:100]
    if len(name) < 1:
        return {"ok": False, "msg": "空间名不能为空"}
    async with SessionLocal() as db:
        space = KbSpace(
            owner_user_id=user_id, name=name,
            description=(description or "")[:2000], visibility="private",
        )
        db.add(space)
        await db.commit()
        await db.refresh(space)
        return {"ok": True, "space_id": space.id}


async def list_spaces(user_id: int) -> list[dict]:
    """用户视角的空间列表（周期三：个人知识库，只返回自己的空间）。"""
    from src.plugins.builtin.knowledge_base.models import KbSpace

    async with SessionLocal() as db:
        rows = list((
            await db.execute(
                select(KbSpace)
                .where(KbSpace.owner_user_id == user_id, KbSpace.status == "active")
                .order_by(KbSpace.id.asc())
            )
        ).scalars().all())
    return [
        {
            "id": s.id, "name": s.name, "description": s.description,
            "visibility": s.visibility, "page_count": s.page_count,
            "owned": True, "my_role": "owner",
        }
        for s in rows
    ]


async def update_space(user_id: int, space_id: int, name: str, description: str = "") -> dict:
    """编辑空间名称/描述（仅 owner；hub 空间无人类 owner 不可编辑）。"""
    from src.plugins.builtin.knowledge_base.models import KbSpace

    name = (name or "").strip()[:100]
    if not name:
        return {"ok": False, "msg": "空间名不能为空"}
    ok, msg = await ensure_owned_space(user_id, space_id, need_owner=True)
    if not ok:
        return {"ok": False, "msg": msg}
    async with SessionLocal() as db:
        space = await db.get(KbSpace, space_id)
        if space is None:
            return {"ok": False, "msg": "空间不存在"}
        space.name = name
        space.description = (description or "")[:2000]
        await db.commit()
    return {"ok": True, "space_id": space_id}


async def delete_space(user_id: int, space_id: int) -> dict:
    """删除空间及其全部内容（仅 owner；级联清词条/修订/文档/任务/原始文件）。

    防呆：知识中心（hub，owner_user_id=0）是系统单例，拒绝删除。
    推送单（kb_public_submission）是全局留痕，仅存 source_space_id 溯源引用，不级联。
    """
    from src.plugins.builtin.knowledge_base.models import (
        KbDocSource,
        KbJob,
        KbSpace,
        KbWikiPage,
        KbWikiRevision,
    )

    raw_exts: dict[int, str] = {}
    async with SessionLocal() as db:
        space = await db.get(KbSpace, space_id)
        if space is None or space.status != "active":
            return {"ok": False, "msg": "空间不存在或已归档"}
        if space.owner_user_id == 0:
            return {"ok": False, "msg": "知识中心是系统公共库，不可删除"}
        if space.owner_user_id != user_id:
            return {"ok": False, "msg": "仅空间所有者可删除"}

        # 收集文档原始文件扩展名（会后置清理文件系统）
        docs = list((await db.execute(
            select(KbDocSource).where(KbDocSource.space_id == space_id)
        )).scalars().all())
        raw_exts = {d.id: (d.file_ext or "").lower() for d in docs}

        # 级联删除：修订（经词条关联）→ 词条 → 文档 → 任务 → 空间
        await db.execute(
            KbWikiRevision.__table__.delete().where(
                KbWikiRevision.page_id.in_(
                    select(KbWikiPage.id).where(KbWikiPage.space_id == space_id)
                )
            )
        )
        await db.execute(
            KbWikiPage.__table__.delete().where(KbWikiPage.space_id == space_id)
        )
        await db.execute(
            KbDocSource.__table__.delete().where(KbDocSource.space_id == space_id)
        )
        await db.execute(
            KbJob.__table__.delete().where(KbJob.space_id == space_id)
        )
        await db.delete(space)
        await db.commit()

    # 后置清理：上传的原始文件（失败不影响删除结果，留残文件无害）
    from src.plugins.builtin.knowledge_base.api_docs import _raw_dir

    try:
        raw_dir = _raw_dir()
        for doc_id, ext in raw_exts.items():
            if ext:
                (raw_dir / f"{doc_id}.{ext}").unlink(missing_ok=True)
    except Exception as exc:  # noqa: BLE001 — 文件清理失败不回滚删除
        logger.warning(f"[KnowledgeBase] 空间 {space_id} 原始文件清理失败（忽略）: {exc}")

    logger.info(f"[KnowledgeBase] 空间 {space_id} 已删除（含 {len(raw_exts)} 份文档及词条/任务）")
    return {"ok": True, "space_id": space_id}
