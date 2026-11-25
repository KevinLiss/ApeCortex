"""学术三源采集器（P2）：arXiv / OpenAlex / Semantic Scholar。

.. design:: 调研结论的坑规避
   - arxiv.py：实例级 3s 限速 → 全局单例 Client；PDF 下载无内置 → 只取元数据+摘要
   - pyalex：礼貌池必须设 email（config.email）→ 默认 max_retries=0 会炸 → 显式 Retry(3)
     基于 requests（同步）→ asyncio.to_thread 包装
   - semanticscholar：无限速器 → 本层实现进程级 1s 共享节流 + 短重试 ≤3 + 连接复用
   三个采集器均为"搜索元数据"用途（供 AI 调研引用），不下载全文。
"""
import asyncio
import time

import httpx

# ---------------------------------------------------------------- 共享限速

_last_call: dict[str, float] = {}
_rate_lock = asyncio.Lock()


async def _throttle(source: str, min_interval: float) -> None:
    """进程级共享节流（semanticscholar 无内置限速的补偿）。"""
    async with _rate_lock:
        now = time.monotonic()
        last = _last_call.get(source, 0.0)
        wait = min_interval - (now - last)
        if wait > 0:
            await asyncio.sleep(wait)
        _last_call[source] = time.monotonic()


# ---------------------------------------------------------------- arXiv

_ARXIV_CLIENT = None  # 全局单例（实例级限速计时共享）


async def search_arxiv(query: str, max_results: int = 5) -> list[dict]:
    """arXiv 检索：元数据+摘要（不下载 PDF；PDF 链接返回给上层决定）。"""
    global _ARXIV_CLIENT

    def _sync() -> list[dict]:
        import arxiv

        global _ARXIV_CLIENT
        if _ARXIV_CLIENT is None:
            _ARXIV_CLIENT = arxiv.Client(delay_seconds=3.0, num_retries=2)  # 官方速率
        search = arxiv.Search(query=query, max_results=max(1, min(max_results, 10)))
        out = []
        for r in _ARXIV_CLIENT.results(search):
            out.append({
                "source": "arxiv",
                "id": r.get_short_id(),
                "title": r.title,
                "authors": [a.name for a in r.authors][:5],
                "abstract": (r.summary or "")[:600],
                "url": r.entry_id,
                "pdf_url": r.pdf_url,
                "published": r.published.isoformat() if r.published else "",
                "categories": r.categories[:3] if r.categories else [],
            })
        return out

    try:
        return await asyncio.to_thread(_sync)
    except Exception as exc:  # noqa: BLE001
        return [{"source": "arxiv", "error": f"arXiv 检索失败: {exc}"}]


# ---------------------------------------------------------------- OpenAlex

def _openalex_setup() -> None:
    import pyalex  # noqa: F401


async def search_openalex(query: str, max_results: int = 5, email: str = "") -> list[dict]:
    """OpenAlex 检索（礼貌池 email 可选）。元数据：DOI/引用数/OA 链接。"""

    def _sync() -> list[dict]:
        import pyalex

        if email:
            pyalex.config.email = email  # 礼貌池（~10万条/日档）
        try:
            pyalex.config.max_retries = 3
        except Exception:  # noqa: BLE001
            pass
        works = (
            pyalex.Works()
            .search(query)
            .select("id,doi,title,publication_year,cited_by_count,abstract_inverted_index,open_access")
            .get(per_page=max(1, min(max_results, 10)))
        )
        out = []
        for w in works:
            try:
                abstract = pyalex.Works().invert_abstract(dict(w.get("abstract_inverted_index") or {})) or ""
            except Exception:  # noqa: BLE001
                abstract = ""
            out.append({
                "source": "openalex",
                "id": w.get("id", ""),
                "title": w.get("title") or "",
                "abstract": abstract[:600],
                "doi": w.get("doi") or "",
                "year": w.get("publication_year"),
                "cited_by": w.get("cited_by_count", 0),
                "url": w.get("doi") or w.get("id", ""),
            })
        return out

    try:
        return await asyncio.to_thread(_sync)
    except Exception as exc:  # noqa: BLE001
        return [{"source": "openalex", "error": f"OpenAlex 检索失败: {exc}"}]


# ---------------------------------------------------------------- Semantic Scholar

_S2_HEADERS = {"User-Agent": "ApeAdmin-KnowledgeBase/1.0"}


async def search_semanticscholar(query: str, max_results: int = 5) -> list[dict]:
    """S2 检索：自建限速（≥1s/请求）+ 重试 ≤3 + 连接复用（调研结论三家最弱，全部补偿）。"""
    fields = "title,abstract,year,authors,url,citationCount,externalIds"
    url = "https://api.semanticscholar.org/graph/v1/paper/search"
    params = {"query": query, "limit": max(1, min(max_results, 10)), "fields": fields}

    for attempt in range(3):
        await _throttle("s2", 1.0)  # 共享节流
        try:
            async with httpx.AsyncClient(timeout=15.0, trust_env=False) as client:
                resp = await client.get(url, params=params, headers=_S2_HEADERS)
            if resp.status_code == 429:
                await asyncio.sleep(2 * (attempt + 1))
                continue
            resp.raise_for_status()
            data = resp.json()
            return [
                {
                    "source": "semanticscholar",
                    "id": p.get("paperId", ""),
                    "title": p.get("title") or "",
                    "abstract": (p.get("abstract") or "")[:600],
                    "authors": [a.get("name", "") for a in (p.get("authors") or [])[:5]],
                    "year": p.get("year"),
                    "cited_by": p.get("citationCount", 0),
                    "url": p.get("url", ""),
                    "doi": (p.get("externalIds") or {}).get("DOI", ""),
                }
                for p in (data.get("data") or [])
            ]
        except httpx.HTTPError:
            if attempt == 2:
                return [{"source": "semanticscholar", "error": "Semantic Scholar 检索失败（网络/限流）"}]
            await asyncio.sleep(1)
    return [{"source": "semanticscholar", "error": "Semantic Scholar 限流，请稍后重试"}]
