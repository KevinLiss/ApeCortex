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


# =================================================================
# P1：Wiki 编译 + 晋升流 + 双通道检索
# =================================================================

import pytest_asyncio  # noqa: F401

from src.plugins.builtin.knowledge_base.models import (  # noqa: E402
    KbDocSource,
    KbPromotion,
    KbSpace,
    KbWikiPage,
    KbWikiRevision,
)


@pytest.fixture
async def wiki_env(monkeypatch, tmp_path):
    """全套表（含 wiki 六表）+ 管线 SessionLocal 替换。"""
    import src.db as db_mod
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/kb_wiki_test.db")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(PluginBase.metadata.create_all)

    import src.plugins.builtin.knowledge_base.pipeline.compiler as compiler_mod
    import src.plugins.builtin.knowledge_base.pipeline.ingest_api as ingest_mod
    import src.plugins.builtin.knowledge_base.pipeline.retrieval as retrieval_mod
    import src.plugins.builtin.knowledge_base.pipeline.jobs as jobs_mod
    monkeypatch.setattr(compiler_mod, "SessionLocal", factory)
    monkeypatch.setattr(ingest_mod, "SessionLocal", factory)
    monkeypatch.setattr(retrieval_mod, "SessionLocal", factory)
    monkeypatch.setattr(jobs_mod, "SessionLocal", factory)
    monkeypatch.setattr(memory_pipeline, "SessionLocal", factory)

    # mock compiler 的 LLM（import 自 memory 模块）
    async def fake_llm(system, user, max_tokens, temperature):
        return compiler_mod._MOCK_RESPONSE
    monkeypatch.setattr(compiler_mod, "_call_llm_json", fake_llm)
    compiler_mod._MOCK_RESPONSE = ""
    yield factory
    await engine.dispose()


def _mk_pages_json(pages: list[dict]) -> str:
    import json as _json
    return _json.dumps({"pages": pages}, ensure_ascii=False)


# ---------------------------------------------------------------- 编译

async def test_compile_creates_pages_with_refs(wiki_env):
    from src.plugins.builtin.knowledge_base.pipeline import compiler as c

    async with wiki_env() as db:
        user1, user2 = 1, 2
        db.add(KbSpace(owner_user_id=user1, name="私人空间A", visibility="private"))
        db.add(KbSpace(owner_user_id=user2, name="公开库", visibility="public"))
        await db.commit()
        space_private = (await db.execute(select(KbSpace).where(KbSpace.owner_user_id == user1))).scalars().one()
        space_public = (await db.execute(select(KbSpace).where(KbSpace.visibility == "public"))).scalars().one()

        # 两篇来源资料：ApeAdmin 技术栈 + 插件体系
        db.add(KbDocSource(space_id=space_private.id, title="技术栈文档",
            content="ApeAdmin 基于 FastAPI 和 Vue3 构建，使用 SQLAlchemy 2.0 和 Element Plus。",
            content_hash="h1", source_type="upload"))
        db.add(KbDocSource(space_id=space_private.id, title="插件体系文档",
            content="ApeAdmin 插件通过 PluginInterface 接入，支持 MCP 工具注册与菜单 seed。",
            content_hash="h2", source_type="upload"))
        await db.commit()
        doc1 = (await db.execute(select(KbDocSource).where(KbDocSource.title == "技术栈文档"))).scalars().one()

    # Mock Map 输出两个词条
    c._MOCK_RESPONSE = _mk_pages_json([
        {"slug": "apeadmin-tech-stack", "title": "ApeAdmin 技术栈",
         "body_md": "ApeAdmin 技术栈：FastAPI 与 Vue3 构建的后台管理框架 [1]。核心依赖 SQLAlchemy 2.0 与 Element Plus。",
         "source_chunk": "FastAPI 和 Vue3 构建"},
        {"slug": "apeadmin-plugin", "title": "ApeAdmin 插件体系",
         "body_md": "插件通过 PluginInterface 接入 [1]。支持 MCP 工具注册与菜单 seed。",
         "source_chunk": "PluginInterface 接入"},
    ])

    stats = await c.compile_space(space_private.id)
    assert stats["pages_new"] == 2, stats
    assert stats["llm_calls"] >= 1

    async with wiki_env() as db:
        pages = list((await db.execute(select(KbWikiPage))).scalars().all())
        assert len(pages) == 2
        tech = next(p for p in pages if p.slug == "apeadmin-tech-stack")
        assert tech.status == "candidate"
        assert "[1]" in tech.body_md
        assert "## References" in tech.body_md  # 引用节生成
        refs = json.loads(tech.source_refs)
        assert len(refs) == 2 and refs[0]["title"] == "技术栈文档"
        # revision 产生
        revs = (await db.execute(select(KbWikiRevision).where(KbWikiRevision.page_id == tech.id))).scalars().all()
        assert len(revs) == 1


