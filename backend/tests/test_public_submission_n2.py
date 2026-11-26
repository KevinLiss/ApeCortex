"""周期三推送流测试（AI 甄选自动入库）。

覆盖（pipeline/submission.py，2026-11-26 用户决策）：
- parse_analyze_response：正常 JSON / 围栏降级 / 非法字段回退 / new_category_name
- submit_public_content：文档与词条双入口、内容快照冻结、幂等（同内容在途/已入库拒），
  仅空间 owner 可推（周期三无成员概念）
- analyze_submission：AI 判无违规 → 自动入库知识中心（hub doc 拷贝 source_type="pushed"
  + wiki_ingest job + status=approved + reviewer_id=0 系统）
- AI 建新分类（new_category_name → ai_ensure_category 幂等）
- AI 判违规 → violation 队列；AI 失败 → 信任默认直接入库
- register_check：复查登记（留痕不改结果；未定论单拒）
- dispose_violation：保留存证 / 清理内容快照（记录行保留）
"""
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.db import Base
from src.plugins.builtin.knowledge_base.models import (
    KbCategory,
    KbDocSource,
    KbJob,
    KbPublicSubmission,
    KbSpace,
    KbWikiPage,
    PluginBase,
)
from src.plugins.builtin.knowledge_base.pipeline import hub as hub_mod
from src.plugins.builtin.knowledge_base.pipeline import jobs as jobs_mod
from src.plugins.builtin.knowledge_base.pipeline import memory as memory_pipeline
from src.plugins.builtin.knowledge_base.pipeline import submission as push

OWNER, STRANGER = 1, 3


