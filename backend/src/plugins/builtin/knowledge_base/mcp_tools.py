"""MCP 工具注册（P0+P1 全集：kb_save / kb_query / kb_ingest / kb_compile）。

.. important::
   - ``user`` 参数为**运行时注入**（宿主 execute_tool 传入），不出现在 JSON Schema，
     LLM 不可见不可伪造——行级隔离的根基（P0 已验证机制）。
   - 公开库写入仅经晋升流或维护者 API；MCP 层无公开库直写工具。
"""
import json

from loguru import logger

from src.models import User
from src.plugins.builtin.knowledge_base.pipeline import memory as memory_pipeline


async def _kb_save(content: str, mem_type: str = "fact", user: User | None = None) -> str:
    """保存一条用户知识到知识库。

    Args:
        content: 自包含的知识陈述（4~200字），例如"用户偏好用中文回复"或"用户的项目技术栈是 Vue3+FastAPI"
        mem_type: 记忆类型：fact=事实(默认) / preference=偏好 / habit=习惯 / event=事件
        user: 运行时注入的当前用户（勿在调用时传入）
    """
    if user is None:
        return json.dumps({"ok": False, "msg": "缺少用户上下文，无法保存"}, ensure_ascii=False)
    result = await memory_pipeline.save_explicit(user.id, content, mem_type)
    return json.dumps(result, ensure_ascii=False)


async def _kb_query(query: str = "", limit: int = 5, user: User | None = None) -> str:
    """检索用户记忆与知识库词条（背景信息一次拿全）。

    检索范围遵循用户在聊天页「知识库」弹窗里的启用配置：
    知识中心公共库 + 用户启用的个人空间词条（未显式选空间=全部个人空间）。

    Args:
        query: 检索关键词（为空则返回高置信度摘要）
        limit: 每通道返回条数上限（默认5，最大10）
        user: 运行时注入的当前用户（勿在调用时传入）
    """
    if user is None:
        return json.dumps({"ok": False, "msg": "缺少用户上下文，无法查询"}, ensure_ascii=False)
    from src.plugins.builtin.knowledge_base.pipeline.chat_prefs import (
        _is_enabled,
        _space_filter,
        get_chat_prefs,
    )
    from src.plugins.builtin.knowledge_base.pipeline.retrieval import search_all

    prefs = await get_chat_prefs(user.id)
    if not _is_enabled(prefs):
        return json.dumps({
            "ok": True, "memories": [], "private_pages": [], "hub_pages": [],
            "profile_excerpt": "", "msg": "用户未启用知识库（聊天页知识库配置可开启）",
        }, ensure_ascii=False)

    result = await search_all(
        user.id, query, limit,
        space_ids=_space_filter(prefs),
        include_hub=prefs["include_hub"],
    )
    result["profile_excerpt"] = (result.get("profile_excerpt") or "")[:400]
    return json.dumps({"ok": True, **result}, ensure_ascii=False)


async def _kb_ingest(
    title: str,
    content: str,
    space_id: int = 0,
    url: str = "",
    user: User | None = None,
) -> str:
    """把一份资料（聊天结论/网页内容/文档）提交进知识库空间，后台自动编译成 Wiki 词条。

    Args:
        title: 资料标题（2~100字）
        content: 资料正文（Markdown 或纯文本）
        space_id: 目标空间ID（0=自动选用户第一个私有空间；无空间时自动创建默认空间）
        url: 来源链接（可空）
        user: 运行时注入的当前用户（勿在调用时传入）
    """
    if user is None:
        return json.dumps({"ok": False, "msg": "缺少用户上下文"}, ensure_ascii=False)
    from src.plugins.builtin.knowledge_base.pipeline.ingest_api import ingest_doc

    result = await ingest_doc(user.id, space_id, title, content, "chat" if not url else "web", url)
    return json.dumps(result, ensure_ascii=False)