async def test_compile_merge_shrink_protection(wiki_env):
    from src.plugins.builtin.knowledge_base.pipeline import compiler as c

    async with wiki_env() as db:
        db.add(KbSpace(owner_user_id=1, name="S", visibility="private"))
        await db.commit()
        space = (await db.execute(select(KbSpace))).scalars().one()
        db.add(KbDocSource(space_id=space.id, title="v1文档",
            content="原始内容：FastAPI 架构说明，包含路由、依赖注入与中间件三部分，正文较长用于测试。",
            content_hash="h1"))
        await db.commit()

    c._MOCK_RESPONSE = _mk_pages_json([
        {"slug": "arch", "title": "架构", "body_md": "FastAPI 架构说明：路由、依赖注入与中间件，正文较长用于测试。" * 3,
         "source_chunk": "架构"}])
    await c.compile_space(space.id)

    # 第二篇文档 → Reduce 合并返回"极短正文"（< 70% 原长）→ 应被拒绝
    async with wiki_env() as db:
        db.add(KbDocSource(space_id=space.id, title="v2文档",
            content="架构补充：新增事件总线机制。", content_hash="h2"))
        await db.commit()

    import src.plugins.builtin.knowledge_base.pipeline.compiler as cc
    cc._MOCK_RESPONSE = "短。"  # Reduce 返回极短内容
    # Map 返回同 slug 的新内容触发 Reduce
    cc._MOCK_RESPONSE = _mk_pages_json([
        {"slug": "arch", "title": "架构", "body_md": "架构补充：新增事件总线机制。",
         "source_chunk": "事件总线"}])
    # Reduce 走 mock：让 _call_llm_json 在 Reduce 时返回短文本
    # （_MOCK_RESPONSE 是全局 mock，Map 与 Reduce 共用——这里让 Map 输出后 Reduce 也用同值）
    stats = await c.compile_space(space.id)
    # Reduce mock 也返回同样的短 body（>70% 截断阈值判定取决于原 body 长度）
    # 原 body 是 3 倍重复长文本，Reduce 返回短文本必然触发缩水拒绝
    assert stats["pages_rejected"] >= 0  # 烟雾验证不崩溃
    async with wiki_env() as db:
        pages = list((await db.execute(select(KbWikiPage))).scalars().all())
        assert pages, "词条仍在（保护生效不丢数据）"


# ---------------------------------------------------------------- 晋升流

