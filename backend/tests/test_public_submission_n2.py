"""N2 公开库推送审核流测试。

覆盖：
- parse_analyze_response：正常 JSON / 围栏降级 / 非法字段回退
- submit_public_doc：内容快照冻结、幂等（同内容在途单拒绝）、目标库必须公开
- analyze_submission：AI 判正常→pending、判违规→violation、AI 失败→pending
- review_submission：通过→快照文档拷入公开库并触发编译 job；拒绝→rejected；自审制衡
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
    PluginBase,
)
from src.plugins.builtin.knowledge_base.pipeline import submission as push
from src.plugins.builtin.knowledge_base.pipeline import jobs as jobs_mod
from src.plugins.builtin.knowledge_base.pipeline import memory as memory_pipeline

OWNER, REVIEWER, OTHER = 1, 2, 3


@pytest.fixture
async def env(monkeypatch, tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/push.db")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(PluginBase.metadata.create_all)
    monkeypatch.setattr(push, "SessionLocal", factory)
    monkeypatch.setattr(jobs_mod, "SessionLocal", factory)
    yield factory
    await engine.dispose()


async def _setup_world(env):
    """私有空间（含一份文档）+ 公开库 + 分类。"""
    async with env() as db:
        cat = KbCategory(name="技术教程", sort=1)
        db.add(cat)
        await db.flush()
        priv = KbSpace(owner_user_id=OWNER, name="我的私有库", visibility="private")
        pub = KbSpace(owner_user_id=REVIEWER, name="公开总库", visibility="public")
        db.add_all([priv, pub])
        await db.flush()
        doc = KbDocSource(
            space_id=priv.id, title="FastAPI 部署指南",
            content="FastAPI 部署的完整说明" * 10,
            content_hash="h1", source_type="upload", char_count=120,
        )
        db.add(doc)
        await db.commit()
        return {"cat_id": cat.id, "priv_id": priv.id, "pub_id": pub.id, "doc_id": doc.id}


# ---------------------------------------------------------------- 解析

def test_parse_analyze_response_ok():
    raw = '{"category_id": 1, "category_reason": "技术内容", "risk_type": "none", "risk_level": "none", "risk_reason": "", "split_titles": ["FastAPI 部署", "Uvicorn 配置"]}'
    r = push.parse_analyze_response(raw)
    assert r["category_id"] == 1
    assert r["risk_type"] == "none"
    assert r["split_titles"] == ["FastAPI 部署", "Uvicorn 配置"]


def test_parse_analyze_response_fenced_and_invalid():
    r = push.parse_analyze_response('```json\n{"category_id": 2, "risk_type": "porn", "risk_level": "high"}\n```')
    assert r["risk_type"] == "porn" and r["risk_level"] == "high"
    assert push.parse_analyze_response("not json") == {}
    r2 = push.parse_analyze_response('{"risk_type": "hack", "risk_level": "extreme"}')
    assert r2["risk_type"] == "none" and r2["risk_level"] == "none"


# ---------------------------------------------------------------- 提交

@pytest.mark.asyncio
async def test_submit_creates_frozen_snapshot(env):
    w = await _setup_world(env)
    # 目标传非公开库 → 拒绝
    bad = await push.submit_public_doc(OWNER, w["priv_id"], w["doc_id"], target_space_id=w["priv_id"])
    assert not bad["ok"]

    r = await push.submit_public_doc(OWNER, w["priv_id"], w["doc_id"], target_space_id=w["pub_id"])
    assert r["ok"]
    async with env() as db:
        sub = (await db.execute(select(KbPublicSubmission))).scalars().first()
        assert sub.status == "ai_reviewing"
        assert sub.doc_title == "FastAPI 部署指南"
        assert "部署" in sub.doc_content  # 快照冻结
        assert sub.target_space_id == w["pub_id"]
        # 分析任务已入队
        jobs = list((await db.execute(select(KbJob))).scalars().all())
        assert any(j.job_type == "public_submission" for j in jobs)

        # 幂等：同内容在途单拒绝
    dup = await push.submit_public_doc(OWNER, w["priv_id"], w["doc_id"])
    assert not dup["ok"] and "在途" in dup["msg"]

    # 源文档删除后快照仍可审
    async with env() as db:
        doc = await db.get(KbDocSource, w["doc_id"])
        await db.delete(doc)
        await db.commit()
    r2 = await push.submit_public_doc(OWNER, w["priv_id"], 999)  # 源已删：不存在
    assert not r2["ok"]


# ---------------------------------------------------------------- AI 分析分流

async def _analyze_with(monkeypatch, env, response: str | None) -> str:
    w = await _setup_world(env)
    await push.submit_public_doc(OWNER, w["priv_id"], w["doc_id"])
    async with env() as db:
        sub = (await db.execute(select(KbPublicSubmission))).scalars().first()
        sid = sub.id

    async def fake_llm(system: str, user: str, max_tokens: int, temperature: float):
        return response

    monkeypatch.setattr(memory_pipeline, "_call_llm_json", fake_llm)
    await push.analyze_submission(sid)
    async with env() as db:
        sub = (await db.execute(select(KbPublicSubmission))).scalars().first()
        return sub.status


@pytest.mark.asyncio
async def test_analyze_normal_goes_pending(monkeypatch, env):
    resp = '{"category_id": 1, "category_reason": "技术文档", "risk_type": "none", "risk_level": "none", "split_titles": ["FastAPI"]}'
    assert await _analyze_with(monkeypatch, env, resp) == "pending"


@pytest.mark.asyncio
async def test_analyze_violation_flagged(monkeypatch, env):
    resp = '{"category_id": null, "category_reason": "", "risk_type": "porn", "risk_level": "high", "split_titles": []}'
    assert await _analyze_with(monkeypatch, env, resp) == "violation"


@pytest.mark.asyncio
async def test_analyze_failure_falls_to_pending(monkeypatch, env):
    assert await _analyze_with(monkeypatch, env, None) == "pending"


# ---------------------------------------------------------------- 审核

@pytest.mark.asyncio
async def test_review_approve_copies_doc_to_public(env):
    w = await _setup_world(env)
    await push.submit_public_doc(OWNER, w["priv_id"], w["doc_id"], target_space_id=w["pub_id"])
    async with env() as db:
        sub = (await db.execute(select(KbPublicSubmission))).scalars().first()
        sid = sub.id

    async with env() as db:
        sub = await db.get(KbPublicSubmission, sid)
        sub.status = "pending"  # AI 分析完成后的正常态
        await db.commit()

    # 自审制衡：提交人不能审自己的
    self_review = await push.review_submission(OWNER, sid, True)
    assert not self_review["ok"] and "自己" in self_review["msg"]

    # 审核员通过并指定分类
    r = await push.review_submission(REVIEWER, sid, True, category_id=w["cat_id"])
    assert r["ok"] and r["status"] == "approved"

    async with env() as db:
        sub = await db.get(KbPublicSubmission, sid)
        assert sub.status == "approved" and sub.reviewer_id == REVIEWER
        # 文档拷入公开库
        docs = list((await db.execute(
            select(KbDocSource).where(KbDocSource.space_id == w["pub_id"])
        )).scalars().all())
        assert len(docs) == 1 and docs[0].source_type == "pushed"
        assert docs[0].title == "FastAPI 部署指南"
        # 编译任务入队
        jobs = list((await db.execute(select(KbJob))).scalars().all())
        assert any(j.job_type == "wiki_ingest" and j.space_id == w["pub_id"] for j in jobs)
        # 分类挂到目标库
        pub = await db.get(KbSpace, w["pub_id"])
        assert pub.category_id == w["cat_id"]

    # 已审单不能重复审
    again = await push.review_submission(REVIEWER, sid, True)
    assert not again["ok"]


@pytest.mark.asyncio
async def test_review_reject(env):
    w = await _setup_world(env)
    await push.submit_public_doc(OWNER, w["priv_id"], w["doc_id"])
    async with env() as db:
        sub = (await db.execute(select(KbPublicSubmission))).scalars().first()
        sub.status = "pending"  # AI 分析完成后的正常态
        await db.commit()
        sid = sub.id
    r = await push.review_submission(REVIEWER, sid, False, comment="内容不适合公开")
    assert r["ok"] and r["status"] == "rejected"
    async with env() as db:
        # 拒绝不入公开库
        docs = list((await db.execute(
            select(KbDocSource).where(KbDocSource.space_id == w["pub_id"])
        )).scalars().all())
        assert docs == []


# ---------------------------------------------------------------- 违规处置

@pytest.mark.asyncio
async def test_dispose_violation(env):
    w = await _setup_world(env)
    await push.submit_public_doc(OWNER, w["priv_id"], w["doc_id"])
    async with env() as db:
        sub = (await db.execute(select(KbPublicSubmission))).scalars().first()
        sub.status = "violation"  # 模拟 AI 判违规
        await db.commit()
        sid = sub.id

    # 仅 violation 单可处置
    assert not (await push.dispose_violation(REVIEWER, sid, "hack"))["ok"]

    # 保留存证：内容保留
    r = await push.dispose_violation(REVIEWER, sid, "kept_evidence")
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
    r2 = await push.dispose_violation(REVIEWER, sid, "deleted")
    assert r2["ok"]
    async with env() as db:
        sub = await db.get(KbPublicSubmission, sid)
        assert sub.status == "disposed" and sub.disposal_action == "deleted"
        assert sub.doc_content == ""
        assert sub.id == sid  # 记录行仍在
