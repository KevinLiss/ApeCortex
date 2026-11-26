"""权限矩阵穷举（周期三回归基线）。

周期三用户决策（2026-11-26）：
- 空间无类型概念：所有空间都是个人知识库，仅 owner 可读写（成员体系下线）
- 知识中心（hub）是唯一公共库：全员可召回 live 词条；写入仅经推送管线
- 晋升流 / 自审制衡 / 贡献榜随共享空间一起下线

矩阵（个人空间 × 动作）：
    role      read   write  publish  recall
    owner     ✓      ✓      ✓        ✓
    stranger  ✗      ✗      ✗        ✗

知识中心（hub，owner_user_id=0 系统所有）：
    任何登录用户：recall live ✓；直接写（ensure_space_access）✗ —— 无人类 owner，
    hub 的唯一写路径是推送管线（submission.submit_public_content，另行测试覆盖）。
"""
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.db import Base
from src.plugins.builtin.knowledge_base.models import (
    KbSpace,
    KbWikiPage,
    PluginBase,
)
from src.plugins.builtin.knowledge_base.pipeline import compiler as c
from src.plugins.builtin.knowledge_base.pipeline import hub as hub_mod
from src.plugins.builtin.knowledge_base.pipeline import ingest_api as ingest
from src.plugins.builtin.knowledge_base.pipeline import jobs as jobs_mod
from src.plugins.builtin.knowledge_base.pipeline import memory as memory_pipeline
from src.plugins.builtin.knowledge_base.pipeline import retrieval as r
from src.plugins.builtin.knowledge_base.pipeline import submission as push

OWNER, STRANGER = 1, 4
ROLES = {"owner": OWNER, "stranger": STRANGER}


@pytest.fixture
async def env(monkeypatch, tmp_path):
    """独立 SQLite + 把插件各管线的 SessionLocal 指到它（与主测试文件同构）。"""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/perm.db")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(PluginBase.metadata.create_all)
    for mod in (c, ingest, r, jobs_mod, memory_pipeline, hub_mod, push):
        monkeypatch.setattr(mod, "SessionLocal", factory)

    # 禁后台 worker（推送测试手动驱动，防 enqueue 后 worker 并发抢跑）
    async def _no_worker(user_id: int) -> None:
        return None

    monkeypatch.setattr(jobs_mod, "_ensure_worker", _no_worker)
    hub_mod.reset_hub_cache()
    yield factory
    hub_mod.reset_hub_cache()
    await engine.dispose()


async def _make_space(env) -> int:
    async with env() as db:
        space = KbSpace(owner_user_id=OWNER, name="个人库", visibility="private")
        db.add(space)
        await db.flush()
        db.add(KbWikiPage(space_id=space.id, slug="m-draft", title="矩阵草稿",
            body_md="矩阵测试正文" * 10, status="draft", confidence=0.9, source_count=3))
        db.add(KbWikiPage(space_id=space.id, slug="m-live", title="矩阵已发布",
            body_md="矩阵测试正文" * 10, status="live", confidence=0.9, source_count=3))
        await db.commit()
        return space.id


async def _page_ids(env) -> dict:
    async with env() as db:
        rows = list((await db.execute(select(KbWikiPage))).scalars().all())
    return {p.slug: p.id for p in rows}


# ---------------------------------------------------------------- 个人空间矩阵

@pytest.mark.parametrize("role,expect", [("owner", True), ("stranger", False)])
async def test_space_read_matrix(env, role, expect):
    sid = await _make_space(env)
    assert (await ingest.get_space_role(ROLES[role], sid) is not None) is expect


@pytest.mark.parametrize("role,expect", [("owner", True), ("stranger", False)])
async def test_space_write_matrix(env, role, expect):
    sid = await _make_space(env)
    ok, _ = await ingest.ensure_space_access(ROLES[role], sid, need_write=True)
    assert ok is expect


@pytest.mark.parametrize("role,expect", [("owner", True), ("stranger", False)])
async def test_space_publish_matrix(env, role, expect):
    await _make_space(env)
    ids = await _page_ids(env)
    res = await c.publish_page(ids["m-draft"], ROLES[role], status="live")
    assert res["ok"] is expect


@pytest.mark.parametrize("role,expect", [("owner", True), ("stranger", False)])
async def test_space_recall_matrix(env, role, expect):
    """owner 从私库通道命中自己的词条；stranger 两条通道都没有。"""
    await _make_space(env)
    out = await r.search_all(ROLES[role], "矩阵测试正文")
    assert bool(out["private_pages"]) is expect
    assert out["shared_pages"] == []  # 共享通道已下线


# ---------------------------------------------------------------- 知识中心（hub）

@pytest.mark.parametrize("role", ["owner", "stranger"])
async def test_hub_recall_live_only(env, role):
    """hub：全员可召回 live；draft/candidate 对任何人都不进召回。"""
    hub_id = await hub_mod.get_hub_space_id()
    async with env() as db:
        db.add(KbWikiPage(space_id=hub_id, slug="hub-live", title="中心已发布",
            body_md="中心测试正文" * 10, status="live", confidence=0.9, source_count=3))
        db.add(KbWikiPage(space_id=hub_id, slug="hub-draft", title="中心草稿",
            body_md="中心测试正文" * 10, status="draft", confidence=0.9, source_count=3))
        await db.commit()

    out = await r.search_all(ROLES[role], "中心测试正文")
    slugs = [p["slug"] for p in out["hub_pages"]]
    assert "hub-live" in slugs
    assert "hub-draft" not in slugs


@pytest.mark.parametrize("role", ["owner", "stranger"])
async def test_hub_no_direct_write(env, role):
    """hub 无人类 owner：任何人直接写（ensure_space_access）都被拒。"""
    hub_id = await hub_mod.get_hub_space_id()
    ok, _ = await ingest.ensure_space_access(ROLES[role], hub_id, need_write=True)
    assert ok is False


# ---------------------------------------------------------------- 推送流权限

@pytest.mark.parametrize("role,expect", [("owner", True), ("stranger", False)])
async def test_submission_requires_space_owner(env, role, expect, monkeypatch):
    """推送知识中心仅空间 owner 可发起（stranger 连源空间都进不去）。"""
    sid = await _make_space(env)

    async def _fake_llm(system, user, max_tokens, temperature):
        return (
            '{"category_id": null, "new_category_name": "", '
            '"risk_type": "none", "risk_level": "none", "split_titles": []}'
        )

    monkeypatch.setattr(memory_pipeline, "_call_llm_json", _fake_llm)

    ids = await _page_ids(env)
    res = await push.submit_public_content(ROLES[role], sid, page_id=ids["m-live"])
    assert res["ok"] is expect