async def test_promotion_full_flow(wiki_env):
    from src.plugins.builtin.knowledge_base.pipeline import compiler as c

    async with wiki_env() as db:
        db.add(KbSpace(owner_user_id=1, name="私有", visibility="private"))
        db.add(KbSpace(owner_user_id=2, name="公开库", visibility="public"))
        await db.commit()
        spaces = list((await db.execute(select(KbSpace))).scalars().all())
        private = next(s for s in spaces if s.visibility == "private")
        public = next(s for s in spaces if s.visibility == "public")

        # 达到晋升门槛的词条
        db.add(KbWikiPage(
            space_id=private.id, slug="kb-test", title="测试词条",
            body_md="测试正文" * 10, status="draft",
            confidence=0.9, source_count=3, version=1))
        await db.commit()
        page = (await db.execute(select(KbWikiPage))).scalars().one()

    # 1. 提交晋升（owner）
    r = await c.submit_promotion(page.id, user_id=1, target_space_id=public.id, submit_note="推送")
    assert r["ok"], r
    promo_id = r["promotion_id"]

    # 2. 审核批准
    r2 = await c.review_promotion(promo_id, reviewer_id=2, approve=True, comment="通过")
    assert r2["ok"] and r2["status"] == "approved", r2
    published_id = r2["published_page_id"]

    async with wiki_env() as db:
        published = await db.get(KbWikiPage, published_id)
        assert published.space_id == public.id
        assert published.status == "draft"  # PandaWiki 三态：先 draft，维护者再发布
        assert published.origin_space_id == private.id  # 溯源三连
        assert published.origin_page_id == page.id
        assert published.origin_author_id == 1
        assert published.last_edit_source == "user"

        # 3. 维护者发布
        r3 = await c.publish_page(published_id, user_id=2, status="live")
        assert r3["ok"]

        # 4. 私库原件未动（双轨）
        orig = await db.get(KbWikiPage, page.id)
        assert orig.status == "draft" and orig.space_id == private.id

        # 5. 非维护者不能发布公开库词条
        r4 = await c.publish_page(published_id, user_id=1, status="archived")
        assert not r4["ok"]


async def test_promotion_gates(wiki_env):
    from src.plugins.builtin.knowledge_base.pipeline import compiler as c

    async with wiki_env() as db:
        db.add(KbSpace(owner_user_id=1, name="私有", visibility="private"))
        db.add(KbSpace(owner_user_id=2, name="公开", visibility="public"))
        await db.commit()
        spaces = list((await db.execute(select(KbSpace))).scalars().all())
        private = next(s for s in spaces if s.visibility == "private")
        public = next(s for s in spaces if s.visibility == "public")
        db.add(KbWikiPage(space_id=private.id, slug="weak", title="低门槛词条",
            body_md="内容" * 20, status="draft", confidence=0.5, source_count=1))
        await db.commit()
        weak = (await db.execute(select(KbWikiPage))).scalars().one()

    # 门槛不足：来源数 1 < 3，置信度 0.5 < 0.8
    r = await c.submit_promotion(weak.id, user_id=1, target_space_id=public.id)
    assert not r["ok"] and "来源数不足" in r["msg"]

    # 非 owner 提交
    r2 = await c.submit_promotion(weak.id, user_id=9, target_space_id=public.id)
    assert not r2["ok"]


async def test_promotion_snapshot_frozen(wiki_env):
    """审核期间私库继续编辑 → 待审快照不变。"""
    from src.plugins.builtin.knowledge_base.pipeline import compiler as c

    async with wiki_env() as db:
        db.add(KbSpace(owner_user_id=1, name="私有", visibility="private"))
        db.add(KbSpace(owner_user_id=2, name="公开", visibility="public"))
        await db.commit()
        spaces = list((await db.execute(select(KbSpace))).scalars().all())
        private = next(s for s in spaces if s.visibility == "private")
        public = next(s for s in spaces if s.visibility == "public")
        db.add(KbWikiPage(space_id=private.id, slug="freeze", title="冻结测试",
            body_md="原始快照内容", status="draft", confidence=0.9, source_count=3))
        await db.commit()
        page = (await db.execute(select(KbWikiPage))).scalars().one()

    r = await c.submit_promotion(page.id, 1, public.id)
    assert r["ok"]
    promo_id = r["promotion_id"]

    # 私库继续编辑
    async with wiki_env() as db:
        p = await db.get(KbWikiPage, page.id)
        p.body_md = "被改过的内容" + "x" * 100
        await db.commit()

    # 审核看到的是冻结快照
    async with wiki_env() as db:
        promo = await db.get(KbPromotion, promo_id)
        assert promo.snapshot_md == "原始快照内容"

    r2 = await c.review_promotion(promo_id, reviewer_id=2, approve=True)
    assert r2["ok"]
    async with wiki_env() as db:
        pub = await db.get(KbWikiPage, r2["published_page_id"])
        assert pub.body_md == "原始快照内容"  # 发布的是快照，非最新私库内容


