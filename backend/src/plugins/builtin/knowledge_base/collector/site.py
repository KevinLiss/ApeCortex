"""站点爬虫（N3）——同域 BFS 多页采集 → 聚合知识文档入库。

引擎策略（用户决策 D3：直接上 crawl4ai）：
- 每页先轻量拉 HTML（httpx）+ trafilatura 抽正文；
- 抽取为空（JS 渲染站点）且 crawl4ai 可用（已装 playwright 浏览器）→ 渲染重抓；
- crawl4ai 不可用时明确降级提示（不静默失败）。

护栏（全部硬约束）：
- 联网开关 + 域名白名单 + SSRF + robots.txt（逐页）
- 同域限制（host 严格一致）、深度上限（默认 2）、总页数上限（默认 20，最大 50）
- 单页超时 / 响应大小限制（复用 web.py 常量）
"""
import asyncio
import re
from collections import deque
from urllib.parse import urljoin, urlsplit

import httpx
from loguru import logger

from src.plugins.builtin.knowledge_base.collector.web import (
    _FETCH_TIMEOUT,
    _MAX_RESPONSE_BYTES,
    _USER_AGENT,
    _extract_markdown,
    _render_fetch,
    _robots_allows,
    check_domain,
    check_ssrf,
)

_SKIP_EXT = (
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico", ".css", ".js",
    ".zip", ".tar", ".gz", ".mp4", ".mp3", ".woff", ".woff2", ".ttf", ".eot",
)
_HREF_RE = re.compile(r'href=["\']([^"\']+)["\']', re.IGNORECASE)
_PER_PAGE_MD_CAP = 30_000  # 单页正文进聚合文档的上限（字符）


def _same_site(a: str, b: str) -> bool:
    return urlsplit(a).netloc.lower() == urlsplit(b).netloc.lower()


def _normalize(url: str, base: str) -> str | None:
    """相对链接解析 + 归一化（去 fragment/query 保守保留）。"""
    try:
        joined = urljoin(base, url.strip())
    except ValueError:
        return None
    if not joined.startswith(("http://", "https://")):
        return None
    return joined.split("#", 1)[0]


def _discover_links(html: str, base: str) -> list[str]:
    """从原始 HTML 提取同域候选链接（去重、跳过静态资源）。"""
    out: list[str] = []
    seen: set[str] = set()
    for raw in _HREF_RE.findall(html or ""):
        url = _normalize(raw, base)
        if url is None or url in seen:
            continue
        if any(url.lower().endswith(ext) for ext in _SKIP_EXT):
            continue
        if not _same_site(url, base):
            continue
        seen.add(url)
        out.append(url)
    return out


async def _fetch_html(url: str, extra_allow: list[str]) -> tuple[str | None, str]:
    """轻量拉原始 HTML（含 SSRF/域名/robots 护栏）。返回 (html, msg)。"""
    ok, msg = check_ssrf(url)
    if not ok:
        return None, msg
    ok, msg = check_domain(url, extra_allow)
    if not ok:
        return None, msg
    if not await _robots_allows(url):
        return None, "robots.txt 禁止抓取该路径"
    try:
        async with httpx.AsyncClient(
            timeout=_FETCH_TIMEOUT, trust_env=False, follow_redirects=True,
            headers={"User-Agent": _USER_AGENT},
        ) as client:
            resp = await client.get(url)
            if resp.status_code >= 400:
                return None, f"HTTP {resp.status_code}"
            ctype = resp.headers.get("content-type", "")
            if "html" not in ctype:
                return None, f"非 HTML 页面: {ctype[:40]}"
            if len(resp.content) > _MAX_RESPONSE_BYTES:
                return None, "响应过大"
            return resp.text, ""
    except httpx.HTTPError as exc:
        detail = str(exc).strip() or type(exc).__name__
        return None, f"抓取失败: {type(exc).__name__}: {detail}"