async def _kb_compile(space_id: int, user: User | None = None) -> str:
    """触发一次知识库空间的后台编译任务（把未编译资料编译成词条）。返回任务ID供轮询。

    Args:
        space_id: 空间ID
        user: 运行时注入的当前用户（勿在调用时传入）
    """
    if user is None:
        return json.dumps({"ok": False, "msg": "缺少用户上下文"}, ensure_ascii=False)
    from src.plugins.builtin.knowledge_base.pipeline.ingest_api import ensure_owned_space

    ok, msg = await ensure_owned_space(user.id, space_id, need_owner=True)
    if not ok:
        return json.dumps({"ok": False, "msg": msg}, ensure_ascii=False)
    from src.plugins.builtin.knowledge_base.pipeline.jobs import enqueue_job

    try:
        job_id = await enqueue_job(user.id, "wiki_ingest", {"space_id": space_id}, space_id=space_id)
    except RuntimeError as exc:  # 成本护栏：当日任务超限
        return json.dumps({"ok": False, "msg": str(exc)}, ensure_ascii=False)
    return json.dumps({"ok": True, "job_id": job_id, "msg": "编译任务已入队"}, ensure_ascii=False)


def register_knowledge_base_mcp_tools() -> None:
    """注册 MCP 工具（P0+P1）。"""
    from src.mcp import mcp_manager

    mcp_manager.register_tool(
        name="kb_save",
        description="保存一条用户知识到知识库（自动去重；重复保存会强化该知识的置信度）",
        handler=_kb_save,
        plugin_name="knowledge_base",
    )
    mcp_manager.register_tool(
        name="kb_query",
        description="检索当前用户的记忆/画像、私有知识库词条与公开知识库词条（三类背景信息一次拿全，回复前先调用）",
        handler=_kb_query,
        plugin_name="knowledge_base",
    )
    mcp_manager.register_tool(
        name="kb_ingest",
        description="把资料（聊天结论/网页内容/文档）提交进知识库空间，后台自动编译成 Wiki 词条",
        handler=_kb_ingest,
        plugin_name="knowledge_base",
    )
    mcp_manager.register_tool(
        name="kb_compile",
        description="触发知识库空间的后台编译任务（未编译资料→词条），返回任务ID",
        handler=_kb_compile,
        plugin_name="knowledge_base",
    )
    # ---- P2：联网采集（网页抓取工具默认注册，运行时校验用户开关；学术源公开元数据直接注册）----
    mcp_manager.register_tool(
        name="kb_collect_web",
        description="抓取一个网页抽取正文为 Markdown（需要用户在知识库设置中开启联网开关）",
        handler=_kb_collect_web,
        plugin_name="knowledge_base",
    )
    mcp_manager.register_tool(
        name="kb_collect_arxiv",
        description="检索 arXiv 论文元数据（标题/作者/摘要/PDF链接）",
        handler=_kb_collect_arxiv,
        plugin_name="knowledge_base",
    )
    mcp_manager.register_tool(
        name="kb_collect_openalex",
        description="检索 OpenAlex 学术文献（跨库元数据：DOI/引用数/摘要）",
        handler=_kb_collect_openalex,
        plugin_name="knowledge_base",
    )
    mcp_manager.register_tool(
        name="kb_research",
        description="发起深度调研任务：规划大纲→检索学术文献→撰写带引用报告→草稿词条（人工确认后发布）",
        handler=_kb_research,
        plugin_name="knowledge_base",
    )
    mcp_manager.register_tool(
        name="kb_crawl_site",
        description="爬取一个网站（同域多页）聚合为知识文档存入个人知识库，后台自动拆解词条（需联网开关）",
        handler=_kb_crawl_site,
        plugin_name="knowledge_base",
    )
    logger.info("Registered 9 knowledge_base MCP tools (P0×2 + P1×2 + P2×4 + N3×1)")


async def _kb_collect_web(url: str, user: User | None = None) -> str:
    """抓取一个网页，抽取正文为 Markdown（联网功能，需在知识库设置中开启联网开关）。

    Args:
        url: 目标网页地址（http/https）
        user: 运行时注入的当前用户（勿在调用时传入）
    """
    if user is None:
        return json.dumps({"ok": False, "msg": "缺少用户上下文"}, ensure_ascii=False)
    from src.plugins.builtin.knowledge_base.collector.web import (
        fetch_page,
        get_domain_allowlist,
        get_web_enabled,
    )
    if not await get_web_enabled(user.id):
        return json.dumps({"ok": False, "msg": "联网开关未开启（知识库设置）"}, ensure_ascii=False)
    allow = await get_domain_allowlist(user.id)
    result = await fetch_page(url, extra_allow=allow)
    return json.dumps(result, ensure_ascii=False)