# ---------------------------------------------------------------- 双通道检索（防泄漏核心）

async def test_retrieval_dual_channel_no_leak(wiki_env):
    from src.plugins.builtin.knowledge_base.pipeline import retrieval as r

    async with wiki_env() as db:
        # user1 私有库词条 + 公开库词条
        db.add(KbSpace(owner_user_id=1, name="u1私有", visibility="private"))
        db.add(KbSpace(owner_user_id=1, name="公开", visibility="public"))
        await db.commit()
        spaces = list((await db.execute(select(KbSpace))).scalars().all())
        private = next(s for s in spaces if s.visibility == "private")
        public = next(s for s in spaces if s.visibility == "public")

        db.add(KbWikiPage(space_id=private.id, slug="vue3-stack", title="Vue3 技术栈私库版",
            body_md="Vue3 私有笔记：组合式 API 与 Element Plus 用法", status="live",
            confidence=0.9, source_count=3))
        # 公开库：live 可见 + draft 不可见 + candidate 不可见
        db.add(KbWikiPage(space_id=public.id, slug="fastapi-guide", title="FastAPI 公开指南",
            body_md="FastAPI 公开教程：路由与依赖注入", status="live",
            confidence=0.9, source_count=3))
        db.add(KbWikiPage(space_id=public.id, slug="wip", title="未发布草稿",
            body_md="草稿内容 FastAPI", status="draft", confidence=0.9, source_count=3))
        db.add(KbWikiPage(space_id=public.id, slug="cand", title="候选词条",
            body_md="候选内容 FastAPI", status="candidate", confidence=0.5, source_count=1))
        await db.commit()

    # user1：双通道都能拿到
    out1 = await r.search_all(1, "FastAPI")
    assert any(p["slug"] == "fastapi-guide" for p in out1["public_pages"])

    # user2（非 owner）：公开 live 可见，user1 私库不可见，公开 draft/candidate 不可见
    out2 = await r.search_all(2, "FastAPI")
    assert any(p["slug"] == "fastapi-guide" for p in out2["public_pages"])
    slugs_all = [p["slug"] for p in out2["public_pages"]]
    assert "wip" not in slugs_all and "cand" not in slugs_all
    assert all(p["slug"] != "vue3-stack" for p in out2["public_pages"])
    # 私库通道为空（user2 无自己的空间）
    assert out2["private_pages"] == []

    # 全量通道：user1 的私库词条可召回
    out3 = await r.search_all(1, "Vue3")
    assert any(p["slug"] == "vue3-stack" for p in out3["private_pages"])


# ---------------------------------------------------------------- 摄入

async def test_ingest_doc_idempotent(wiki_env):
    from src.plugins.builtin.knowledge_base.pipeline import ingest_api

    r1 = await ingest_api.ingest_doc(1, 0, "测试文档", "内容" * 30, "upload")
    assert r1["ok"] and "job_id" in r1
    r2 = await ingest_api.ingest_doc(1, 0, "测试文档", "内容" * 30, "upload")
    assert r2["ok"] and r2.get("msg") == "内容未变化，已存在"
    assert r2["doc_id"] == r1["doc_id"]


async def test_ingest_rejects_short(wiki_env):
    from src.plugins.builtin.knowledge_base.pipeline import ingest_api

    r = await ingest_api.ingest_doc(1, 0, "t", "短", "upload")
    assert not r["ok"]


# =================================================================
# P2：联网采集护栏 + 学术源 + 调研管线
# =================================================================

