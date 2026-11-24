"""MCP 工具注册——kb_save / kb_query。

.. important::
   - 工具签名中的 ``user`` 参数为**运行时注入**（宿主 execute_tool 传入），
     不会出现在暴露给 LLM 的 JSON Schema 中（宿主 _infer_schema 跳过保留参数）。
   - ``user`` 注入保证了行级隔离：AI 助手无法指定/伪造其他用户身份
     （调研结论：mem0 "身份键服务端注入+强制 filter"）。
   - 必须传 ``plugin_name``，插件禁用时自动注销。
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
    """检索知识库中当前用户的记忆与画像，用于在回复前回忆用户的背景信息。

    Args:
        query: 检索关键词（为空则返回最高置信度的记忆摘要）
        limit: 返回条数上限（默认5，最大20）
        user: 运行时注入的当前用户（勿在调用时传入）
    """
    if user is None:
        return json.dumps({"ok": False, "msg": "缺少用户上下文，无法查询"}, ensure_ascii=False)
    hits, profile_excerpt = await memory_pipeline.search_memories(user.id, query, limit)
    return json.dumps(
        {"ok": True, "memories": hits, "profile_excerpt": profile_excerpt},
        ensure_ascii=False,
    )


def register_knowledge_base_mcp_tools() -> None:
    """注册 MCP 工具。在 ``plugin.py`` 的 ``register_mcp_tools()`` 中调用。

    required_permissions 留空 = 所有登录用户可用（聊天 AI 助手需要无门槛调用；
    行级隔离由 user 注入保证，非管理员无法越权读取他人记忆）。
    """
    from src.mcp import mcp_manager

    mcp_manager.register_tool(
        name="kb_save",
        description="保存一条用户知识到知识库（自动去重；重复保存会强化该知识的置信度）",
        handler=_kb_save,
        plugin_name="knowledge_base",
    )
    mcp_manager.register_tool(
        name="kb_query",
        description="检索当前用户的记忆与画像（背景信息），回复前先调用它了解用户",
        handler=_kb_query,
        plugin_name="knowledge_base",
    )
    logger.info("Registered 2 knowledge_base MCP tools (kb_save / kb_query)")
