"""AI 知识库插件入口（P0：记忆层）。

.. sidebar:: 插件功能
   :title: 你需要做什么

   1. 启用插件（后台 → 插件管理）→ 自动建表 + seed 菜单
   2. 与 AI 助手正常聊天（宿主 runner 在每轮对话完成后发射
      ``chat_message_complete`` 事件）
   3. 插件后台提取值得记忆的知识（单次 LLM ADD-only + MD5 去重）
   4. 在「AI 知识库 → 记忆画像」页查看/删除记忆、重写画像
   5. 聊天中 AI 可调用 kb_save / kb_query MCP 工具主动存取知识
"""
from fastapi import FastAPI
from loguru import logger

from src.core.config import settings
from src.db import SessionLocal
from src.plugins import PluginInterface, event_bus
from src.plugins.builtin.knowledge_base.models import (  # noqa: F401 — 注册 ORM 元数据
    KbMemoryItem,
    KbProfile,
    PluginBase,
)
from src.plugins.builtin.knowledge_base.seed import seed_knowledge_base_data

# 宿主聊天完成事件（见 src/ai/runner.py _finish_run 中的发射点）
_EVENT_CHAT_COMPLETE = "chat_message_complete"


class KnowledgeBasePlugin(PluginInterface):
    """AI 知识库插件——聊天知识自动沉淀与用户画像。"""

    name = "knowledge_base"
    display_name = "AI 知识库"
    description = (
        "聊天知识自动沉淀与用户画像：对话完成后自动提取值得记忆的事实与偏好"
        "（单次 LLM 提取 + MD5 去重强化），形成个人画像；AI 助手可经 MCP 工具 "
        "kb_save / kb_query 主动存取。"
    )
    version = "0.1.0"
    author = "ApeAdmin"

    # ── 生命周期：安装（建表 + seed）──────────────────
    async def install(self) -> None:
        from src.db import engine

        async with engine.begin() as conn:
            await conn.run_sync(
                lambda sync_conn: PluginBase.metadata.create_all(sync_conn)
            )

        async with SessionLocal() as db:
            await seed_knowledge_base_data(db)
            await db.commit()
        logger.info("[KnowledgeBase] installed — tables ready, menus seeded")

    # ── 生命周期：卸载 ─────────────────────────────────
    async def uninstall(self) -> None:
        from sqlalchemy import delete

        from src.db import engine
        from src.models import Menu

        async with engine.begin() as conn:
            await conn.run_sync(
                lambda sync_conn: PluginBase.metadata.drop_all(sync_conn)
            )
        async with SessionLocal() as db:
            await db.execute(
                delete(Menu).where(Menu.permission.like("knowledge_base:%"))
            )
            await db.execute(delete(Menu).where(Menu.path == "/knowledge-base"))
            await db.commit()
        logger.info("[KnowledgeBase] uninstalled — tables dropped, menus removed")

    # ── 生命周期：注册路由 + 事件 ──────────────────────
    def register(self, app: FastAPI) -> None:
        from src.plugins.builtin.knowledge_base.api import router

        app.include_router(router, prefix=settings.API_PREFIX)

        # 订阅聊天完成事件 → 后台记忆提取（handler 内部 spawn 任务，快速返回）
        event_bus.on(
            _EVENT_CHAT_COMPLETE,
            self._on_chat_message_complete,
            plugin_name=self.name,
        )
        logger.info("[KnowledgeBase] registered — routes + chat-complete hook")

    # ── 生命周期：注册 MCP 工具 ────────────────────────
    def register_mcp_tools(self) -> None:
        from src.plugins.builtin.knowledge_base.mcp_tools import (
            register_knowledge_base_mcp_tools,
        )

        register_knowledge_base_mcp_tools()

    # ── 事件处理 ──────────────────────────────────────
    async def _on_chat_message_complete(self, **kwargs) -> None:
        from src.plugins.builtin.knowledge_base.pipeline.memory import (
            on_chat_message_complete,
        )

        await on_chat_message_complete(**kwargs)