async def _crawl_one(url: str, extra_allow: list[str]) -> dict:
    """抓一页 → (content_md, title, links, msg)。JS 站点渲染降级。"""
    html, msg = await _fetch_html(url, extra_allow)
    if html is None:
        return {"ok": False, "msg": msg, "links": []}

    links = _discover_links(html, url)
    result = _extract_markdown(url, html)
    if result.get("ok"):
        return {"ok": True, "url": url, "title": result["title"], "content_md": result["content_md"], "links": links}

    # JS 渲染站：crawl4ai 重抓（装了才可用，否则明确降级）
    rendered = await _render_fetch(url)
    if rendered.get("ok"):
        return {
            "ok": True, "url": url, "title": rendered["title"],
            "content_md": rendered["content_md"], "links": links,
        }
    return {"ok": False, "msg": rendered.get("msg") or result.get("msg") or "正文抽取为空", "links": links}


async def crawl_site(
    user_id: int,
    start_url: str,
    max_pages: int = 20,
    max_depth: int = 2,
    space_id: int = 0,
    title: str = "",
    extra_allow: list[str] | None = None,
    compile_now: bool = True,
) -> dict:
    """同域 BFS 爬取 → 聚合一份 Markdown 知识文档入库（source_type=crawl）。"""
    from src.plugins.builtin.knowledge_base.collector.web import get_web_enabled
    from src.plugins.builtin.knowledge_base.pipeline.ingest_api import ingest_doc

    if not await get_web_enabled(user_id):
        return {"ok": False, "msg": "联网开关未开启（知识库设置页确认开启后重试）"}

    start_url = (start_url or "").strip()
    if not start_url.startswith(("http://", "https://")):
        return {"ok": False, "msg": "仅支持 http/https 起始地址"}
    ok, msg = check_ssrf(start_url)
    if not ok:
        return {"ok": False, "msg": msg}
    allow = extra_allow or []
    ok, msg = check_domain(start_url, allow)
    if not ok:
        return {"ok": False, "msg": msg}

    max_pages = max(1, min(int(max_pages), 50))
    max_depth = max(0, min(int(max_depth), 3))

    visited: set[str] = set()
    frontier: deque[tuple[str, int]] = deque([(start_url.split("#")[0], 0)])
    pages: list[dict] = []
    failures: list[str] = []

    while frontier and len(visited) < max_pages:
        url, depth = frontier.popleft()
        if url in visited:
            continue
        visited.add(url)
        page = await _crawl_one(url, allow)
        if page.get("ok"):
            pages.append(page)
            if depth < max_depth:
                for link in page.get("links", []):
                    if link not in visited and len(visited) + len(frontier) < max_pages + 20:
                        frontier.append((link, depth + 1))
        else:
            failures.append(f"{url}: {page.get('msg', '')}")
        await asyncio.sleep(0.5)  # 礼貌间隔（爬速护栏）

    if not pages:
        return {
            "ok": False,
            "msg": "未抓到任何正文：" + "；".join(failures[:3]),
            "pages": 0,
        }

    # 聚合为一份知识文档
    site = urlsplit(pages[0]["url"])
    doc_title = (title.strip() if title.strip() else f"{site.netloc} 站点采集")
    parts = [f"> 来源站点：{site.scheme}://{site.netloc}｜起始页：{start_url}｜共 {len(pages)} 页\n"]
    for i, p in enumerate(pages, 1):
        md = p["content_md"][:_PER_PAGE_MD_CAP]
        parts.append(f"\n## [{i}] {p['title'] or p['url']}\n\n来源：{p['url']}\n\n{md}\n")
    content = "\n".join(parts)

    ingest = await ingest_doc(
        user_id, space_id or 0, doc_title[:200], content,
        source_type="crawl", url=start_url, compile_now=compile_now,
    )
    if not ingest.get("ok"):
        return {"ok": False, "msg": ingest.get("msg", "入库失败")}

    logger.info(f"[KnowledgeBase] 站点爬取入库 user={user_id} url={start_url} pages={len(pages)}")
    return {
        "ok": True,
        "doc_id": ingest.get("doc_id"),
        "space_id": ingest.get("space_id"),
        "job_id": ingest.get("job_id"),
        "pages": len(pages),
        "skipped": len(failures),
        "msg": f"已采集 {len(pages)} 页入库「{doc_title}」，跳过 {len(failures)} 页",
    }
