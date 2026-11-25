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
    display_name = "ApeCortex"
    description = (
        "ApeCortex——给 ApeAdmin 装上知识大脑皮层：聊天记忆沉淀与画像、LLM Wiki 编译、"
        "私有/共享/公开空间与 PR 式晋升、三通道召回、联网采集与学术深度调研、"
        "公开库只读出口与 MCP 知识接口。"
    )
    version = "0.4.0"
    author = "ApeAdmin"

    # ── 生命周期：安装（建表 + seed）──────────────────
    async def install(self) -> None:
        from src.db import engine

        # create_all 幂等：P0 已安装的库升级 P1 时，新表自动补建（SQLite 不做列级 ALTER，
        # 模型演进遵循"新功能新表"纪律；既有表不删列）
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

    def on_load(self) -> None:
        """启动时恢复孤儿任务（P1：kb_job 表）。"""
        import asyncio

        async def _recover() -> None:
            try:
                from src.plugins.builtin.knowledge_base.pipeline.jobs import recover_orphan_jobs
                await recover_orphan_jobs()
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[KnowledgeBase] 孤儿任务恢复跳过（表未建/首装）: {exc}")

        try:
            loop = asyncio.get_running_loop()
            loop.create_task(_recover())
        except RuntimeError:
            pass

    # ── 生命周期：注册路由 + 事件 ──────────────────────
    def register(self, app: FastAPI) -> None:
        from src.plugins.builtin.knowledge_base.api import router
        from src.plugins.builtin.knowledge_base.api_public import router as public_router
        from src.plugins.builtin.knowledge_base.api_wiki import router as wiki_router

        app.include_router(router, prefix=settings.API_PREFIX)
        app.include_router(wiki_router, prefix=settings.API_PREFIX)
        app.include_router(public_router, prefix=settings.API_PREFIX)

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
