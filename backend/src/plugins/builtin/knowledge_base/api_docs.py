"""文档管理 API（N1）——空间内文档的上传/列表/详情/删除/重新编译。

.. pattern:: 与 api_wiki 同纪律
   - 行级隔离：ensure_space_access（周期三：读写均仅空间 owner）
   - 响应统一信封
"""
import json
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.deps import get_current_user
from src.core.deps import require_permission as _require_perm
from src.core.exceptions import success_response
from src.db import get_db
from src.models import User
from src.plugins.builtin.knowledge_base.models import (
    KbDocSource,
    KbSpace,
    KbWikiPage,
)
from src.plugins.builtin.knowledge_base.pipeline.fileparse import parse_file_to_text
from src.plugins.builtin.knowledge_base.pipeline.ingest_api import (
    ensure_space_access,
    ingest_doc,
)

router = APIRouter(prefix="/knowledge-base", tags=["ApeCortex 文档管理"])


async def _space_or_404(db: AsyncSession, space_id: int) -> KbSpace:
    space = (await db.execute(
        select(KbSpace).where(KbSpace.id == space_id)
    )).scalars().first()
    if space is None:
        raise HTTPException(status_code=404, detail="空间不存在")
    return space


def _doc_out(d: KbDocSource, page_count: int = 0) -> dict:
    return {
        "id": d.id,
        "space_id": d.space_id,
        "title": d.title,
        "url": d.url,
        "source_type": d.source_type,
        "file_ext": d.file_ext,
        "char_count": d.char_count,
        "compiled_at": d.compiled_at.isoformat() if d.compiled_at else None,
        "page_count": page_count,
        "created_at": d.created_at.isoformat() if d.created_at else None,
    }


async def _doc_page_counts(db: AsyncSession, space_id: int) -> dict[int, int]:
    """该空间内 doc_id → 引用它的词条数（扫 source_refs JSON）。"""
    pages = list((await db.execute(
        select(KbWikiPage).where(
            KbWikiPage.space_id == space_id, KbWikiPage.is_deleted.is_(False)
        )
    )).scalars().all())
    counts: dict[int, int] = {}
    for p in pages:
        try:
            refs = json.loads(p.source_refs or "[]")
        except (json.JSONDecodeError, TypeError):
            continue
        for ref in refs:
            doc_id = ref.get("doc_id") if isinstance(ref, dict) else None
            if doc_id:
                counts[doc_id] = counts.get(doc_id, 0) + 1
    return counts


# ---------------------------------------------------------------- 文档列表/详情

@router.get("/spaces/{space_id}/docs")
async def list_docs(
    space_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
    _perm: Annotated[User, Depends(_require_perm("knowledge_base:space:list"))],
):
    """空间文档列表（读权限=成员级）。含每份文档拆出的词条数。"""
    ok, msg = await ensure_space_access(user.id, space_id, need_write=False)
    if not ok:
        raise HTTPException(status_code=403, detail=msg)
    await _space_or_404(db, space_id)

    docs = list((await db.execute(
        select(KbDocSource).where(KbDocSource.space_id == space_id)
        .order_by(KbDocSource.id.desc())
    )).scalars().all())
    counts = await _doc_page_counts(db, space_id)
    return success_response(data={
        "items": [_doc_out(d, counts.get(d.id, 0)) for d in docs],
        "space": await _space_summary(db, space_id),
    })


async def _space_summary(db: AsyncSession, space_id: int) -> dict:
    from sqlalchemy import func

    space = await _space_or_404(db, space_id)
    doc_count = (await db.execute(
        select(func.count()).select_from(KbDocSource)
        .where(KbDocSource.space_id == space_id)
    )).scalar_one()
    page_count = (await db.execute(
        select(func.count()).select_from(KbWikiPage).where(
            KbWikiPage.space_id == space_id, KbWikiPage.is_deleted.is_(False)
        )
    )).scalar_one()
    return {
        "id": space.id,
        "name": space.name,
        "description": space.description,
        "visibility": space.visibility,
        "owner_user_id": space.owner_user_id,
        "doc_count": doc_count,
        "page_count": page_count,
    }


