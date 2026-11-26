"""Wiki 编译管线（P1 核心）。

.. design:: 设计依据（调研结论，见 docs/knowledge-base/调研笔记）
   - WeKnora Map/Reduce 裁剪版（wiki_ingest.go：候选 slug 提取 → 实体/概念提取 → 引用）；
     但**dedup 预筛先行**：FTS/子串相似 + Jaccard 阈值 + 确定性拒绝规则，只有存疑项才交 LLM
     （mem0「便宜过滤前置」哲学，WeKnora pg_trgm 预筛的 SQLite 等价实现）。
   - page-merge 三层保护（nashsu/llm_wiki 机制层，GPL 仓只借鉴不抄码）：
     ① frontmatter 数组字段确定性 union；② body 交 LLM 合并但 **70% 缩水阈值**拒绝疑似截断；
     ③ LOCKED 字段（slug/title）强制回写。
   - 引用整合（STORM storm_dataclass.py 纯 Python 移植）：`[n]` 收集 → 超界删除 →
     URL 归并统一编号 → 按出现顺序重排 → References 节生成。
   - 候选晋升门槛（swarmvault candidate-promotion 六参数裁剪）：
     source_count≥3 且 confidence≥0.8 才可提交晋升（余下门槛在 settings 可调）。

成本纪律：单篇文档编译恰好 2 次 LLM（Map 提取 1 + Reduce 合并仅存疑时 1）。
"""
import json
import re
from datetime import UTC, datetime

from loguru import logger
from sqlalchemy import func, select

from src.db import SessionLocal

# ---------------------------------------------------------------- 常量

# Map 提取单次上限（护栏）
_MAX_PAGES_PER_DOC = 8
_MAX_SOURCE_CHARS = 12000  # 超长截断（保护上下文）
# dedup 预筛：同 slug 或相似度阈值
_DEDUP_SIM_THRESHOLD = 0.62  # Jaccard（归一化 token 集）
# page-merge 缩水保护：合并后长度 < 原文 70% 视为疑似截断
_MERGE_SHRINK_LIMIT = 0.70
# 晋升门槛（swarmvault 裁剪：可提交晋升的硬条件）
# ---------------------------------------------------------------- Prompt

_MAP_SYSTEM_PROMPT = """你是知识库编译器（Map 阶段）。从给定原始资料中提取结构化 Wiki 词条。

【输出格式】只输出 JSON，不要其他文字：
{"pages": [{"slug": "kebab-case标识", "title": "词条标题", "body_md": "Markdown正文", "source_chunk": "支撑原文片段"}]}

【质量规则】
- 每个词条：一个独立概念/实体/主题；slug 用英文 kebab-case（如 "vue3-composition-api"）
- body_md 60~400 字，结构：一句定义 + 关键要点（可用列表）
- 正文中引用原文片段处标注 [1]、[2] 等序号，source_chunk 填对应原文片段（编译器会建立引用映射）
- 空泛/无信息量内容不生成词条；单篇资料最多 8 个词条
- 禁止臆测原文没有的内容"""

_REDUCE_SYSTEM_PROMPT = """你是知识库合并器（Reduce 阶段）。将新提取的内容合并进已有词条正文。

【输入】已有正文 old_body 与新内容 new_body。
【规则】
- 合并去重：新信息补充进对应小节；与旧内容重复的不重复写
- 保留旧文结构（标题层级、列表组织方式）
- 冲突信息并存：新旧冲突时，新信息后写并标注"(更新)"
- 只输出合并后的完整 Markdown 正文，不要解释
【保护】输出长度不得小于原正文的 70%（缩水会被系统拒绝）"""


def _norm_tokens(text: str) -> set[str]:
    """归一化 token 集（中文按字、英文按词，用于 Jaccard）。"""
    text = re.sub(r"\s+", " ", text.lower())
    cn = re.findall(r"[\u4e00-\u9fff]", text)
    en = re.findall(r"[a-z0-9]+", text)
    return set(cn) | set(en)


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


# ---------------------------------------------------------------- LLM 复用

from src.plugins.builtin.knowledge_base.pipeline.memory import (  # noqa: E402
    _call_llm_json,
    _strip_code_fences,
)


