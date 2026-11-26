"""N3 聊天知识库偏好 + 自动注入 + 站点爬虫测试。

覆盖：
- get/set_chat_prefs：默认值、非法 scope 拒绝、幂等 upsert
- build_context_injection：auto_inject 关→None；scope=off→None；
  开启后包含记忆/画像/词条分节；memory_enabled 关→无记忆分节；长度护栏
- patch/unpatch runner：包装与还原幂等
- crawl_site 护栏：联网开关关→拒绝；非 http→拒绝（网络 IO 不出网，仅验护栏短路）
- _discover_links：同域过滤、静态资源跳过
"""
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.db import Base
from src.plugins.builtin.knowledge_base.collector import site as site_mod
from src.plugins.builtin.knowledge_base.models import (
    KbDocSource,
    KbMemoryItem,
    KbProfile,
    KbSetting,
    KbSpace,
    KbWikiPage,
    PluginBase,
)
from src.plugins.builtin.knowledge_base.pipeline import chat_prefs as cp
from src.plugins.builtin.knowledge_base.pipeline import memory as memory_pipeline
from src.plugins.builtin.knowledge_base.pipeline import retrieval as retrieval_mod

USER_ID = 7


@pytest.fixture
async def env(monkeypatch, tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/prefs.db")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(PluginBase.metadata.create_all)
    for mod in (cp, memory_pipeline, retrieval_mod):
        monkeypatch.setattr(mod, "SessionLocal", factory)
    yield factory
    await engine.dispose()


# ---------------------------------------------------------------- 偏好读写

@pytest.mark.asyncio
async def test_chat_prefs_defaults_and_set(env):
    prefs = await cp.get_chat_prefs(USER_ID)
    assert prefs == {
        "recall_scope": "private", "recall_space_id": 0,
        "memory_enabled": True, "auto_inject": False,
    }

    bad = await cp.set_chat_prefs(USER_ID, {"recall_scope": "galaxy"})
    assert not bad["ok"]

    ok = await cp.set_chat_prefs(USER_ID, {
        "recall_scope": "space", "recall_space_id": 5,
        "memory_enabled": False, "auto_inject": True,
    })
    assert ok["ok"]
    prefs2 = await cp.get_chat_prefs(USER_ID)
    assert prefs2["recall_scope"] == "space" and prefs2["recall_space_id"] == 5
    assert prefs2["memory_enabled"] is False and prefs2["auto_inject"] is True

    async with env() as db:
        rows = list((await db.execute(
            select(KbSetting).where(KbSetting.user_id == USER_ID)
        )).scalars().all())
        assert {r.key for r in rows} == set(cp._PREF_KEYS)  # upsert 不重复

    # 幂等：再写一次同值不报错
    assert (await cp.set_chat_prefs(USER_ID, {"recall_scope": "space", "recall_space_id": 5})).get("ok")


# ---------------------------------------------------------------- 自动注入

async def _seed_recall_data(env):
    async with env() as db:
        db.add(KbProfile(user_id=USER_ID, profile_md="## 基本信息\nVue3 独立开发者", version=1))
        db.add(KbMemoryItem(user_id=USER_ID, mem_type="fact", content="用户的技术栈是 Vue3 组合式 API",
                            content_hash="m1", confidence=0.9, source_count=2))
        space = KbSpace(owner_user_id=USER_ID, name="私有库", visibility="private")
        db.add(space)
        await db.flush()
        db.add(KbWikiPage(space_id=space.id, slug="vue3", title="Vue3 组合式 API",
                          body_md="Vue3 组合式 API 的知识 " * 10, status="live",
                          confidence=0.9, source_count=2))
        await db.commit()
        return space.id


@pytest.mark.asyncio
async def test_injection_off_and_scope_off(env):
    await _seed_recall_data(env)
    # 默认（auto_inject=False）→ 无注入
    assert await cp.build_context_injection(USER_ID, "Vue3 组合式") is None
    # scope=off → 即使 auto_inject 也无注入
    await cp.set_chat_prefs(USER_ID, {"recall_scope": "off", "auto_inject": True})
    assert await cp.build_context_injection(USER_ID, "Vue3") is None


@pytest.mark.asyncio
async def test_injection_full_sections(env):
    await _seed_recall_data(env)
    await cp.set_chat_prefs(USER_ID, {"recall_scope": "private", "auto_inject": True})
    text = await cp.build_context_injection(USER_ID, "Vue3 组合式 API")
    assert text is not None
    assert "知识库上下文" in text
    assert "用户画像摘要" in text
    assert "用户长期记忆" in text
    assert "相关知识词条" in text
    assert "Vue3" in text
    assert len(text) <= cp._INJECT_MAX_CHARS


@pytest.mark.asyncio
async def test_injection_memory_disabled(env):
    await _seed_recall_data(env)
    await cp.set_chat_prefs(USER_ID, {"recall_scope": "private", "auto_inject": True, "memory_enabled": False})
    text = await cp.build_context_injection(USER_ID, "Vue3 组合式 API")
    assert text is not None
    assert "用户画像摘要" not in text
    assert "用户长期记忆" not in text
    assert "相关知识词条" in text


@pytest.mark.asyncio
async def test_injection_public_scope_filters_private(env):
    await _seed_recall_data(env)
    await cp.set_chat_prefs(USER_ID, {"recall_scope": "public", "auto_inject": True})
    text = await cp.build_context_injection(USER_ID, "Vue3 组合式 API")
    # 无公开库内容 → 无词条分节（私有内容不因公开范围外泄）
    assert text is not None
    assert "相关知识词条" not in text


# ---------------------------------------------------------------- runner patch

@pytest.mark.asyncio
async def test_runner_patch_roundtrip():
    import src.ai.runner as runner_mod

    original = runner_mod._build_context
    try:
        cp.patch_runner_context()
        assert getattr(runner_mod, "_kb_inject_patched", False) is True
        wrapped = runner_mod._build_context
        assert wrapped is not original
        # 幂等：二次挂接不重复包装
        cp.patch_runner_context()
        assert runner_mod._build_context is wrapped
    finally:
        cp.unpatch_runner_context()
        assert runner_mod._build_context is original
        assert getattr(runner_mod, "_kb_inject_patched", False) is False


# ---------------------------------------------------------------- 爬虫护栏

@pytest.mark.asyncio
async def test_crawl_guards(env):
    # 联网开关未开启 → 拒绝（不出网）
    r = await site_mod.crawl_site(USER_ID, "https://example.com/docs")
    assert not r["ok"] and "联网开关" in r["msg"]

    # 开启后非 http 协议 → 拒绝
    async with env() as db:
        db.add(KbSetting(user_id=USER_ID, key="web_enabled", value="1"))
        await db.commit()
    r2 = await site_mod.crawl_site(USER_ID, "ftp://example.com")
    assert not r2["ok"]


def test_discover_links_same_site_only():
    base = "https://docs.example.com/intro"
    html = (
        '<a href="/guide">Guide</a>'
        '<a href="https://docs.example.com/advanced">Advanced</a>'
        '<a href="https://other.example.net/page">External</a>'
        '<a href="/static/logo.png">Image</a>'
        '<a href="/docs#section">Anchor</a>'
    )
    links = site_mod._discover_links(html, base)
    assert "https://docs.example.com/guide" in links
    assert "https://docs.example.com/advanced" in links
    assert "https://docs.example.com/docs" in links  # anchor 剥离
    assert all(l.startswith("https://docs.example.com") for l in links)
    assert all(not l.endswith(".png") for l in links)