@router.get("/spaces/{space_id}/docs/{doc_id}")
async def doc_detail(
    space_id: int,
    doc_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
    _perm: Annotated[User, Depends(_require_perm("knowledge_base:space:list"))],
):
    """文档全文 + 拆出的词条列表（双向溯源的文档侧）。"""
    ok, msg = await ensure_space_access(user.id, space_id, need_write=False)
    if not ok:
        raise HTTPException(status_code=403, detail=msg)

    doc = (await db.execute(
        select(KbDocSource).where(
            KbDocSource.id == doc_id, KbDocSource.space_id == space_id
        )
    )).scalars().first()
    if doc is None:
        raise HTTPException(status_code=404, detail="文档不存在")

    pages = list((await db.execute(
        select(KbWikiPage).where(
            KbWikiPage.space_id == space_id, KbWikiPage.is_deleted.is_(False)
        )
    )).scalars().all())
    derived = []
    for p in pages:
        try:
            refs = json.loads(p.source_refs or "[]")
        except (json.JSONDecodeError, TypeError):
            continue
        if any(isinstance(r, dict) and r.get("doc_id") == doc_id for r in refs):
            derived.append({
                "id": p.id, "slug": p.slug, "title": p.title,
                "status": p.status, "version": p.version,
            })
    return success_response(data={**_doc_out(doc, len(derived)), "content": doc.content, "pages": derived})


# ---------------------------------------------------------------- 上传（文件/文本）

@router.post("/spaces/{space_id}/docs/upload")
async def upload_doc_file(
    space_id: int,
    file: Annotated[UploadFile, File()],
    user: Annotated[User, Depends(get_current_user)],
    _perm: Annotated[User, Depends(_require_perm("knowledge_base:doc:upload"))],
):
    """文件上传入库：md/txt 直读，pdf（pypdf）、docx（python-docx）解析为文本。

    原始文件字节存插件上传目录（uploads/plugins/knowledge_base/docs/），
    下载时优先返回原文件（PDF 下载回来还是排版完好的 PDF）；
    无原始文件（粘贴文本/聊天结论/爬取）下载给 Markdown 文本。
    """
    data = await file.read()
    try:
        title, content = parse_file_to_text(file.filename or "", data)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    ext = ""
    fname = file.filename or ""
    if "." in fname:
        ext = fname.rsplit(".", 1)[1].lower()[:10]

    result = await ingest_doc(
        user.id, space_id, title, content,
        source_type="upload", url=None, file_ext=ext or None,
    )
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("msg"))

    # 保存原始文件字节（幂等：同内容重复上传复用同一 doc）
    if ext:
        _save_raw_file(result.get("doc_id"), ext, data)
    return success_response(data=result, msg="上传成功，已入编译队列")


def _raw_dir() -> Path:
    from src.core.config import settings

    d = Path(settings.PLUGINS_UPLOAD_DIR) / "knowledge_base" / "docs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _save_raw_file(doc_id: int | None, ext: str, data: bytes) -> None:
    if not doc_id or not ext or not data:
        return
    try:
        (_raw_dir() / f"{doc_id}.{ext}").write_bytes(data)
    except OSError as exc:
        logger.warning(f"[KnowledgeBase] 原始文件保存失败 doc={doc_id}: {exc}")


@router.post("/spaces/{space_id}/docs")
async def post_doc(
    space_id: int,
    body: dict,
    user: Annotated[User, Depends(get_current_user)],
    _perm: Annotated[User, Depends(_require_perm("knowledge_base:doc:upload"))],
):
    """文本/URL 文档入库（原 JSON 入口，迁移到 doc:upload 权限）。"""
    result = await ingest_doc(
        user.id, space_id, body.get("title", ""), body.get("content", ""),
        body.get("source_type", "upload"), body.get("url"),
    )
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("msg"))
    return success_response(data=result)


# ---------------------------------------------------------------- 下载

_DOC_DOWNLOAD_EXT = {
    "md": "text/markdown",
    "markdown": "text/markdown",
    "txt": "text/plain",
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}