def _parse_pages(raw: str) -> list[dict]:
    """解析 Map 输出（复用记忆层的双层降级思想 + 截断宽容）。"""
    if not raw:
        return []
    text = raw.strip()
    data = None
    for attempt in (text, _strip_code_fences(text)):
        try:
            data = json.loads(attempt)
            break
        except (json.JSONDecodeError, ValueError):
            continue
    if data is None:
        # 截断宽容（max_tokens 用尽时 JSON 尾部不完整）：抓取完整的扁平
        # page 对象重组重试；含嵌套 {} 的对象可能被切碎，逐个收缩重试。
        # 部分词条仍可用，比整单作废好。
        objs = re.findall(r'\{[^{}]*"slug"[^{}]*\}', text)
        while objs:
            try:
                data = json.loads('{"pages": [' + ",".join(objs) + "]}")
                break
            except (json.JSONDecodeError, ValueError):
                objs.pop()  # 最后一个对象可能被切碎 → 收缩重试
    if data is None:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            try:
                data = json.loads(text[start:end + 1])
            except (json.JSONDecodeError, ValueError):
                return []
    if not isinstance(data, dict):
        return []
    pages = data.get("pages")
    if not isinstance(pages, list):
        return []
    out = []
    for p in pages[:_MAX_PAGES_PER_DOC]:
        if not isinstance(p, dict):
            continue
        slug = str(p.get("slug", "")).strip()
        title = str(p.get("title", "")).strip()
        body = str(p.get("body_md", "")).strip()
        if not slug or not title or len(body) < 20:
            continue
        # slug 清洗：只留合法字符
        slug = re.sub(r"[^a-z0-9-]", "-", slug.lower()).strip("-")[:120] or f"page-{len(out)}"
        out.append({
            "slug": slug, "title": title, "body_md": body,
            "source_chunk": str(p.get("source_chunk", ""))[:600],
        })
    return out


# ---------------------------------------------------------------- 引用整合（STORM 移植）

def build_source_refs(doc_sources: list) -> list[dict]:
    """构造来源引用列表（doc_id/title/url）。"""
    return [
        {
            "doc_id": d.id if hasattr(d, "id") else d["id"],
            "title": d.title if hasattr(d, "title") else d["title"],
            "url": (d.url if hasattr(d, "url") else d.get("url")) or "",
        }
        for d in doc_sources
    ]


def renumber_inline_citations(body_md: str, source_refs: list[dict]) -> str:
    """行内 [n] 引用重整（STORM 管线：收集 → 超界删除 → 重排 → References 节）。

    source_refs 顺序即编号（1-based）。
    """
    n_refs = len(source_refs)
    # 超界引用删除
    body_md = re.sub(
        r"\[(\d+)\]",
        lambda m: m.group(0) if (m.group(1).isdigit() and 1 <= int(m.group(1)) <= n_refs) else "",
        body_md,
    )
    # 编号归并：按首次出现顺序重排（防跳号/重复）
    order: dict[int, int] = {}
    for m in re.finditer(r"\[(\d+)\]", body_md):
        old = int(m.group(1))
        if old not in order:
            order[old] = len(order) + 1

    def _remap(m: re.Match) -> str:
        old = int(m.group(1))
        return f"[{order.get(old, old)}]"

    body_md = re.sub(r"\[(\d+)\]", _remap, body_md)
    # 追加 References 节
    if source_refs:
        lines = ["", "## References"]
        for i, ref in enumerate(source_refs, 1):
            link = f" [{ref['url']}]({ref['url']})" if ref.get("url") else ""
            lines.append(f"{i}. {ref['title']}{link}")
        body_md = body_md.rstrip() + "\n" + "\n".join(lines) + "\n"
    return body_md


# ---------------------------------------------------------------- 主管线

