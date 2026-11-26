"""N3 聊天知识库偏好（周期三启用集合模型）+ 自动注入 + 站点爬虫测试。

覆盖：
- get/set_chat_prefs：默认值（include_hub 开、enabled_spaces 空列表）、
  非法输入容错（非列表→[]）、幂等 upsert
- _is_enabled / _space_filter：hub 或空间任一启用；空列表+hub 关→未启用
- build_context_injection：auto_inject 关→None；未启用→None；
  开启后包含记忆/画像/词条分节；memory_enabled 关→无记忆分节；
  include_hub=False → 只注入个人空间词条
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
    KbMemoryItem,
    KbProfile,
    KbSetting,
    KbSpace,
    KbWikiPage,
    PluginBase,
)
from src.plugins.builtin.knowledge_base.pipeline import chat_prefs as cp
from src.plugins.builtin.knowledge_base.pipeline import hub as hub_mod
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
    for mod in (cp, memory_pipeline, retrieval_mod, hub_mod):
        monkeypatch.setattr(mod, "SessionLocal", factory)
    hub_mod.reset_hub_cache()
    yield factory
    hub_mod.reset_hub_cache()
    await engine.dispose()


# ---------------------------------------------------------------- 偏好读写

@pytest.mark.asyncio
async def test_chat_prefs_defaults_and_set(env):
    prefs = await cp.get_chat_prefs(USER_ID)
    assert prefs == {
        "include_hub": True, "spaces_mode": "all", "enabled_spaces": [],
        "memory_enabled": True, "auto_inject": False,
    }

    # 非法输入容错：非列表 → []
    bad = await cp.set_chat_prefs(USER_ID, {"enabled_spaces": "not-a-list"})
    assert bad["ok"]
    assert (await cp.get_chat_prefs(USER_ID))["enabled_spaces"] == []

    ok = await cp.set_chat_prefs(USER_ID, {
        "include_hub": False, "spaces_mode": "custom", "enabled_spaces": [1, 2, "x", 3],
        "memory_enabled": False, "auto_inject": True,
    })
    assert ok["ok"]
    prefs2 = await cp.get_chat_prefs(USER_ID)
    assert prefs2["include_hub"] is False
    assert prefs2["spaces_mode"] == "custom"
    assert prefs2["enabled_spaces"] == [1, 2, 3]  # 非法项被滤掉
    assert prefs2["memory_enabled"] is False and prefs2["auto_inject"] is True

    # 「只留知识中心」可达：custom + 空列表（旧模型做不到的表达）
    assert (await cp.set_chat_prefs(USER_ID, {
        "include_hub": True, "spaces_mode": "custom", "enabled_spaces": [],
    }))["ok"]
    only_hub = await cp.get_chat_prefs(USER_ID)
    assert only_hub["spaces_mode"] == "custom" and only_hub["enabled_spaces"] == []
    assert cp._is_enabled(only_hub) is True and cp._space_filter(only_hub) == []

    async with env() as db:
        rows = list((await db.execute(
            select(KbSetting).where(KbSetting.user_id == USER_ID)
        )).scalars().all())
        assert {r.key for r in rows} == set(cp._PREF_KEYS)  # upsert 不重复

    # 幂等：再写一次同值不报错
    assert (await cp.set_chat_prefs(USER_ID, {"enabled_spaces": [1, 2, 3]})).get("ok")


@pytest.mark.asyncio
async def test_spaces_mode_legacy_migration(env):
    """旧数据（无 mode 键）迁移推断：列表空→all；非空→custom。"""
    async with env() as db:
        db.add(KbSetting(user_id=21, key="chat_enabled_spaces", value="[]"))  # 旧默认
        db.add(KbSetting(user_id=22, key="chat_enabled_spaces", value="[7]"))  # 旧勾选
        await db.commit()
    p21 = await cp.get_chat_prefs(21)
    assert p21["spaces_mode"] == "all" and p21["enabled_spaces"] == []
    p22 = await cp.get_chat_prefs(22)
    assert p22["spaces_mode"] == "custom" and p22["enabled_spaces"] == [7]
    # 保存后 mode 键落库固化
    await cp.set_chat_prefs(21, {"spaces_mode": "custom", "enabled_spaces": []})
    p21b = await cp.get_chat_prefs(21)
    assert p21b["spaces_mode"] == "custom"


def test_is_enabled_and_space_filter():
    """周期三三态模式矩阵：mode(all/custom) × hub × 勾选列表。"""

    def mk(hub, mode, spaces):
        return {"include_hub": hub, "spaces_mode": mode, "enabled_spaces": spaces}

    # hub 开 → 启用（无论模式与列表）
    assert cp._is_enabled(mk(True, "all", [])) is True
    assert cp._is_enabled(mk(True, "custom", [])) is True   # 只留知识中心（个人空间不参与）
    # mode=all → 个人空间全参与，即使 hub 关
    assert cp._is_enabled(mk(False, "all", [])) is True
    # mode=custom：勾了才启用
    assert cp._is_enabled(mk(False, "custom", [5])) is True
    assert cp._is_enabled(mk(False, "custom", [])) is False  # 全关 → 未启用

    # 空间过滤：all → None（不过滤=全部）；custom → 勾选列表（可空=不参与）
    assert cp._space_filter(mk(True, "all", [])) is None
    assert cp._space_filter(mk(True, "custom", [])) == []      # 只留知识中心的表达
    assert cp._space_filter(mk(True, "custom", [1, 2])) == [1, 2]


# ---------------------------------------------------------------- 自动注入

async def _seed_recall_data(env):
    """用户画像/记忆 + 个人空间词条 + 知识中心 hub 词条。"""
    hub_id = await hub_mod.get_hub_space_id()
    async with env() as db:
        db.add(KbProfile(user_id=USER_ID, profile_md="## 基本信息\nVue3 独立开发者", version=1))
        db.add(KbMemoryItem(user_id=USER_ID, mem_type="fact", content="用户的技术栈是 Vue3 组合式 API",
                            content_hash="m1", confidence=0.9, source_count=2))
        space = KbSpace(owner_user_id=USER_ID, name="个人库", visibility="private")
        db.add(space)
        await db.flush()
        db.add(KbWikiPage(space_id=space.id, slug="vue3", title="Vue3 组合式 API",
                          body_md="Vue3 组合式 API 的知识 " * 10, status="live",
                          confidence=0.9, source_count=2))
        db.add(KbWikiPage(space_id=hub_id, slug="hub-fastapi", title="FastAPI 指南",
                          body_md="FastAPI 路由与依赖注入的知识 " * 10, status="live",
                          confidence=0.9, source_count=2))
        await db.commit()
        return space.id


@pytest.mark.asyncio
async def test_injection_off_and_disabled(env):
    await _seed_recall_data(env)
    # 默认（auto_inject=False）→ 无注入
    assert await cp.build_context_injection(USER_ID, "Vue3 组合式") is None
    # 未启用（hub 关 + custom 模式未选空间）→ 即使 auto_inject 也无注入
    await cp.set_chat_prefs(USER_ID, {
        "include_hub": False, "spaces_mode": "custom", "enabled_spaces": [], "auto_inject": True,
    })
    assert await cp.build_context_injection(USER_ID, "Vue3") is None
    # 但「只留知识中心」（hub 开 + custom 空列表）→ hub 词条可注入
    await cp.set_chat_prefs(USER_ID, {"include_hub": True})
    text = await cp.build_context_injection(USER_ID, "FastAPI 指南")
    assert text is not None and "FastAPI" in text
    assert "Vue3 组合式 API 的知识" not in text  # 个人空间未启用，私库词条不进注入


@pytest.mark.asyncio
async def test_injection_full_sections(env):
    await _seed_recall_data(env)
    await cp.set_chat_prefs(USER_ID, {"auto_inject": True})
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
    await cp.set_chat_prefs(USER_ID, {"auto_inject": True, "memory_enabled": False})
    text = await cp.build_context_injection(USER_ID, "Vue3 组合式 API")
    assert text is not None
    assert "用户画像摘要" not in text
    assert "用户长期记忆" not in text
    assert "相关知识词条" in text


@pytest.mark.asyncio
async def test_injection_hub_excluded(env):
    """hub 关 + custom 勾选个人空间 → 词条分节只有个人库内容，hub 词条不进注入。"""
    space_id = await _seed_recall_data(env)
    await cp.set_chat_prefs(USER_ID, {
        "include_hub": False, "spaces_mode": "custom",
        "enabled_spaces": [space_id], "auto_inject": True,
    })
    text = await cp.build_context_injection(USER_ID, "FastAPI 指南")
    assert text is not None
    assert "FastAPI 路由" not in text  # hub 词条不注入
    # hub 词条可见的查询走默认偏好（hub 开）→ hub 词条出现
    await cp.set_chat_prefs(USER_ID, {"auto_inject": True, "include_hub": True})
    text2 = await cp.build_context_injection(USER_ID, "FastAPI 指南")
    assert text2 is not None and "FastAPI" in text2


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
    assert all(link.startswith("https://docs.example.com") for link in links)
    assert all(not link.endswith(".png") for link in links)
