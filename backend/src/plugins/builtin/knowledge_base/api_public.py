"""公开库只读出口（M3）：外部 AI / 站点以只读凭证访问已发布的公开词条。

.. important:: 边界
   - 只读：无任何写入端点。
   - 只见 ``visibility == 'public'`` 且 ``status == 'live'`` 的词条；
     draft / candidate / archived、私有/共享空间内容在此路由的 SQL 中不存在读取路径。
   - 鉴权：``X-Kb-Key`` 只读凭证（sha256 存库，明文仅创建时返回一次）。
   - 限流：每凭证 60 秒 10 次（进程内窗口，多 worker 为尽力而为）。
   - 审计：累计请求数与最近使用时间写回凭证行，异常只记日志不抛。
"""
import hashlib
import time

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from loguru import logger
from sqlalchemy import select

from src.db import get_db

router = APIRouter(prefix="/knowledge-base/public", tags=["ApeCortex 公开库出口"])

_RATE_LIMIT = 10
_RATE_WINDOW = 60.0
_hits: dict[int, list[float]] = {}


def hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _rate_ok(key_id: int) -> bool:
    now = time.monotonic()
    bucket = [t for t in _hits.get(key_id, []) if now - t < _RATE_WINDOW]
    if len(bucket) >= _RATE_LIMIT:
        _hits[key_id] = bucket
        return False
    bucket.append(now)
    _hits[key_id] = bucket
    return True


async def _resolve_key(db, raw: str | None):
    from src.plugins.builtin.knowledge_base.models import KbPublicKey

    if not raw:
        raise HTTPException(status_code=401, detail="缺少 X-Kb-Key")
    row = (await db.execute(
        select(KbPublicKey).where(KbPublicKey.key_hash == hash_key(raw))
    )).scalars().first()
    if row is None or not row.enabled:
        raise HTTPException(status_code=401, detail="凭证无效或已停用")
    return row


async def _touch_key(db, row) -> None:
    from datetime import datetime, timezone

    row.request_count += 1
    row.last_used_at = datetime.now(timezone.utc)
    await db.commit()


@router.get("/search")
async def public_search(
    q: str = Query(default="", max_length=100),
    limit: int = Query(default=5, ge=1, le=20),
    x_kb_key: str | None = Header(default=None, alias="X-Kb-Key"),
    db=Depends(get_db),
):
    """按关键词检索公开库 live 词条（零命中不返回）。"""
    from src.plugins.builtin.knowledge_base.models import KbSpace, KbWikiPage
    from src.plugins.builtin.knowledge_base.pipeline.retrieval import _scored_hit, _split_words

    key_row = await _resolve_key(db, x_kb_key)
    if not _rate_ok(key_row.id):
        raise HTTPException(status_code=429, detail="请求过于频繁（每凭证 10 次/分钟）")

    rows = list((await db.execute(
        select(KbWikiPage, KbSpace.name)
        .join(KbSpace, KbWikiPage.space_id == KbSpace.id)
        .where(
            KbSpace.visibility == "public",
            KbSpace.status == "active",
            KbWikiPage.status == "live",
            KbWikiPage.is_deleted == False,  # noqa: E712
        )
        .order_by(KbWikiPage.source_count.desc())
        .limit(200)
    )).all())

    query = q.strip()
    words = _split_words(query) if query else []
    scored = []
    for page, space_name in rows:
        score, coverage = _scored_hit(page.title + page.body_md[:500], words, 1.0)
        if query and coverage <= 0:
            continue
        scored.append((page, space_name, score, coverage))
    scored.sort(key=lambda x: -x[2])

    await _touch_key(db, key_row)
    logger.info(f"[ApeCortex] 公开库出口检索 key={key_row.id} q={query[:40]} hits={len(scored)}")

    return {
        "total": len(scored),
        "items": [
            {
                "slug": p.slug,
                "title": p.title,
                "snippet": p.body_md[:200],
                "space": space_name,
                "relevance": round(score, 2),
                "origin_author_id": p.origin_author_id,
                "updated_at": p.updated_at.isoformat() if p.updated_at else None,
            }
            for p, space_name, score, _ in scored[:limit]
        ],
    }


@router.get("/pages/{slug}")
async def public_page(
    slug: str,
    x_kb_key: str | None = Header(default=None, alias="X-Kb-Key"),
    db=Depends(get_db),
):
    """按 slug 读取单篇公开 live 词条全文。"""
    from src.plugins.builtin.knowledge_base.models import KbSpace, KbWikiPage

    key_row = await _resolve_key(db, x_kb_key)
    if not _rate_ok(key_row.id):
        raise HTTPException(status_code=429, detail="请求过于频繁（每凭证 10 次/分钟）")

    row = (await db.execute(
        select(KbWikiPage, KbSpace.name)
        .join(KbSpace, KbWikiPage.space_id == KbSpace.id)
        .where(
            KbSpace.visibility == "public",
            KbSpace.status == "active",
            KbWikiPage.status == "live",
            KbWikiPage.is_deleted == False,  # noqa: E712
            KbWikiPage.slug == slug,
        )
    )).first()
    if row is None:
        raise HTTPException(status_code=404, detail="词条不存在")

    page, space_name = row
    await _touch_key(db, key_row)
    return {
        "slug": page.slug,
        "title": page.title,
        "body_md": page.body_md,
        "space": space_name,
        "origin_author_id": page.origin_author_id,
        "version": page.version,
        "updated_at": page.updated_at.isoformat() if page.updated_at else None,
    }