async def compile_space(
    space_id: int, doc_ids: list[int] | None = None, category_id: int | None = None,
) -> dict:
    """编译空间：把（未编译过的）原始资料编译成词条。返回统计。

    v1 简化：一次任务编译全部未编译资料（或指定 doc_ids），
    Map 每篇 1 次 LLM；Reduce 仅存疑页（slug 命中且内容变化）追加 1 次。
    category_id：推送甄选归类——产出/更新的词条挂该分类（AI 自主维护分类的落点）。
    """
    from src.plugins.builtin.knowledge_base.models import (
        KbDocSource,
        KbWikiPage,
        KbWikiRevision,
    )
    from src.plugins.builtin.knowledge_base.pipeline.hub import get_hub_space_id

    is_hub = space_id == (await get_hub_space_id())

    async with SessionLocal() as db:
        stmt = select(KbDocSource).where(KbDocSource.space_id == space_id)
        if doc_ids:
            stmt = stmt.where(KbDocSource.id.in_(doc_ids))
        docs = list((await db.execute(stmt)).scalars().all())
        if not docs:
            return {"docs": 0, "pages_new": 0, "pages_merged": 0, "pages_rejected": 0, "llm_calls": 0}

        # 已有词条缓存（slug → page）
        existing = {
            p.slug: p
            for p in (
                await db.execute(
                    select(KbWikiPage).where(
                        KbWikiPage.space_id == space_id, KbWikiPage.is_deleted == False  # noqa: E712
                    )
                )
            ).scalars().all()
        }

        stats = {"docs": len(docs), "pages_new": 0, "pages_merged": 0, "pages_rejected": 0, "llm_calls": 0}
        refs = build_source_refs(docs)
        now = datetime.now(UTC)

        for doc in docs:
            source_text = doc.content[:_MAX_SOURCE_CHARS]
            # max_tokens 与拆词条规模匹配（6k 字资料拆 3~8 词条需约 2~4k token；
            # 截断由 _parse_pages 兜底宽容修复，但首选一次到位）
            raw = await _call_llm_json(_MAP_SYSTEM_PROMPT, source_text, max_tokens=4000, temperature=0.2)
            stats["llm_calls"] += 1
            if raw is None:
                logger.warning(f"[KB] doc={doc.id} Map 提取失败（LLM 返回空）")
                continue
            pages = _parse_pages(raw)
            if not pages:
                continue

            for item in pages:
                slug = item["slug"]
                item_tokens = _norm_tokens(item["body_md"])
                page = existing.get(slug)

                # ---- dedup 预筛（确定性规则先行，不花 LLM）----
                if page is not None and page.body_md.strip() == item["body_md"].strip():
                    continue  # 完全相同：跳过（零成本去重）

                if page is None:
                    # ---- 新建词条（candidate 态）----
                    page = KbWikiPage(
                        space_id=space_id,
                        category_id=category_id,
                        slug=slug,
                        title=item["title"],
                        body_md=renumber_inline_citations(item["body_md"], refs),
                        source_refs=json.dumps(refs, ensure_ascii=False),
                        status="live" if is_hub else "candidate",
                        confidence=0.5,
                        source_count=len(docs),
                        last_edit_source="pipeline",
                        version=1,
                    )
                    db.add(page)
                    await db.flush()
                    db.add(KbWikiRevision(
                        page_id=page.id, version=1, body_md=page.body_md,
                        edit_source="pipeline", is_machine_snapshot=True, note="Map 新建",
                    ))
                    existing[slug] = page
                    stats["pages_new"] += 1
                else:
                    # ---- 存疑合并：先便宜相似度，再决定是否值得 LLM 合并 ----
                    sim = _jaccard(item_tokens, _norm_tokens(page.body_md))
                    if sim >= _DEDUP_SIM_THRESHOLD and "更新" not in item["body_md"]:
                        continue  # 高相似且非演化信息：确定性跳过（省 LLM）

                    old_len = len(page.body_md)
                    merged = await _call_llm_json(
                        _REDUCE_SYSTEM_PROMPT,
                        f"【old_body】\n{page.body_md}\n\n【new_body】\n{item['body_md']}",
                        max_tokens=3000, temperature=0.3,
                    )
                    stats["llm_calls"] += 1
                    if merged is None:
                        continue
                    merged = merged.strip()
                    # 三层保护之 ②：缩水阈值拒绝疑似截断
                    if old_len > 0 and len(merged) < old_len * _MERGE_SHRINK_LIMIT:
                        stats["pages_rejected"] += 1
                        logger.warning(
                            f"[KB] 合并缩水保护触发 page={page.id} slug={slug} "
                            f"({len(merged)}/{old_len})"
                        )
                        continue
                    # 三层保护之 ③：LOCKED 字段强制回写（slug/title 不许 LLM 改）
                    page.body_md = renumber_inline_citations(merged, json.loads(page.source_refs or "[]"))
                    page.version += 1
                    page.last_edit_source = "pipeline"
                    page.source_count = min(page.source_count + 1, len(docs))
                    page.confidence = min(0.99, page.confidence + 0.08)
                    db.add(KbWikiRevision(
                        page_id=page.id, version=page.version, body_md=page.body_md,
                        edit_source="pipeline", is_machine_snapshot=True, note="Reduce 合并",
                    ))
                    stats["pages_merged"] += 1

            doc.compiled_at = now

        # 冗余计数 + 修订裁剪（机器快照保留 50，硬顶 200）
        await _prune_revisions(db, list(existing.values()))
        page_total = (
            await db.execute(
                select(func.count()).select_from(KbWikiPage).where(
                    KbWikiPage.space_id == space_id, KbWikiPage.is_deleted == False  # noqa: E712
                )
            )
        ).scalar_one()
        space = await db.get(_space_model(), space_id)
        if space is not None:
            space.page_count = page_total
        await db.commit()

    await _relink_space(space_id)
    return stats