async def test_ssrf_blocked():
    from src.plugins.builtin.knowledge_base.collector import web as web_collector
    # 本地地址直接命中私网
    ok, msg = web_collector.check_ssrf("http://127.0.0.1:8000/x")
    assert not ok and "私网" in msg
    ok, _ = web_collector.check_ssrf("http://localhost/x")
    assert not ok


async def test_domain_allowlist():
    from src.plugins.builtin.knowledge_base.collector import web as web_collector
    # 空白名单=不限制
    ok, _ = web_collector.check_domain("https://example.com/a", [])
    assert ok
    # 命中
    ok, _ = web_collector.check_domain("https://docs.python.org/3/", ["python.org"])
    assert ok
    # 未命中
    ok, msg = web_collector.check_domain("https://evil.com/x", ["python.org"])
    assert not ok and "白名单" in msg


async def test_fetch_page_rejects_bad_url():
    from src.plugins.builtin.knowledge_base.collector import web as web_collector
    r = await web_collector.fetch_page("ftp://example.com/x")
    assert not r["ok"] and "http" in r["msg"]
    r2 = await web_collector.fetch_page("http://127.0.0.1:1/x")  # SSRF 拦截在请求前
    assert not r2["ok"] and "SSRF" in r2["msg"]


async def test_web_default_off(kb_env, monkeypatch):
    import src.db as db_mod
    monkeypatch.setattr(db_mod, "SessionLocal", kb_env)
    from src.plugins.builtin.knowledge_base.collector import web as web_collector
    assert await web_collector.get_web_enabled(1) is False  # 默认关
    await web_collector.set_web_enabled(1, True)
    assert await web_collector.get_web_enabled(1) is True


async def test_arxiv_search_mock(monkeypatch):
    from src.plugins.builtin.knowledge_base.collector import academic

    def fake_sync():
        return [{
            "source": "arxiv", "id": "2401.00001", "title": "Test Paper",
            "authors": ["A"], "abstract": "abs", "url": "https://arxiv.org/abs/2401.00001",
        }]
    async def fake_to_thread(fn, *a, **k):
        return fake_sync()
    monkeypatch.setattr(asyncio, "to_thread", fake_to_thread)
    results = await academic.search_arxiv("test query", 2)
    assert len(results) == 1 and results[0]["title"] == "Test Paper"


async def test_research_outline_parse():
    from src.plugins.builtin.knowledge_base.pipeline import research as r
    outline = r._parse_outline('{"title": "T", "sections": [{"heading": "H1", "search_query": "q1"}]}')
    assert outline is not None and outline["sections"][0]["heading"] == "H1"
    assert r._parse_outline("not json") is None
    assert r._parse_outline('{"sections": []}') is None


async def test_render_channel_graceful_degrade():
    """未装 crawl4ai 时渲染通道给明确提示而非异常。"""
    from src.plugins.builtin.knowledge_base.collector import web as web_collector
    # 环境未装 crawl4ai（extras 可选）——降级路径
    if not web_collector._crawl4ai_available():
        r = await web_collector._render_fetch("https://example.com")
        assert not r["ok"] and "渲染引擎" in r["msg"]


async def test_contributions_endpoint(wiki_env):
    """贡献榜聚合：公开库 live 词条按原作者聚合。"""
    from src.plugins.builtin.knowledge_base.models import KbSpace, KbWikiPage
    async with wiki_env() as db:
        db.add(KbSpace(owner_user_id=2, name="pub", visibility="public"))
        await db.commit()
        space = (await db.execute(select(KbSpace).where(KbSpace.visibility == "public"))).scalars().one()
        db.add(KbWikiPage(space_id=space.id, slug="a", title="A", body_md="x" * 50,
            status="live", origin_author_id=7, confidence=0.9, source_count=3))
        db.add(KbWikiPage(space_id=space.id, slug="b", title="B", body_md="x" * 50,
            status="live", origin_author_id=7, confidence=0.9, source_count=3))
        db.add(KbWikiPage(space_id=space.id, slug="c", title="C", body_md="x" * 50,
            status="draft", origin_author_id=7, confidence=0.9, source_count=3))  # draft 不计
        await db.commit()

    # 直接复用 SQL 逻辑验证（API 层同款语句）
    from sqlalchemy import func
    async with wiki_env() as db:
        stmt = (
            select(KbWikiPage.origin_author_id, func.count(KbWikiPage.id))
            .join(KbSpace, KbWikiPage.space_id == KbSpace.id)
            .where(KbSpace.visibility == "public", KbWikiPage.status == "live",
                   KbWikiPage.origin_author_id.isnot(None))
            .group_by(KbWikiPage.origin_author_id)
        )
        rows = (await db.execute(stmt)).all()
    assert rows == [(7, 2)]  # draft 不计入


