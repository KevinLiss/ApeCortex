"""深度调研管线（P2，STORM 骨架裁剪版）。

.. design:: 设计依据
   - STORM 五阶段裁到 3 阶段（视角发现/多视角对话砍掉——成本大头，调研默认 45-50 次
     LLM 砍到 8-15 次）：大纲（1 次）→ 逐段采集+成文（每段 1 次）
   - 产出落草稿态词条（research.draft），**人工确认后才转正**（危险操作确认原则）
   - 断点续跑：outline/sections 两阶段产物落 kb_job.result（STORM engine 断点设计）
   - 成本护栏：LLM 调用硬上限 + 采集 URL 上限 + token 预算粗计
"""
import json

from loguru import logger

# ---------------------------------------------------------------- 成本护栏

_MAX_LLM_CALLS = 15          # 单次调研 LLM 调用上限（熔断）
_MAX_SECTIONS = 5            # 大纲最多分节（每节 1 次 LLM 成文）
_MAX_QUERY_RESULTS = 3       # 每个检索词最多来源数
_TOTAL_TOKEN_BUDGET = 60000  # 粗估 token 预算（字符/3 估算，超限熔断）

# ---------------------------------------------------------------- Prompts

_OUTLINE_PROMPT = """你是调研大纲规划器。为给定主题生成一份调研报告大纲。

【输出】只输出 JSON：
{"title": "报告标题", "sections": [{"heading": "小节标题", "search_query": "该节需要的检索关键词"}]}

【规则】小节数量 3~5 节；每节 search_query 是明确的检索词（论文/网页都能搜）；
heading 简洁（10~20字）。"""

_SECTION_PROMPT = """你是调研报告撰写者。基于检索到的资料写一节内容。

【输出】只输出 Markdown 正文（60~400字），引用资料处标注 [n]（n 为资料序号）。
禁止臆测资料中没有的内容。"""


# ---------------------------------------------------------------- LLM 复用

from src.plugins.builtin.knowledge_base.pipeline.memory import (  # noqa: E402
    _call_llm_json,
    _strip_code_fences,
)


def _parse_outline(raw: str) -> dict | None:
    if not raw:
        return None
    text = _strip_code_fences(raw.strip())
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            return None
        try:
            data = json.loads(text[start:end + 1])
        except (json.JSONDecodeError, ValueError):
            return None
    sections = data.get("sections")
    if not isinstance(sections, list) or not sections:
        return None
    out_sections = []
    for s in sections[:_MAX_SECTIONS]:
        if not isinstance(s, dict):
            continue
        heading = str(s.get("heading", "")).strip()
        query = str(s.get("search_query", "")).strip()
        if heading:
            out_sections.append({"heading": heading[:80], "search_query": query or heading})
    if not out_sections:
        return None
    return {"title": str(data.get("title", ""))[:120] or "调研报告", "sections": out_sections}