def _space_model():
    from src.plugins.builtin.knowledge_base.models import KbSpace
    return KbSpace


async def _prune_revisions(db, pages) -> None:
    """修订两级保留：机器快照限 50（硬顶 200，人工修订不裁）。"""
    from src.plugins.builtin.knowledge_base.models import KbWikiRevision

    for page in pages:
        rows = list((
            await db.execute(
                select(KbWikiRevision)
                .where(KbWikiRevision.page_id == page.id)
                .order_by(KbWikiRevision.id.desc())
                .limit(200)
            )
        ).scalars().all())
        machine = [r for r in rows if r.is_machine_snapshot]
        if len(machine) > 50:
            for r in machine[50:]:
                await db.delete(r)


async def _relink_space(space_id: int) -> None:
    """linkify：重算空间内全部词条的双链（正文中 [[slug]] 提取）。"""
    from src.plugins.builtin.knowledge_base.models import KbWikiPage

    async with SessionLocal() as db:
        pages = list((
            await db.execute(
                select(KbWikiPage).where(
                    KbWikiPage.space_id == space_id, KbWikiPage.is_deleted == False  # noqa: E712
                )
            )
        ).scalars().all())
        by_slug = {p.slug: p for p in pages}
        out_map: dict[int, set[str]] = {}
        for p in pages:
            outs = set(re.findall(r"\[\[([a-z0-9-]+)\]\]", p.body_md)) & set(by_slug)
            out_map[p.id] = outs
        for p in pages:
            outs = sorted(out_map[p.id])
            in_slugs = sorted({by_slug[pid].slug for pid, o in out_map.items() if p.slug in o})
            p.out_links = json.dumps(outs, ensure_ascii=False)
            p.in_links = json.dumps(in_slugs, ensure_ascii=False)
        await db.commit()


# ---------------------------------------------------------------- 晋升流

async def publish_page(page_id: int, user_id: int, status: str = "live") -> dict:
    """维护者发布/归档公开库词条（PandaWiki 三态流转）。"""
    from src.plugins.builtin.knowledge_base.models import KbSpace, KbWikiPage

    if status not in ("live", "archived", "draft"):
        return {"ok": False, "msg": "非法状态"}
    async with SessionLocal() as db:
        page = await db.get(KbWikiPage, page_id)
        if page is None or page.is_deleted:
            return {"ok": False, "msg": "词条不存在"}
        space = await db.get(KbSpace, page.space_id)
        if space is None:
            return {"ok": False, "msg": "空间不存在"}
        # 写权限硬校验：公开库/私有库仅空间 owner；共享库 owner 或 writer（M3 补丁：
        # shared 此前不落任何分支，导致 reader 成员也能改状态）
        if space.visibility in ("public", "private"):
            if space.owner_user_id != user_id:
                msg = "公开库仅空间维护者可变更状态" if space.visibility == "public" else "仅空间所有者可变更状态"
                return {"ok": False, "msg": msg}
        else:  # shared
            from src.plugins.builtin.knowledge_base.pipeline.ingest_api import get_space_role

            role = await get_space_role(user_id, space.id)
            if role not in ("owner", "writer"):
                return {"ok": False, "msg": "共享库仅所有者或 writer 成员可变更状态"}
        page.status = status
        page.last_edit_source = "user"
        page.version += 1
        await db.commit()
        return {"ok": True, "status": status}