async def _kb_crawl_site(
    url: str,
    max_pages: int = 20,
    max_depth: int = 2,
    space_id: int = 0,
    title: str = "",
    user: User | None = None,
) -> str:
    """爬取一个网站（同域多页 BFS），聚合为知识文档存入个人知识库并自动拆解词条。

    适合文档站/博客站批量采集：给起始页地址即可，同域链接自动跟进。
    JS 渲染站点在安装渲染引擎（crawl4ai）后可采；未装时静态页仍可采。

    Args:
        url: 起始页地址（http/https，爬取范围限同域名）
        max_pages: 总页数上限（默认 20，最大 50）
        max_depth: 链接跟进深度（默认 2，最大 3）
        space_id: 存入哪个知识空间（0=默认空间）
        title: 文档标题（留空自动用站点名）
        user: 运行时注入的当前用户（勿在调用时传入）
    """
    if user is None:
        return json.dumps({"ok": False, "msg": "缺少用户上下文"}, ensure_ascii=False)
    from src.plugins.builtin.knowledge_base.collector.site import crawl_site
    from src.plugins.builtin.knowledge_base.collector.web import get_domain_allowlist

    allow = await get_domain_allowlist(user.id)
    result = await crawl_site(
        user.id, url, max_pages=max_pages, max_depth=max_depth,
        space_id=space_id, title=title, extra_allow=allow,
    )
    return json.dumps(result, ensure_ascii=False)


async def _kb_collect_arxiv(query: str, max_results: int = 5, user: User | None = None) -> str:
    """检索 arXiv 论文（标题/作者/摘要/PDF链接，无需联网开关——学术 API 公开元数据）。

    Args:
        query: 检索词（英文效果最佳）
        max_results: 返回条数（默认5，最大10）
        user: 运行时注入的当前用户（勿在调用时传入）
    """
    from src.plugins.builtin.knowledge_base.collector.academic import search_arxiv
    results = await search_arxiv(query, max_results)
    return json.dumps(
        {"ok": not results or "error" not in results[0], "results": results[:max_results]},
        ensure_ascii=False,
    )


async def _kb_collect_openalex(query: str, max_results: int = 5, user: User | None = None) -> str:
    """检索 OpenAlex 学术文献（跨库元数据：DOI/引用数/年份/摘要）。

    Args:
        query: 检索词
        max_results: 返回条数（默认5，最大10）
        user: 运行时注入的当前用户（勿在调用时传入）
    """
    from src.plugins.builtin.knowledge_base.collector.academic import search_openalex
    results = await search_openalex(query, max_results)
    return json.dumps(
        {"ok": not results or "error" not in results[0], "results": results[:max_results]},
        ensure_ascii=False,
    )


async def _kb_research(topic: str, space_id: int = 0, user: User | None = None) -> str:
    """发起一次深度调研任务：AI 规划大纲→检索学术文献→撰写带引用的报告→落为草稿词条（需人工确认发布）。

    Args:
        topic: 调研主题（如"RAG 与长上下文的取舍"）
        space_id: 目标空间ID（0=默认空间）
        user: 运行时注入的当前用户（勿在调用时传入）
    """
    if user is None:
        return json.dumps({"ok": False, "msg": "缺少用户上下文"}, ensure_ascii=False)
    from src.plugins.builtin.knowledge_base.pipeline.ingest_api import get_or_create_default_space
    from src.plugins.builtin.knowledge_base.pipeline.jobs import enqueue_job
    if not space_id:
        space_id = await get_or_create_default_space(user.id)
    try:
        job_id = await enqueue_job(
            user.id, "research", {"topic": topic, "space_id": space_id}, space_id=space_id,
        )
    except RuntimeError as exc:  # 成本护栏：当日任务超限
        return json.dumps({"ok": False, "msg": str(exc)}, ensure_ascii=False)
    return json.dumps(
        {"ok": True, "job_id": job_id, "msg": "调研任务已入队，完成后在任务中心/词条页查看草稿"},
        ensure_ascii=False,
    )