async def run_research(
    user_id: int,
    space_id: int,
    topic: str,
    use_academic: bool = True,
    use_web: bool = False,
    progress_cb=None,  # async fn(step: str, pct: int)
) -> dict:
    """执行深度调研：大纲 → 逐节检索采集 → 成文 → 草稿词条落库。

    返回 {ok, job_meta, page_id, outline, sections}——page 为 draft 态，
    需人工在词条页确认发布（research.confirm 流程走 publish API）。
    """
    from src.db import SessionLocal
    from src.plugins.builtin.knowledge_base.collector import academic
    from src.plugins.builtin.knowledge_base.models import (
        KbDocSource,
        KbWikiPage,
        KbWikiRevision,
    )

    topic = topic.strip()[:200]
    if len(topic) < 4:
        return {"ok": False, "msg": "主题过短"}

    llm_calls = 0
    token_usage = {"in": 0, "out": 0}

    def _budget_ok() -> bool:
        return (token_usage["in"] + token_usage["out"]) * 3 < _TOTAL_TOKEN_BUDGET

    async def _llm(system: str, user: str, max_tokens: int) -> str | None:
        nonlocal llm_calls
        if llm_calls >= _MAX_LLM_CALLS or not _budget_ok():
            return None
        llm_calls += 1
        raw = await _call_llm_json(system, user, max_tokens=max_tokens, temperature=0.4)
        token_usage["in"] += len(system) + len(user) + 200
        token_usage["out"] += max_tokens // 2
        return raw

    # ---- 阶段 1：大纲 ----
    if progress_cb:
        await progress_cb("outline", 10)
    raw = await _llm(_OUTLINE_PROMPT, f"【主题】{topic}", 700)
    outline = _parse_outline(raw or "")
    if outline is None:
        return {"ok": False, "msg": "大纲生成失败（模型不可用或超预算）", "llm_calls": llm_calls}

    # ---- 阶段 2：逐节检索+成文 ----
    body_sections: list[dict] = []
    sources: list[dict] = []
    total_sections = len(outline["sections"])

    for idx, sec in enumerate(outline["sections"]):
        if progress_cb:
            await progress_cb(f"section {idx + 1}/{total_sections}", 15 + int(70 * idx / max(total_sections, 1)))

        query = sec["search_query"]
        # 检索：学术源（arXiv+OpenAlex 并行）→ 网页源（可选，需开关）
        results: list[dict] = []
        try:
            import asyncio
            tasks = [academic.search_arxiv(query, _MAX_QUERY_RESULTS),
                     academic.search_openalex(query, _MAX_QUERY_RESULTS)]
            if use_academic:
                sem = academic.search_semanticscholar(query, _MAX_QUERY_RESULTS)
                tasks.append(sem)
            batches = await asyncio.gather(*tasks, return_exceptions=True)
            for b in batches:
                if isinstance(b, list):
                    results.extend([r for r in b if "error" not in r])
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[KB] research 检索异常: {exc}")

        # 引用池（限 8 条/节）
        refs = []
        for r in results[:8]:
            refs.append({
                "title": r.get("title", "")[:120],
                "url": r.get("url") or r.get("doi") or "",
                "snippet": (r.get("abstract") or "")[:300],
            })
        sources.extend(refs)

        if not refs:
            body_sections.append({"heading": sec["heading"], "body": "*（本节未检索到可用资料）*"})
            continue

        # 成文（每节 1 次 LLM）
        refs_text = "\n".join(f"[{i + 1}] {r['title']}\n{r['snippet']}" for i, r in enumerate(refs))
        sec_raw = await _llm(_SECTION_PROMPT, f"【节标题】{sec['heading']}\n【检索资料】\n{refs_text}", 900)
        body = _strip_code_fences((sec_raw or "").strip())
        if not body:
            body = "*（本节生成失败）*"
        body_sections.append({"heading": sec["heading"], "body": body})

    # ---- 阶段 3：拼装报告 → 草稿词条 ----
    if progress_cb:
        await progress_cb("writing", 90)

    parts = [f"# {outline['title']}", ""]
    for sec in body_sections:
        parts.append(f"## {sec['heading']}")
        parts.append(sec["body"])
        parts.append("")
    report_md = "\n".join(parts).strip()

    # 来源引用节
    if sources:
        parts = ["", "## References"]
        seen_urls = set()
        n = 0
        for s in sources:
            u = s.get("url") or s.get("title")
            if u in seen_urls:
                continue
            seen_urls.add(u)
            n += 1
            link = f" [{s['url']}]({s['url']})" if s.get("url") else ""
            parts.append(f"{n}. {s['title']}{link}")
        report_md += "\n" + "\n".join(parts) + "\n"

    # 落库：原始资料 + 草稿词条（需人工确认发布）
    import hashlib
    import re as _re

    async with SessionLocal() as db:
        doc = KbDocSource(
            space_id=space_id,
            title=f"[调研] {outline['title']}",
            content=report_md,
            content_hash=hashlib.blake2b(report_md.encode(), digest_size=24).hexdigest(),
            source_type="research",
            char_count=len(report_md),
        )
        db.add(doc)
        await db.flush()

        slug = _re.sub(r"[^a-z0-9-]", "-", topic.lower()).strip("-")[:100] or "research"
        page = KbWikiPage(
            space_id=space_id,
            slug=f"{slug}-research",
            title=outline["title"],
            body_md=report_md,
            source_refs=json.dumps(
                [{"doc_id": doc.id, "title": s.get("title", ""), "url": s.get("url", "")} for s in sources[:10]],
                ensure_ascii=False,
            ),
            status="draft",  # 草稿态——人工确认后才 live（危险操作确认原则）
            confidence=0.7,
            source_count=len(sources),
            last_edit_source="agent",
        )
        db.add(page)
        await db.flush()
        db.add(KbWikiRevision(
            page_id=page.id, version=1, body_md=report_md,
            edit_source="agent", is_machine_snapshot=True, note="research 生成",
        ))
        await db.commit()
        page_id = page.id
        doc_id = doc.id

    if progress_cb:
        await progress_cb("done", 100)
    return {
        "ok": True, "page_id": page_id, "doc_id": doc_id,
        "outline": outline, "llm_calls": llm_calls,
        "token_usage": token_usage,
        "status": "draft（待人工确认发布）",
    }