@router.get("/spaces/{space_id}/docs/{doc_id}/download")
async def download_doc(
    space_id: int,
    doc_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
    _perm: Annotated[User, Depends(_require_perm("knowledge_base:space:list"))],
):
    """下载文档：优先返回上传的原始文件字节（PDF 还是排版完好的 PDF）。

    回退链：原始文件存在 → 原文件（原始扩展名）；
    无原始文件（粘贴文本/聊天结论/爬取/调研）→ Markdown 文本（.md）。
    """
    ok, msg = await ensure_space_access(user.id, space_id, need_write=False)
    if not ok:
        raise HTTPException(status_code=403, detail=msg)

    doc = (await db.execute(
        select(KbDocSource).where(
            KbDocSource.id == doc_id, KbDocSource.space_id == space_id
        )
    )).scalars().first()
    if doc is None:
        raise HTTPException(status_code=404, detail="文档不存在")

    # 文件名：去除路径分隔符防注入，空格/斜杠替换
    safe_title = (doc.title or "document").replace("/", "_").replace("\\", "_").replace(" ", "_")[:80]

    from urllib.parse import quote

    # ① 原始文件（上传时保存了字节）
    raw_ext = (doc.file_ext or "").lower()
    if raw_ext:
        raw_path = _raw_dir() / f"{doc.id}.{raw_ext}"
        if raw_path.is_file():
            raw = raw_path.read_bytes()
            quoted = quote(f"{safe_title}.{raw_ext}")
            media = _DOC_DOWNLOAD_EXT.get(raw_ext, "application/octet-stream")
            from fastapi.responses import Response

            return Response(
                content=raw,
                media_type=media,
                headers={
                    "Content-Disposition": f"attachment; filename*=UTF-8''{quoted}",
                },
            )

    # ② 文本型回退：Markdown
    from fastapi.responses import Response

    ext = "md"
    media_type = _DOC_DOWNLOAD_EXT[ext]
    filename = f"{safe_title}.{ext}"
    quoted = quote(filename)
    return Response(
        content=doc.content,
        media_type=f"{media_type}; charset=utf-8",
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quoted}",
        },
    )


# ---------------------------------------------------------------- 删除/重新编译

@router.delete("/spaces/{space_id}/docs/{doc_id}")
async def delete_doc(
    space_id: int,
    doc_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
    _perm: Annotated[User, Depends(_require_perm("knowledge_base:doc:delete"))],
):
    """删除文档（物理删除「料」；已编译出的词条保留——它们是独立成品）。"""
    ok, msg = await ensure_space_access(user.id, space_id, need_write=True)
    if not ok:
        raise HTTPException(status_code=403, detail=msg)

    doc = (await db.execute(
        select(KbDocSource).where(
            KbDocSource.id == doc_id, KbDocSource.space_id == space_id
        )
    )).scalars().first()
    if doc is None:
        raise HTTPException(status_code=404, detail="文档不存在")
    await db.delete(doc)
    await db.commit()
    return success_response(msg="文档已删除（已编译词条保留）")


@router.post("/spaces/{space_id}/docs/{doc_id}/compile")
async def compile_doc(
    space_id: int,
    doc_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
    _perm: Annotated[User, Depends(_require_perm("knowledge_base:doc:compile"))],
):
    """重新编译一份文档 → 拆解/合并词条（复用 wiki_ingest 任务管线）。"""
    ok, msg = await ensure_space_access(user.id, space_id, need_write=True)
    if not ok:
        raise HTTPException(status_code=403, detail=msg)

    doc = (await db.execute(
        select(KbDocSource).where(
            KbDocSource.id == doc_id, KbDocSource.space_id == space_id
        )
    )).scalars().first()
    if doc is None:
        raise HTTPException(status_code=404, detail="文档不存在")

    from src.plugins.builtin.knowledge_base.pipeline.jobs import enqueue_job

    try:
        job_id = await enqueue_job(
            user.id, "wiki_ingest",
            {"space_id": space_id, "doc_ids": [doc_id]},
            space_id=space_id,
        )
    except RuntimeError as exc:  # 成本护栏：当日任务超限
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    return success_response(data={"job_id": job_id}, msg="编译任务已入队")
