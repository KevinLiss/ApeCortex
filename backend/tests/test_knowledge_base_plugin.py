"""Knowledge base plugin tests (P0 memory layer).

Covers:
- extraction response parsing: happy path + double-layer JSON fallbacks (mem0
  style: raw → strip code fences → outermost braces) + invalid payloads
- content normalization & MD5 dedup (batch-internal + DB-level reinforcement)
- explicit save path (kb_save) and search_memories scoping
- profile rewrite (version bump, markdown replace)
- MCP manager: `user` reserved param is injected at call time and never
  exposed in the inferred JSON schema (host patch)
- chat-complete event handler spawns a background job and never raises
"""

import asyncio
import json

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.db import Base
from src.plugins.builtin.knowledge_base.models import (
    KbMemoryItem,
    KbProfile,
    PluginBase,
)
from src.plugins.builtin.knowledge_base.pipeline import memory as memory_pipeline


# ---------------------------------------------------------------- fixtures
@pytest.fixture
async def kb_env(monkeypatch, tmp_path):
    """Isolated file SQLite with core + plugin tables, wired into the
    memory pipeline's SessionLocal."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/kb_test.db")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(PluginBase.metadata.create_all)
    monkeypatch.setattr(memory_pipeline, "SessionLocal", factory)
    yield factory
    await engine.dispose()


@pytest.fixture
def mock_llm(monkeypatch):
    """Stub the LLM call; records prompts for assertions."""
    calls: list[str] = []

    async def fake(system: str, user: str, max_tokens: int, temperature: float) -> str:
        calls.append(user)
        return fake.response

    fake.response = ""
    fake.calls = calls
    monkeypatch.setattr(memory_pipeline, "_call_llm_json", fake)
    return fake


# ---------------------------------------------------------------- parsing
def test_parse_plain_json():
    raw = json.dumps({"memories": [{"content": "用户偏好中文回复", "type": "preference"}]})
    items = memory_pipeline.parse_extraction_response(raw)
    assert items == [{"content": "用户偏好中文回复", "mem_type": "preference"}]


def test_parse_code_fenced_json():
    raw = '```json\n{"memories": [{"content": "用户的技术栈是 Vue3+FastAPI", "type": "fact"}]}\n```'
    items = memory_pipeline.parse_extraction_response(raw)
    assert items == [{"content": "用户的技术栈是 Vue3+FastAPI", "mem_type": "fact"}]


def test_parse_json_with_prose_padding():
    raw = '好的，以下是提取结果：\n{"memories": [{"content": "用户在深夜工作", "type": "habit"}]}\n以上。'
    items = memory_pipeline.parse_extraction_response(raw)
    assert items == [{"content": "用户在深夜工作", "mem_type": "habit"}]


def test_parse_invalid_returns_empty():
    for raw in ("", "not json at all", '{"memories": "not-a-list"}', '{"memories": [42]}'):
        assert memory_pipeline.parse_extraction_response(raw) == []


def test_parse_rejects_bad_items():
    raw = json.dumps({"memories": [
        {"content": "ab", "type": "fact"},                 # too short
        {"content": "x" * 300, "type": "fact"},           # too long
        {"content": "正常长度的记忆条目内容", "type": "weird"},  # bad type → falls back to fact
        {"content": "另一条正常长度的记忆", "type": "event"},
    ]})
    items = memory_pipeline.parse_extraction_response(raw)
    assert len(items) == 2
    assert items[0]["mem_type"] == "fact"  # weird normalized to fact
    assert items[1]["mem_type"] == "event"


# ---------------------------------------------------------------- dedup
async def test_save_explicit_creates_then_reinforces(kb_env):
    first = await memory_pipeline.save_explicit(1, "用户偏好用中文回复", "preference")
    assert first["ok"] and first["action"] == "created"

    # same content, different spacing/case → same hash → reinforcement
    second = await memory_pipeline.save_explicit(1, "用户 偏好用中文 回复", "preference")
    assert second["ok"] and second["action"] == "reinforced"

    async with kb_env() as db:
        rows = (await db.execute(select(KbMemoryItem))).scalars().all()
    assert len(rows) == 1
    assert rows[0].source_count == 2
    assert rows[0].confidence > 0.6


async def test_save_explicit_validates_length(kb_env):
    result = await memory_pipeline.save_explicit(1, "ab")
    assert not result["ok"]


async def test_extract_and_store_dedup_batch_and_db(kb_env, mock_llm):
    mock_llm.response = json.dumps({"memories": [
        {"content": "用户的公司是 ApeAdmin", "type": "fact"},
        {"content": "用户的公司是 apeadmin", "type": "fact"},   # batch-internal dup
        {"content": "用户的项目用 SQLite 存储", "type": "fact"},
    ]})

    transcript = "用户: 我们公司是 ApeAdmin，另外我补充一下，用户的项目用 SQLite 存储。\n助手: 已了解。"
    inserted = await memory_pipeline.extract_and_store(
        user_id=1, session_id=10, run_id=99, transcript_override=transcript
    )
    assert inserted == 2

    # re-run identical extraction → all reinforced, zero new rows
    inserted_again = await memory_pipeline.extract_and_store(
        user_id=1, session_id=10, run_id=100, transcript_override=transcript
    )
    assert inserted_again == 0

    async with kb_env() as db:
        rows = (await db.execute(select(KbMemoryItem))).scalars().all()
    assert len(rows) == 2
    for row in rows:
        assert row.source_count == 2
        assert row.session_id == 10
        assert row.source_run_id == 99  # first occurrence wins (no LLM update decision)


async def test_extract_and_store_user_isolation(kb_env, mock_llm):
    mock_llm.response = json.dumps({"memories": [
        {"content": "用户住在上海", "type": "fact"},
    ]})
    await memory_pipeline.extract_and_store(
        user_id=1, session_id=1, transcript_override="用户: 我住在上海，在张江高科附近上班，平时习惯地铁通勤。"
    )
    await memory_pipeline.extract_and_store(
        user_id=2, session_id=2, transcript_override="用户: 我住在上海，在张江高科附近上班，平时习惯地铁通勤。"
    )

    async with kb_env() as db:
        rows = (await db.execute(select(KbMemoryItem))).scalars().all()
    assert len(rows) == 2
    assert {r.user_id for r in rows} == {1, 2}


async def test_extract_and_store_short_transcript_skipped(kb_env, mock_llm):
    # 转写太短 → 不调 LLM，直接返回 0
    inserted = await memory_pipeline.extract_and_store(
        user_id=1, session_id=1, transcript_override="用户: 你好"
    )
    assert inserted == 0
    assert mock_llm.calls == []  # 省了一次调用


# ---------------------------------------------------------------- profile
async def test_profile_rewrite_bumps_version(kb_env, mock_llm):
    mock_llm.response = json.dumps({"memories": [
        {"content": "用户的职业是独立开发者", "type": "fact"},
    ]})
    await memory_pipeline.extract_and_store(
        user_id=1, session_id=1, transcript_override="用户: 我的职业是独立开发者，主要做后台管理框架产品。"
    )

    mock_llm.response = "## 基本信息\n- 独立开发者\n"
    ok1 = await memory_pipeline.rewrite_profile(1)
    assert ok1

    mock_llm.response = "## 基本信息\n- 独立开发者（已升级 v2）\n"
    ok2 = await memory_pipeline.rewrite_profile(1)
    assert ok2

    async with kb_env() as db:
        profile = (await db.execute(select(KbProfile))).scalars().one()
    assert profile.version == 2
    assert "v2" in profile.profile_md


async def test_profile_rewrite_without_memories_fails(kb_env, mock_llm):
    assert await memory_pipeline.rewrite_profile(1) is False
    assert mock_llm.calls == []  # 无记忆时省 LLM 调用


# ---------------------------------------------------------------- search
async def test_search_memories_scoped_to_user(kb_env, mock_llm):
    mock_llm.response = json.dumps({"memories": [
        {"content": "用户的技术栈是 Vue3", "type": "fact"},
    ]})
    await memory_pipeline.extract_and_store(
        user_id=1, session_id=1, transcript_override="用户: 我的前端技术栈是 Vue3，组件库用 Element Plus。"
    )
    mock_llm.response = json.dumps({"memories": [
        {"content": "用户的技术栈是 React", "type": "fact"},
    ]})
    await memory_pipeline.extract_and_store(
        user_id=2, session_id=2, transcript_override="用户: 我的前端技术栈是 React，状态管理用 zustand。"
    )

    hits_user1, profile1 = await memory_pipeline.search_memories(1, "Vue")
    hits_user2, profile2 = await memory_pipeline.search_memories(2, "Vue")

    assert len(hits_user1) == 1 and "Vue3" in hits_user1[0]["content"]
    assert hits_user2 == []  # 行级隔离：查不到别人的记忆


# ---------------------------------------------------------------- event hook
async def test_chat_complete_event_spawns_job_no_raise(kb_env, mock_llm, monkeypatch):
    # LLM 失败也绝不向上抛（聊天主流程不可被记忆层打断）
    async def failing(system, user, max_tokens, temperature):
        raise RuntimeError("llm down")

    monkeypatch.setattr(memory_pipeline, "_call_llm_json", failing)
    await memory_pipeline.on_chat_message_complete(user_id=1, session_id=5, run_id=1)
    # 后台任务异步执行，等它跑完
    await asyncio.sleep(0.1)


# ---------------------------------------------------------------- MCP host patch
async def test_mcp_reserved_user_param_not_in_schema_but_injected():
    """`user` 是运行时注入参数：不出现在 schema（LLM 不可见不可伪造），调用时注入。"""
    from src.mcp.manager import mcp_manager

    received: dict = {}

    async def handler(content: str, user=None) -> str:
        received["content"] = content
        received["user"] = user
        return "ok"

    mcp_manager.register_tool(
        name="_test_kb_user_inject",
        description="test",
        handler=handler,
        plugin_name="knowledge_base",
        persist=False,
    )
    try:
        schema = mcp_manager.get_tool("_test_kb_user_inject").input_schema
        assert "user" not in schema.get("properties", {})
        assert "content" in schema.get("properties", {})

        sentinel = object()
        result = await mcp_manager.call_tool(
            "_test_kb_user_inject", {"content": "hi"}, user=sentinel
        )
        assert result == "ok"
        assert received["user"] is sentinel
        assert received["content"] == "hi"
    finally:
        mcp_manager.unregister_tool("_test_kb_user_inject")