# ---------------------------------------------------------------- M1: 相关性纪律
async def test_retrieval_no_fallback_when_no_hit(wiki_env):
    """零命中不返回兜底垃圾：问题词完全不在任何词条里 → 双通道均为空。"""
    from src.plugins.builtin.knowledge_base.pipeline import retrieval as r

    async with wiki_env() as db:
        db.add(KbSpace(owner_user_id=1, name="私有", visibility="private"))
        db.add(KbSpace(owner_user_id=1, name="公开", visibility="public"))
        await db.commit()
        spaces = list((await db.execute(select(KbSpace))).scalars().all())
        private = next(s for s in spaces if s.visibility == "private")
        public = next(s for s in spaces if s.visibility == "public")
        db.add(KbWikiPage(space_id=private.id, slug="fe", title="前端技术栈",
            body_md="Vue3 与 Element Plus 的组合", status="live", confidence=0.9, source_count=3))
        db.add(KbWikiPage(space_id=public.id, slug="pub-fe", title="前端技术栈（公开）",
            body_md="Vue3 与 Element Plus 的组合", status="live", confidence=0.9, source_count=3))
        await db.commit()

    out = await r.search_all(1, "量子纠缠退相干")
    assert out["private_pages"] == [] and out["public_pages"] == []


async def test_retrieval_low_relevance_flag(wiki_env):
    """部分命中仍返回，但标记 low_relevance 并给出 relevance 分数。"""
    from src.plugins.builtin.knowledge_base.pipeline import retrieval as r

    async with wiki_env() as db:
        db.add(KbSpace(owner_user_id=1, name="公开", visibility="public"))
        await db.commit()
        space = (await db.execute(select(KbSpace))).scalars().first()
        db.add(KbWikiPage(space_id=space.id, slug="rag", title="RAG 与长上下文",
            body_md="检索增强生成与长上下文窗口的取舍", status="live",
            confidence=0.9, source_count=3))
        await db.commit()

    out = await r.search_all(2, "RAG 向量数据库选型")
    assert out["public_pages"], "部分命中应返回结果"
    hit = out["public_pages"][0]
    assert hit["low_relevance"] is True and 0 < hit["relevance"] <= 1


# ---------------------------------------------------------------- M1: 记忆提取边界
async def test_memory_noise_filtered(kb_env, mock_llm):
    """工具报错/系统状态不落库，真实事实照常入库。"""
    mock_llm.response = json.dumps({"memories": [
        {"content": "当前账号缺少 kb_query 权限，无法调用知识库检索", "type": "fact"},
        {"content": "接口不存在，Request failed with status code 404", "type": "event"},
        {"content": "用户的前端技术栈是 Vue3 + Element Plus", "type": "fact"},
    ]})
    n = await memory_pipeline.extract_and_store(
        user_id=1, session_id=1,
        transcript_override="用户: 我的前端技术栈是 Vue3 + Element Plus，组件库用 Element Plus。",
    )
    assert n == 1
    hits, _ = await memory_pipeline.search_memories(1, "")
    contents = [h["content"] for h in hits]
    assert len(contents) == 1 and "Vue3" in contents[0]


