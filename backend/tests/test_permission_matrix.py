"""权限矩阵穷举（M3 补丁后的回归基线）。

角色 × 动作矩阵（shared 空间）：
    role      read   write  publish  recall  promote
    owner     ✓      ✓      ✓        ✓       ✓
    writer    ✓      ✓      ✓        ✓       ✗（仅 owner 可提交晋升）
    reader    ✓      ✗      ✗        ✓       ✗
    stranger  ✗      ✗      ✗        ✗       ✗

public 空间：任何登录用户 read + recall；write/publish 仅 owner。
private 空间：仅 owner 全部动作。

背景：shared 曾不落任何权限分支，导致 reader 可发布 live（v0.4.0 补丁修复）。
本文件为穷举回归，防止新增分支时再次漏项。
"""
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.db import Base
from src.plugins.builtin.knowledge_base.models import (
    KbSpace,
    KbSpaceMember,
    KbWikiPage,
    PluginBase,
)
from src.plugins.builtin.knowledge_base.pipeline import compiler as c
from src.plugins.builtin.knowledge_base.pipeline import ingest_api as ingest
from src.plugins.builtin.knowledge_base.pipeline import jobs as jobs_mod
from src.plugins.builtin.knowledge_base.pipeline import memory as memory_pipeline
from src.plugins.builtin.knowledge_base.pipeline import retrieval as r

OWNER, WRITER, READER, STRANGER = 1, 2, 3, 4
ROLE_USER = {"owner": OWNER, "writer": WRITER, "reader": READER, "stranger": STRANGER}


@pytest.fixture
async def env(monkeypatch, tmp_path):
    """独立 SQLite + 把插件各管线的 SessionLocal 指到它（与主测试文件同构）。"""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/perm.db")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(PluginBase.metadata.create_all)
    for mod in (c, ingest, r, jobs_mod, memory_pipeline):
        monkeypatch.setattr(mod, "SessionLocal", factory)
    yield factory
    await engine.dispose()


async def _make_space(env, visibility: str) -> int:
    async with env() as db:
        db.add(KbSpace(owner_user_id=OWNER, name=f"{visibility}库", visibility=visibility))
        await db.commit()
        space = (await db.execute(select(KbSpace))).scalars().first()
        db.add(KbWikiPage(space_id=space.id, slug="m-draft", title="矩阵草稿",
            body_md="矩阵测试正文" * 10, status="draft", confidence=0.9, source_count=3))
        db.add(KbWikiPage(space_id=space.id, slug="m-live", title="矩阵已发布",
            body_md="矩阵测试正文" * 10, status="live", confidence=0.9, source_count=3))
        if visibility == "shared":
            db.add(KbSpaceMember(space_id=space.id, user_id=WRITER, role="writer"))
            db.add(KbSpaceMember(space_id=space.id, user_id=READER, role="reader"))
        await db.commit()
        return space.id


async def _page_ids(env):
    async with env() as db:
        rows = list((await db.execute(select(KbWikiPage))).scalars().all())
    return {p.slug: p.id for p in rows}


# ---------------------------------------------------------------- shared 空间矩阵
@pytest.mark.parametrize("role,expect", [("owner", True), ("writer", True), ("reader", True), ("stranger", False)])
async def test_shared_read_matrix(env, role, expect):
    sid = await _make_space(env, "shared")
    assert (await ingest.get_space_role(ROLE_USER[role], sid) is not None) is expect


@pytest.mark.parametrize("role,expect", [("owner", True), ("writer", True), ("reader", False), ("stranger", False)])
async def test_shared_write_matrix(env, role, expect):
    sid = await _make_space(env, "shared")
    ok, _ = await ingest.ensure_space_access(ROLE_USER[role], sid, need_write=True)
    assert ok is expect


@pytest.mark.parametrize("role,expect", [("owner", True), ("writer", True), ("reader", False), ("stranger", False)])
async def test_shared_publish_matrix(env, role, expect):
    sid = await _make_space(env, "shared")
    ids = await _page_ids(env)
    res = await c.publish_page(ids["m-draft"], ROLE_USER[role], status="live")
    assert res["ok"] is expect


@pytest.mark.parametrize("role,expect", [("owner", True), ("writer", True), ("reader", True), ("stranger", False)])
async def test_shared_recall_matrix(env, role, expect):
    """owner 从私有通道命中自己的空间；成员从共享通道命中；非成员两条通道都没有。"""
    await _make_space(env, "shared")
    out = await r.search_all(ROLE_USER[role], "矩阵测试正文")
    hit = bool(out["shared_pages"]) or bool(out["private_pages"])
    assert hit is expect
    if role == "owner":
        assert out["shared_pages"] == []  # owner 走私有通道，不重复出现在共享通道


@pytest.mark.parametrize("role,expect", [("owner", True), ("writer", False), ("reader", False), ("stranger", False)])
async def test_shared_promote_matrix(env, role, expect):
    """晋升提交仍仅 owner（writer 走 owner 审核后由 owner 提交）。"""
    sid = await _make_space(env, "shared")
    async with env() as db:
        db.add(KbSpace(owner_user_id=OWNER, name="公开目标", visibility="public"))
        await db.commit()
        target = (await db.execute(
            select(KbSpace).where(KbSpace.visibility == "public")
        )).scalars().first()
        target_id = target.id
    ids = await _page_ids(env)
    res = await c.submit_promotion(ids["m-draft"], ROLE_USER[role], target_id, "矩阵")
    assert res["ok"] is expect


# ---------------------------------------------------------------- public / private
@pytest.mark.parametrize("role,expect", [("owner", True), ("writer", False), ("reader", False), ("stranger", False)])
async def test_public_write_only_owner(env, role, expect):
    sid = await _make_space(env, "public")
    ok, _ = await ingest.ensure_space_access(ROLE_USER[role], sid, need_write=True)
    assert ok is expect


@pytest.mark.parametrize("role,expect", [("owner", True), ("writer", True), ("reader", True), ("stranger", True)])
async def test_public_read_all_logged_in(env, role, expect):
    sid = await _make_space(env, "public")
    assert (await ingest.get_space_role(ROLE_USER[role], sid) is not None) is expect


@pytest.mark.parametrize("role", ["owner", "writer", "reader", "stranger"])
async def test_public_recall_live_only(env, role):
    """公开库任何登录用户可召回 live；draft 对任何人都不进召回。"""
    await _make_space(env, "public")
    out = await r.search_all(ROLE_USER[role], "矩阵测试正文")
    slugs = [p["slug"] for p in out["public_pages"]]
    assert "m-live" in slugs
    assert "m-draft" not in slugs


@pytest.mark.parametrize("role,expect", [("owner", True), ("writer", False), ("reader", False), ("stranger", False)])
async def test_private_write_only_owner(env, role, expect):
    sid = await _make_space(env, "private")
    ok, _ = await ingest.ensure_space_access(ROLE_USER[role], sid, need_write=True)
    assert ok is expect


@pytest.mark.parametrize("role,expect", [("owner", True), ("writer", False), ("reader", False), ("stranger", False)])
async def test_private_read_only_owner(env, role, expect):
    sid = await _make_space(env, "private")
    assert (await ingest.get_space_role(ROLE_USER[role], sid) is not None) is expect
