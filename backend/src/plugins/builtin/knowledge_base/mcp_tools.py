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
    """检索当前用户的记忆、私有知识库词条与公开知识库词条（三类背景信息一次拿全）。

    回复用户问题前先调用本工具，可获得：用户个人记忆/画像、
    用户私有 Wiki 词条、全员共享的公开 Wiki 词条。

    Args:
        query: 检索关键词（为空则返回高置信度摘要）
        limit: 每通道返回条数上限（默认5，最大10）
        user: 运行时注入的当前用户（勿在调用时传入）
    """
    if user is None:
        return json.dumps({"ok": False, "msg": "缺少用户上下文，无法查询"}, ensure_ascii=False)
    from src.plugins.builtin.knowledge_base.pipeline.retrieval import search_all

    result = await search_all(user.id, query, limit)
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

    job_id = await enqueue_job(user.id, "wiki_ingest", {"space_id": space_id}, space_id=space_id)
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
    logger.info("Registered 4 knowledge_base MCP tools (kb_save/kb_query/kb_ingest/kb_compile)")
