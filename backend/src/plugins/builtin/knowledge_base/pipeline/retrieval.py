"""检索层（P1）：双通道召回——私库（记忆+词条）与公开库词条合并。

.. design:: SQL 硬过滤防泄漏（规划 10.4）
   - 私库通道：user_id == 当前用户（权重 1.0）
   - 公开通道：space.visibility == 'public'（权重 0.9）
   private 内容的召回路径在 SQL 层不存在——不是 if 判断防泄漏。
"""


from sqlalchemy import select

from src.db import SessionLocal

# 相关性纪律（M1）：零命中一律不返回（宁缺毋滥），低覆盖率只标注不丢弃
_LOW_RELEVANCE_COVERAGE = 0.5


def _scored_hit(text: str, query_words: list[str], base: float) -> tuple[float, float]:
    """关键词覆盖率打分 → (score, coverage)。coverage=0 表示完全不相关。"""
    if not query_words:
        return base, 1.0
    hit = sum(1 for w in query_words if w and w in text)
    coverage = hit / len(query_words)
    return base * (0.4 + 0.6 * coverage), coverage


# 轻量同义词扩展（M4）：中英别名互扩，提升跨语言/缩写召回；不做会误命中的 2-gram 泛化
_SYNONYMS: dict[str, tuple[str, ...]] = {
    "rag": ("检索增强", "retrieval augmented"),
    "检索增强": ("rag",),
    "llm": ("大模型", "语言模型"),
    "大模型": ("llm",),
    "mcp": ("工具协议", "model context protocol"),
    "向量": ("embedding", "语义检索"),
    "embedding": ("向量",),
    "知识库": ("knowledge base", "kb"),
}


def _split_words(query: str) -> list[str]:
    import re
    words = [w for w in re.split(r"\s+", query.strip()) if w]
    # 中文查询整串也算一个词（子串匹配）
    if query.strip() and len(query.strip()) >= 2:
        words.append(query.strip())
    # 同义词扩展（仅对命中词表的关键词）
    expanded = list(words)
    for w in words:
        for syn in _SYNONYMS.get(w.lower(), ()):
            expanded.append(syn)
    return list(dict.fromkeys(expanded))


def _score_page(title: str, body: str, query_words: list[str], base: float) -> tuple[float, float]:
    """字段加权打分（M4）：标题命中权重 2、正文 1；coverage 取"有命中的查询词比例"。"""
    if not query_words:
        return base, 1.0
    title_hit = sum(1 for w in query_words if w and w in title)
    body_hit = sum(1 for w in query_words if w and w in body)
    weighted = (2 * title_hit + body_hit) / (3 * len(query_words))
    coverage = sum(1 for w in query_words if w and (w in title or w in body)) / len(query_words)
    return base * (0.4 + 0.6 * weighted), coverage


async def search_all(
    user_id: int,
    query: str,
    limit_per_channel: int = 5,
    space_ids: list[int] | None = None,
    include_hub: bool = True,
) -> dict:
    """检索：个人空间词条 + 知识中心词条 + 记忆（记忆复用 memory.search_memories）。

    返回 {"private_pages": [...], "hub_pages": [...], "memories": [...], "profile_excerpt": str}

    周期三：个人空间（owner 硬过滤）+ 知识中心（hub space，live-only）；
    shared 通道下线。space_ids/include_hub 是聊天偏好启用集合的过滤器。
    """
    from src.plugins.builtin.knowledge_base.models import KbSpace, KbWikiPage
    from src.plugins.builtin.knowledge_base.pipeline.hub import get_hub_space_id
    from src.plugins.builtin.knowledge_base.pipeline.memory import search_memories

    q = query.strip()
    words = _split_words(q) if q else []
    limit = max(1, min(limit_per_channel, 10))

    hub_space_id = await get_hub_space_id() if include_hub else None

    async with SessionLocal() as db:
        # 个人空间词条（owner 硬过滤；space_ids 为空=全部自己的空间）
        private_cond = [
            KbSpace.owner_user_id == user_id,
            KbSpace.status == "active",
            KbSpace.id != hub_space_id if hub_space_id else True,  # type: ignore[arg-type]
        ]
        if space_ids is not None:
            if not space_ids:
                private_cond = None
            else:
                private_cond.append(KbSpace.id.in_(space_ids))
        private_pages = []
        if private_cond:
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

        # 知识中心词条（hub space，live-only：candidate/draft 不外泄）
        hub_pages = []
        if hub_space_id:
            hub_rows = list((
                await db.execute(
                    select(KbWikiPage, KbSpace.name, KbSpace.visibility)
                    .join(KbSpace, KbWikiPage.space_id == KbSpace.id)
                    .where(
                        KbWikiPage.space_id == hub_space_id,
                        KbSpace.status == "active",
                        KbWikiPage.status == "live",
                        KbWikiPage.is_deleted == False,  # noqa: E712
                    )
                    .order_by(KbWikiPage.source_count.desc(), KbWikiPage.updated_at.desc())
                    .limit(200)
                )
            ).all())
            hub_pages = [(p, {"id": p.space_id, "name": name, "visibility": vis}) for p, name, vis in hub_rows]

        def _fmt(p: KbWikiPage, space: dict) -> dict:
            return {
                "id": p.id, "slug": p.slug, "title": p.title,
                "snippet": p.body_md[:200],
                "space": space, "visibility": space["visibility"],
                "origin_author": p.origin_author_id,
            }

        def _rank(pages, weight):
            scored = []
            for p, sp in pages:
                s, cov = _score_page(p.title, p.body_md[:500], words, weight)
                if q and cov <= 0:
                    continue  # 零命中：不返回兜底垃圾（M1 相关性纪律）
                scored.append((p, sp, s, cov))
            scored.sort(key=lambda x: (-x[2], -x[0].source_count))
            out = []
            for p, sp, s, cov in scored[:limit]:
                item = _fmt(p, sp)
                item["relevance"] = round(s, 2)
                item["low_relevance"] = bool(q) and cov < _LOW_RELEVANCE_COVERAGE
                out.append(item)
            return out

        private_out = _rank(private_pages, 1.0)
        hub_out = _rank(hub_pages, 0.9)

    memories, profile_excerpt = await search_memories(user_id, q, limit)

    return {
        "private_pages": private_out,
        "hub_pages": hub_out,
        # 兼容键（周期三前的调用方）
        "public_pages": hub_out,
        "shared_pages": [],
        "memories": memories,
        "profile_excerpt": profile_excerpt,
    }