@pytest.fixture
async def env(monkeypatch, tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/push.db")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(PluginBase.metadata.create_all)
    monkeypatch.setattr(push, "SessionLocal", factory)
    monkeypatch.setattr(jobs_mod, "SessionLocal", factory)
    monkeypatch.setattr(hub_mod, "SessionLocal", factory)

    # 禁后台 worker（测试内手动驱动 analyze，防止 enqueue 后 worker 并发双跑）
    async def _no_worker(user_id: int) -> None:
        return None

    monkeypatch.setattr(jobs_mod, "_ensure_worker", _no_worker)
    hub_mod.reset_hub_cache()
    yield factory
    hub_mod.reset_hub_cache()
    await engine.dispose()


async def _setup_world(env):
    """个人空间（含一份文档 + 一条词条）+ 一个分类。"""
    async with env() as db:
        cat = KbCategory(name="技术教程", sort=1)
        db.add(cat)
        await db.flush()
        priv = KbSpace(owner_user_id=OWNER, name="我的知识库", visibility="private")
        db.add(priv)
        await db.flush()
        doc = KbDocSource(
            space_id=priv.id, title="FastAPI 部署指南",
            content="FastAPI 部署的完整说明" * 10,
            content_hash="h1", source_type="upload", char_count=120,
        )
        page = KbWikiPage(space_id=priv.id, slug="deploy", title="部署词条",
            body_md="词条正文：Uvicorn 与 Gunicorn 的取舍", status="live",
            confidence=0.9, source_count=3)
        db.add_all([doc, page])
        await db.commit()
        return {"cat_id": cat.id, "priv_id": priv.id, "doc_id": doc.id, "page_id": page.id}


def _ok_resp(cat_id=1):
    return (
        f'{{"category_id": {cat_id}, "new_category_name": "", "category_reason": "技术文档",'
        ' "risk_type": "none", "risk_level": "none", "risk_reason": "",'
        ' "split_titles": ["FastAPI 部署", "Uvicorn 配置"]}'
    )


# ---------------------------------------------------------------- 解析

def test_parse_analyze_response_ok():
    raw = _ok_resp()
    r = push.parse_analyze_response(raw)
    assert r["category_id"] == 1
    assert r["risk_type"] == "none"
    assert r["split_titles"] == ["FastAPI 部署", "Uvicorn 配置"]
    assert r["new_category_name"] is None


def test_parse_analyze_response_fenced_and_invalid():
    r = push.parse_analyze_response('```json\n{"category_id": 2, "risk_type": "porn", "risk_level": "high"}\n```')
    assert r["risk_type"] == "porn" and r["risk_level"] == "high"
    assert push.parse_analyze_response("not json") == {}
    r2 = push.parse_analyze_response('{"risk_type": "hack", "risk_level": "extreme"}')
    assert r2["risk_type"] == "none" and r2["risk_level"] == "none"  # 非法值回退 none
    r3 = push.parse_analyze_response('{"new_category_name": "  运维手册  "}')
    assert r3["new_category_name"] == "运维手册"  # 裁剪空白


# ---------------------------------------------------------------- 提交

@pytest.mark.asyncio
async def test_submit_creates_frozen_snapshot(env):
    w = await _setup_world(env)

    # 非 owner 连源空间都进不去
    bad = await push.submit_public_content(STRANGER, w["priv_id"], doc_id=w["doc_id"])
    assert not bad["ok"]

    # 缺对象
    none_obj = await push.submit_public_content(OWNER, w["priv_id"])
    assert not none_obj["ok"]

    # 文档推送：快照冻结 + AI 甄选任务入队
    r = await push.submit_public_content(OWNER, w["priv_id"], doc_id=w["doc_id"])
    assert r["ok"]
    async with env() as db:
        sub = (await db.execute(select(KbPublicSubmission))).scalars().first()
        assert sub.status == "ai_reviewing"
        assert sub.doc_title == "FastAPI 部署指南"
        assert "部署" in sub.doc_content  # 快照冻结
        assert sub.source_doc_id == w["doc_id"] and sub.source_page_id == 0
        jobs = list((await db.execute(select(KbJob))).scalars().all())
        assert any(j.job_type == "public_submission" for j in jobs)

        # 幂等：同内容在途单拒绝
    dup = await push.submit_public_content(OWNER, w["priv_id"], doc_id=w["doc_id"])
    assert not dup["ok"] and "在途" in dup["msg"]

    # 词条推送入口：source_page_id 溯源
    pr = await push.submit_public_content(OWNER, w["priv_id"], page_id=w["page_id"])
    assert pr["ok"]
    async with env() as db:
        subs = list((await db.execute(
            select(KbPublicSubmission).where(KbPublicSubmission.source_page_id == w["page_id"])
        )).scalars().all())
        assert len(subs) == 1 and subs[0].doc_title == "部署词条"

    # 源文档删除后快照仍可甄选（快照冻结语义）
    async with env() as db:
        doc = await db.get(KbDocSource, w["doc_id"])
        await db.delete(doc)
        await db.commit()
    r2 = await push.submit_public_content(OWNER, w["priv_id"], doc_id=999)  # 源已删：不存在
    assert not r2["ok"]


# ---------------------------------------------------------------- AI 甄选分流

async def _analyze_with(monkeypatch, env, response: str | None):
    w = await _setup_world(env)
    await push.submit_public_content(OWNER, w["priv_id"], doc_id=w["doc_id"])
    async with env() as db:
        sub = (await db.execute(select(KbPublicSubmission))).scalars().first()
        sid = sub.id

    async def fake_llm(system: str, user: str, max_tokens: int, temperature: float):
        return response

    monkeypatch.setattr(memory_pipeline, "_call_llm_json", fake_llm)
    result = await push.analyze_submission(sid)
    async with env() as db:
        sub = await db.get(KbPublicSubmission, sid)
    return result, sub, w


@pytest.mark.asyncio
async def test_analyze_auto_approves_and_copies_to_hub(monkeypatch, env):
    """AI 判无违规 → 自动入库：hub 文档拷贝 + 编译任务 + reviewer_id=0。"""
    result, sub, w = await _analyze_with(monkeypatch, env, _ok_resp())
    assert result["ok"] and result["status"] == "approved"
    assert sub.status == "approved" and sub.reviewer_id == 0  # 系统自动，无人工审批
    assert "自动入库" in sub.review_comment
    assert sub.ai_risk_type == "none"
    assert "FastAPI 部署" in sub.ai_split_preview

    async with env() as db:
        hub = (await db.execute(
            select(KbSpace).where(KbSpace.owner_user_id == 0)
        )).scalars().first()
        assert hub is not None and hub.name == hub_mod.HUB_SPACE_NAME
        docs = list((await db.execute(
            select(KbDocSource).where(KbDocSource.space_id == hub.id)
        )).scalars().all())
        assert len(docs) == 1 and docs[0].source_type == "pushed"
        assert docs[0].title == "FastAPI 部署指南"
        # 编译拆词条任务入队（透传 category_id）
        jobs = list((await db.execute(select(KbJob))).scalars().all())
        assert any(j.job_type == "wiki_ingest" and j.space_id == hub.id for j in jobs)


@pytest.mark.asyncio
async def test_analyze_creates_new_category(monkeypatch, env):
    """AI 觉得现有分类都不合适 → 自主建新分类（幂等去重）。"""
    resp = ('{"category_id": null, "new_category_name": "运维手册", "category_reason": "部署运维类",'
            ' "risk_type": "none", "risk_level": "none", "split_titles": ["部署"]}')
    _, sub, _ = await _analyze_with(monkeypatch, env, resp)
    assert sub.status == "approved"

    async with env() as db:
        cats = list((await db.execute(
            select(KbCategory).where(KbCategory.name == "运维手册")
        )).scalars().all())
        assert len(cats) == 1  # 建了且只有一份
        assert sub.ai_category_id == cats[0].id
    # 同名再建 → 幂等复用
    cid = await hub_mod.ai_ensure_category("运维手册")
    assert cid == sub.ai_category_id


@pytest.mark.asyncio
async def test_analyze_violation_flagged(monkeypatch, env):
    resp = (
        '{"category_id": null, "new_category_name": "", "category_reason": "", '
        '"risk_type": "porn", "risk_level": "high", "split_titles": []}'
    )
    hub_id = await hub_mod.get_hub_space_id()  # 违规路径不建 hub，先显式建好再验证空
    result, sub, _ = await _analyze_with(monkeypatch, env, resp)
    assert result["status"] == "violation"
    assert sub.status == "violation"
    # 违规不入库：hub 无文档
    async with env() as db:
        docs = list((await db.execute(
            select(KbDocSource).where(KbDocSource.space_id == hub_id)
        )).scalars().all())
        assert docs == []


@pytest.mark.asyncio
async def test_analyze_failure_auto_approves(monkeypatch, env):
    """AI 失败（None）→ 信任默认直接入库（成本纪律：不阻塞推送）。"""
    result, sub, _ = await _analyze_with(monkeypatch, env, None)
    assert result["ok"] and result["status"] == "approved"
    assert sub.status == "approved"
    assert "限额" in sub.review_comment or "跳过" in sub.review_comment


# ---------------------------------------------------------------- 复查登记

@pytest.mark.asyncio
async def test_register_check(env):
    w = await _setup_world(env)
    await push.submit_public_content(OWNER, w["priv_id"], doc_id=w["doc_id"])
    async with env() as db:
        sid = (await db.execute(select(KbPublicSubmission))).scalars().first().id

    # 未定论（ai_reviewing）→ 拒
    r = await push.register_check(2, sid, "提前复查")
    assert not r["ok"]

    # 定论后登记：留痕不改结果
    async with env() as db:
        sub = await db.get(KbPublicSubmission, sid)
        sub.status = "approved"
        await db.commit()
    r2 = await push.register_check(2, sid, "复核内容正常")
    assert r2["ok"]
    async with env() as db:
        sub = await db.get(KbPublicSubmission, sid)
        assert sub.status == "approved"  # 结果不变
        assert sub.check_comment == "复核内容正常"
        assert sub.checked_by == 2 and sub.checked_at is not None


# ---------------------------------------------------------------- 违规处置

@pytest.mark.asyncio
async def test_dispose_violation(env):
    w = await _setup_world(env)
    await push.submit_public_content(OWNER, w["priv_id"], doc_id=w["doc_id"])
    async with env() as db:
        sid = (await db.execute(select(KbPublicSubmission))).scalars().first().id

    # 非法动作
    assert not (await push.dispose_violation(2, sid, "hack"))["ok"]

    # 非 violation 单不可处置（还在 ai_reviewing）
    assert not (await push.dispose_violation(2, sid, "deleted"))["ok"]

    async with env() as db:
        sub = await db.get(KbPublicSubmission, sid)
        sub.status = "violation"  # 模拟 AI 判违规
        await db.commit()

    # 保留存证：内容保留
    r = await push.dispose_violation(2, sid, "kept_evidence")
    assert r["ok"]
    async with env() as db:
        sub = await db.get(KbPublicSubmission, sid)
        assert sub.status == "disposed" and sub.disposal_action == "kept_evidence"
        assert "部署" in sub.doc_content

    # 清理内容：快照清空，记录行保留
    async with env() as db:
        sub = await db.get(KbPublicSubmission, sid)
        sub.status = "violation"  # 重置模拟第二单
        await db.commit()
    r2 = await push.dispose_violation(2, sid, "deleted")
    assert r2["ok"]
    async with env() as db:
        sub = await db.get(KbPublicSubmission, sid)
        assert sub.status == "disposed" and sub.disposal_action == "deleted"
        assert sub.doc_content == ""
        assert sub.id == sid  # 记录行仍在


@pytest.mark.asyncio
async def test_ai_ensure_category_idempotent_min(env):
    """最小复现：同库内连续两次 ai_ensure_category。"""
    c1 = await hub_mod.ai_ensure_category("手册A")
    c2 = await hub_mod.ai_ensure_category("手册A")
    assert c1 == c2, f"第一次={c1} 第二次={c2}"
