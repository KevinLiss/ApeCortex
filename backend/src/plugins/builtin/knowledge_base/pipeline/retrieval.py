"""检索层（P1）：双通道召回——私库（记忆+词条）与公开库词条合并。

.. design:: SQL 硬过滤防泄漏（规划 10.4）
   - 私库通道：user_id == 当前用户（权重 1.0）
   - 公开通道：space.visibility == 'public'（权重 0.9）
   private 内容的召回路径在 SQL 层不存在——不是 if 判断防泄漏。
"""


from loguru import logger
from sqlalchemy import select

from src.db import SessionLocal


def _scored_hit(text: str, query_words: list[str], base: float) -> float:
    """关键词覆盖率打分（v1 关键词匹配；FTS5 后续版本）。"""
    if not query_words:
        return base
    hit = sum(1 for w in query_words if w and w in text)
    return base * (0.4 + 0.6 * (hit / len(query_words)))


def _split_words(query: str) -> list[str]:
    import re
    words = [w for w in re.split(r"\s+", query.strip()) if w]
    # 中文查询整串也算一个词（子串匹配）
    if query.strip() and len(query.strip()) >= 2:
        words.append(query.strip())
    return words


async def search_all(
    user_id: int,
    query: str,
    limit_per_channel: int = 5,
) -> dict:
    """双通道检索：私有词条 + 公开词条 + 记忆（记忆复用 memory.search_memories）。

    返回 {"private_pages": [...], "public_pages": [...], "memories": [...], "profile_excerpt": str}
    """
    from src.plugins.builtin.knowledge_base.models import KbSpace, KbWikiPage
    from src.plugins.builtin.knowledge_base.pipeline.memory import search_memories

    q = query.strip()
    words = _split_words(q) if q else []
    limit = max(1, min(limit_per_channel, 10))

    async with SessionLocal() as db:
        # 私有词条（自己的空间，全部状态可见，软删排除）
        private_cond = (
            KbSpace.owner_user_id == user_id,
            KbSpace.status == "active",
        )
        public_cond = (
            KbSpace.visibility == "public",
            KbSpace.status == "active",
            # 公开库只召回已发布内容（candidate/draft 不外泄）
            KbWikiPage.status == "live",
        )

        private_rows = list((
            await db.execute(
                select(KbWikiPage, KbSpace.name, KbSpace.visibility)
                .join(KbSpace, KbWikiPage.space_id == KbSpace.id)
                .where(
                    *private_cond,
                    KbWikiPage.is_deleted == False,  # noqa: E712
                )
                .order_by(KbWikiPage.source_count.desc(), KbWikiPage.updated_at.desc())
                .limit(200)
            )
        ).all())
        private_pages = [(p, {"id": p.space_id, "name": name, "visibility": vis}) for p, name, vis in private_rows]

        public_rows = list((
            await db.execute(
                select(KbWikiPage, KbSpace.name, KbSpace.visibility)
                .join(KbSpace, KbWikiPage.space_id == KbSpace.id)
                .where(
                    *public_cond,
                    KbWikiPage.is_deleted == False,  # noqa: E712
                )
                .order_by(KbWikiPage.source_count.desc(), KbWikiPage.updated_at.desc())
                .limit(200)
            )
        ).all())
        public_pages = [(p, {"id": p.space_id, "name": name, "visibility": vis}) for p, name, vis in public_rows]

        def _fmt(p: KbWikiPage, space: dict) -> dict:
            return {
                "id": p.id, "slug": p.slug, "title": p.title,
                "snippet": p.body_md[:200],
                "space": space, "visibility": space["visibility"],
                "origin_author": p.origin_author_id,
            }

        def _space_of(space_id: int) -> dict:
            if space_id not in space_cache:
                space_cache[space_id] = {"id": space_id, "name": "?", "visibility": "?"}
            return space_cache[space_id]

        def _rank(pages, weight):
            scored = [
                (p, sp, _scored_hit(p.title + p.body_md[:500], words, weight))
                for p, sp in pages
            ]
            scored = [(p, sp, s) for p, sp, s in scored if q == "" or s > 0.36]
            scored.sort(key=lambda x: -x[2])
            return [_fmt(p, sp) for p, sp, _ in scored[:limit]]

        private_out = _rank(private_pages, 1.0)
        public_out = _rank(public_pages, 0.9)

    memories, profile_excerpt = await search_memories(user_id, q, limit)

    return {
        "private_pages": private_out,
        "public_pages": public_out,
        "memories": memories,
        "profile_excerpt": profile_excerpt,
    }
