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

# 存量表新增列迁移清单：表 → (列名, DDL 类型)。启动时 PRAGMA 自查，缺则 ALTER TABLE 补列。
_COLUMN_MIGRATIONS: dict[str, list[tuple[str, str]]] = {
    "kb_space": [
        ("category_id", "INTEGER NULL"),
    ],
    "kb_doc_source": [
        ("file_ext", "VARCHAR(10) NULL"),
    ],
    "kb_wiki_page": [
        ("category_id", "INTEGER NULL"),
    ],
    "kb_public_submission": [
        ("source_page_id", "INTEGER NOT NULL DEFAULT 0"),
        ("check_comment", "VARCHAR(500) NOT NULL DEFAULT ''"),
        ("checked_by", "INTEGER NULL"),
        ("checked_at", "TIMESTAMP NULL"),
    ],
}


def _ensure_columns(sync_conn) -> None:
    """SQLite 友好的列级迁移：缺列补列，幂等（在 create_all 之后跑）。"""
    from sqlalchemy import text

    for table, columns in _COLUMN_MIGRATIONS.items():
        try:
            existing = {
                row[1] for row in sync_conn.execute(text(f"PRAGMA table_info({table})"))
            }
        except Exception:  # noqa: BLE001 — 表不存在（首装时 create_all 已建最新结构）
            continue
        for col, ddl in columns:
            if col in existing:
                continue
            sync_conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}"))
            logger.info(f"[KnowledgeBase] 迁移：{table} 补列 {col}")


class KnowledgeBasePlugin(PluginInterface):
    """AI 知识库插件——聊天知识自动沉淀与用户画像。"""

    name = "knowledge_base"
    display_name = "ApeCortex"
    description = (
        "ApeCortex——给 ApeAdmin 装上知识大脑皮层：聊天记忆沉淀与画像、LLM Wiki 编译、"
        "私有/共享/公开空间与 PR 式晋升、三通道召回、联网采集与学术深度调研、"
        "公开库只读出口与 MCP 知识接口。"
    )
    version = "0.8.0"
    author = "ApeAdmin"

    # ── 生命周期：安装（建表 + seed）──────────────────
    async def install(self) -> None:
        from src.db import engine

        # create_all 幂等：P0 已安装的库升级时新表自动补建；既有表新增列走
        # _ensure_columns 的 PRAGMA 自查 + ALTER TABLE ADD COLUMN（SQLite 安全路径）
        async with engine.begin() as conn:
            await conn.run_sync(
                lambda sync_conn: PluginBase.metadata.create_all(sync_conn)
            )
            await conn.run_sync(_ensure_columns)

        async with SessionLocal() as db:
            await seed_knowledge_base_data(db)
            await db.commit()
        logger.info("[KnowledgeBase] installed — tables ready, menus seeded")

    # ── 生命周期：卸载 ─────────────────────────────────
    async def uninstall(self) -> None:
        from sqlalchemy import delete

        from src.db import engine
        from src.models import Menu

        # 还原宿主 runner 补丁（N3 自动注入）
        try:
            from src.plugins.builtin.knowledge_base.pipeline.chat_prefs import unpatch_runner_context
            unpatch_runner_context()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[KnowledgeBase] 自动注入补丁还原跳过: {exc}")

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
        """启动时恢复孤儿任务 + 菜单结构自愈迁移（幂等，旧库升级即收敛）。"""
        import asyncio

        async def _recover() -> None:
            try:
                from src.plugins.builtin.knowledge_base.pipeline.jobs import recover_orphan_jobs
                await recover_orphan_jobs()
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[KnowledgeBase] 孤儿任务恢复跳过（表未建/首装）: {exc}")

        async def _reseed_menus() -> None:
            # N1 升级自愈：旧库的菜单结构（顶级目录名/词条管理独立菜单）在重启后自动收敛
            try:
                async with SessionLocal() as db:
                    await seed_knowledge_base_data(db)
                    await db.commit()
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[KnowledgeBase] 菜单自愈迁移跳过: {exc}")

        try:
            loop = asyncio.get_running_loop()
            loop.create_task(_recover())
            loop.create_task(_reseed_menus())
        except RuntimeError:
            pass

        # N3 自动注入：运行时包装宿主 runner._build_context（零宿主源码侵入，禁用时还原）
        try:
            from src.plugins.builtin.knowledge_base.pipeline.chat_prefs import patch_runner_context
            patch_runner_context()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[KnowledgeBase] 自动注入补丁挂接跳过: {exc}")

    # ── 生命周期：注册路由 + 事件 ──────────────────────
    def register(self, app: FastAPI) -> None:
        from src.plugins.builtin.knowledge_base.api import router
        from src.plugins.builtin.knowledge_base.api_docs import router as docs_router
        from src.plugins.builtin.knowledge_base.api_hub import router as hub_router
        from src.plugins.builtin.knowledge_base.api_public import router as public_router
        from src.plugins.builtin.knowledge_base.api_push import router as push_router
        from src.plugins.builtin.knowledge_base.api_wiki import router as wiki_router

        app.include_router(router, prefix=settings.API_PREFIX)
        app.include_router(wiki_router, prefix=settings.API_PREFIX)
        app.include_router(public_router, prefix=settings.API_PREFIX)
        app.include_router(hub_router, prefix=settings.API_PREFIX)
        app.include_router(docs_router, prefix=settings.API_PREFIX)
        app.include_router(push_router, prefix=settings.API_PREFIX)

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