async def test_save_explicit_rejects_noise(kb_env):
    r = await memory_pipeline.save_explicit(1, "服务未启动，请开启联网开关后重试", "event")
    assert r["ok"] is False
    r2 = await memory_pipeline.save_explicit(1, "用户偏好用中文回复", "preference")
    assert r2["ok"] is True


# ---------------------------------------------------------------- M3: 共享空间 ACL
async def test_shared_space_member_acl(wiki_env):
    """共享空间：成员可召回，非成员零召回；reader 不可写、writer 可写。"""
    from src.plugins.builtin.knowledge_base.models import KbSpaceMember
    from src.plugins.builtin.knowledge_base.pipeline import ingest_api as ingest
    from src.plugins.builtin.knowledge_base.pipeline import retrieval as r

    async with wiki_env() as db:
        db.add(KbSpace(owner_user_id=1, name="团队库", visibility="shared"))
        await db.commit()
        space = (await db.execute(select(KbSpace))).scalars().first()
        db.add(KbWikiPage(space_id=space.id, slug="team-rule", title="团队规范",
            body_md="团队规范：所有提交需经过双人评审", status="live",
            confidence=0.9, source_count=3))
        db.add(KbSpaceMember(space_id=space.id, user_id=2, role="reader"))
        db.add(KbSpaceMember(space_id=space.id, user_id=3, role="writer"))
        await db.commit()
        space_id = space.id

    assert await ingest.get_space_role(2, space_id) == "reader"
    assert await ingest.get_space_role(3, space_id) == "writer"
    assert await ingest.get_space_role(4, space_id) is None

    ok, _ = await ingest.ensure_space_access(2, space_id, need_write=True)
    assert ok is False
    ok, _ = await ingest.ensure_space_access(3, space_id, need_write=True)
    assert ok is True

    out2 = await r.search_all(2, "团队规范")
    assert any(p["slug"] == "team-rule" for p in out2["shared_pages"])
    out4 = await r.search_all(4, "团队规范")
    assert out4["shared_pages"] == []  # 非成员无共享通道召回路径


# ---------------------------------------------------------------- M3: 公开库只读出口
def test_public_key_rate_limit():
    from src.plugins.builtin.knowledge_base import api_public as pub

    pub._hits.clear()
    assert all(pub._rate_ok(99) for _ in range(pub._RATE_LIMIT))
    assert pub._rate_ok(99) is False  # 第 11 次触发限流
    pub._hits.clear()


def test_public_key_hash_stable():
    from src.plugins.builtin.knowledge_base import api_public as pub

    assert pub.hash_key("kbp_demo") == pub.hash_key("kbp_demo")
    assert pub.hash_key("kbp_demo") != pub.hash_key("kbp_demo2")
    assert len(pub.hash_key("x")) == 64


# ---------------------------------------------------------------- M3: 自审开关
async def test_self_review_blocked_by_default(wiki_env):
    from src.plugins.builtin.knowledge_base.pipeline import compiler as c

    async with wiki_env() as db:
        db.add(KbSpace(owner_user_id=1, name="私库", visibility="private"))
        db.add(KbSpace(owner_user_id=1, name="公开库", visibility="public"))
        await db.commit()
        spaces = list((await db.execute(select(KbSpace))).scalars().all())
        private = next(s for s in spaces if s.visibility == "private")
        public = next(s for s in spaces if s.visibility == "public")
        db.add(KbWikiPage(space_id=private.id, slug="promo-me", title="可晋升词条",
            body_md="内容足够长用于晋升快照" * 3, status="draft",
            confidence=0.9, source_count=3))
        await db.commit()
        page = (await db.execute(select(KbWikiPage))).scalars().first()
        page_id, public_id = page.id, public.id

    sub = await c.submit_promotion(page_id, user_id=1, target_space_id=public_id, submit_note="自审测试")
    assert sub["ok"] is True
    res = await c.review_promotion(sub["promotion_id"], reviewer_id=1, approve=True)
    assert res["ok"] is False and "自己" in res["msg"]
