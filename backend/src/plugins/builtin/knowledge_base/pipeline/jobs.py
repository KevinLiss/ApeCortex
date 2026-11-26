"""任务中枢（P1）：kb_job 表 + per-user asyncio worker（仿宿主 ChatRun 模式）。

- enqueue_job：落 pending 行 → spawn/复用该用户的 worker → 立即返回 job_id
- worker：FIFO 认领该用户 pending 任务；running 时增量更新 step/progress
- 失败：status=failed + error 落库，绝不向上抛（聊天/页面请求不受影响）
- 重启兜底：recover_orphan_jobs 把遗留 running 标 failed（同宿主 recover_orphan_runs）
"""
import asyncio
import json
from datetime import UTC, datetime
from typing import Any

from loguru import logger
from sqlalchemy import select, update

from src.db import SessionLocal

TERMINAL = ("done", "failed", "cancelled")

# per-user worker 任务表（user_id → asyncio.Task）
_workers: dict[int, asyncio.Task] = {}
_lock = asyncio.Lock()


async def enqueue_job(
    user_id: int,
    job_type: str,
    payload: dict[str, Any] | None = None,
    space_id: int | None = None,
) -> int:
    """入队一个任务，返回 job_id（页面/MCP 调用后轮询用）。

    成本护栏（M5）：``kb_setting.daily_job_limit``（user_id=0，0=不限）限制单用户当日任务数，
    超限抛 ``RuntimeError``（调用方转 429/友好提示）。
    """
    from datetime import datetime

    from sqlalchemy import func

    from src.plugins.builtin.knowledge_base.models import KbJob, KbSetting

    async with SessionLocal() as db:
        limit_row = (await db.execute(
            select(KbSetting).where(
                KbSetting.key == "daily_job_limit", KbSetting.user_id == 0
            )
        )).scalars().first()
        try:
            daily_limit = int(str(limit_row.value).strip()) if limit_row else 0
        except ValueError:
            daily_limit = 0
        if daily_limit > 0:
            day_start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
            used = (await db.execute(
                select(func.count()).select_from(KbJob).where(
                    KbJob.user_id == user_id,
                    KbJob.created_at >= day_start,
                )
            )).scalar_one()
            if used >= daily_limit:
                raise RuntimeError(f"今日任务已达上限（{used}/{daily_limit}），请明日再试或在设置中调整")

        job = KbJob(
            user_id=user_id,
            space_id=space_id,
            job_type=job_type,
            payload=json.dumps(payload or {}, ensure_ascii=False),
            status="pending",
        )
        db.add(job)
        await db.commit()
        await db.refresh(job)
        job_id = job.id

    await _ensure_worker(user_id)
    return job_id


async def _ensure_worker(user_id: int) -> None:
    async with _lock:
        task = _workers.get(user_id)
        if task is None or task.done():
            _workers[user_id] = asyncio.create_task(_worker_loop(user_id))


async def _worker_loop(user_id: int) -> None:
    """该用户的串行 worker（SQLite 单写者纪律）。"""
    try:
        while True:
            job_id = await _claim_next(user_id)
            if job_id is None:
                break
            await _execute(job_id)
    except Exception as exc:  # noqa: BLE001
        logger.error(f"[KB] worker user={user_id} 异常退出: {exc}")
    finally:
        _workers.pop(user_id, None)


async def _claim_next(user_id: int) -> int | None:
    """认领该用户最早的 pending 任务（置 running），无任务返回 None。"""
    from src.plugins.builtin.knowledge_base.models import KbJob

    async with SessionLocal() as db:
        row = (
            await db.execute(
                select(KbJob)
                .where(KbJob.user_id == user_id, KbJob.status == "pending")
                .order_by(KbJob.id.asc())
                .limit(1)
            )
        ).scalars().first()
        if row is None:
            return None
        row.status = "running"
        row.started_at = datetime.now(UTC)
        await db.commit()
        return row.id


async def _update_job(job_id: int, **fields) -> None:
    from src.plugins.builtin.knowledge_base.models import KbJob

    async with SessionLocal() as db:
        job = await db.get(KbJob, job_id)
        if job is None:
            return
        for k, v in fields.items():
            setattr(job, k, v)
        await db.commit()


async def _execute(job_id: int) -> None:
    """执行单个任务（按 job_type 分发；异常统一落 failed）。"""
    from src.plugins.builtin.knowledge_base.models import KbJob

    async with SessionLocal() as db:
        job = await db.get(KbJob, job_id)
        if job is None:
            return
        job_type, payload = job.job_type, json.loads(job.payload or "{}")
        user_id, space_id = job.user_id, job.space_id
    try:
        result: dict = {}
        if job_type == "wiki_ingest":
            from src.plugins.builtin.knowledge_base.pipeline.compiler import compile_space
            await _update_job(job_id, step="compiling", progress=10)
            doc_ids = payload.get("doc_ids")
            result = await compile_space(space_id, doc_ids, category_id=payload.get("category_id"))
            if result.get("llm_calls"):
                await _bump_llm_calls(job_id, result["llm_calls"])
        elif job_type == "research":
            from src.plugins.builtin.knowledge_base.pipeline.research import run_research

            async def _cb(step: str, pct: int) -> None:
                await _update_job(job_id, step=step, progress=pct)

            result = await run_research(
                user_id=user_id,
                space_id=payload.get("space_id") or 0,
                topic=payload.get("topic", ""),
                progress_cb=_cb,
            )
            if result.get("llm_calls"):
                await _bump_llm_calls(job_id, result["llm_calls"])
            result.pop("outline", None)  # 大纲不进 result（体积）
        elif job_type == "memory_extract":
            from src.plugins.builtin.knowledge_base.pipeline.memory import extract_and_store
            inserted = await extract_and_store(
                user_id=user_id,
                session_id=payload.get("session_id", 0),
                transcript_override=payload.get("transcript"),
            )
            result = {"inserted": inserted}
        elif job_type == "public_submission":
            from src.plugins.builtin.knowledge_base.pipeline.submission import analyze_submission
            result = await analyze_submission(payload.get("submission_id", 0))
            await _bump_llm_calls(job_id, 1)
        else:
            raise ValueError(f"未知任务类型: {job_type}")

        await _update_job(
            job_id, status="done", progress=100, step="",
            result=json.dumps(result, ensure_ascii=False),
            finished_at=datetime.now(UTC),
        )
        logger.info(f"[KB] job {job_id} done: {result}")
    except Exception as exc:  # noqa: BLE001
        await _update_job(
            job_id, status="failed", error=str(exc)[:2000],
            finished_at=datetime.now(UTC),
        )
        logger.error(f"[KB] job {job_id} failed: {exc}")


async def _bump_llm_calls(job_id: int, calls: int) -> None:
    from src.plugins.builtin.knowledge_base.models import KbJob

    async with SessionLocal() as db:
        job = await db.get(KbJob, job_id)
        if job is not None:
            job.llm_calls += calls
            await db.commit()


async def recover_orphan_jobs() -> int:
    """启动兜底：遗留 running 的任务标 failed（进程内 worker 已不存在）。"""
    from src.plugins.builtin.knowledge_base.models import KbJob

    # 首次安装/表未建时跳过（on_load 先于 install 的竞态保护）
    async with SessionLocal() as db:
        try:
            await db.execute(select(KbJob).limit(1))
        except Exception:
            return 0

    async with SessionLocal() as db:
        result = await db.execute(
            update(KbJob)
            .where(KbJob.status == "running")
            .values(
                status="failed",
                error="服务重启，任务中断，请重试",
                finished_at=datetime.now(UTC),
            )
        )
        await db.commit()
        count = getattr(result, "rowcount", 0) or 0
        if count:
            logger.warning(f"[KB] 重启恢复：{count} 个孤儿任务标 failed")
        return count


async def list_jobs(user_id: int, page: int = 1, page_size: int = 20) -> dict:
    """用户任务列表（页面轮询）。"""
    from sqlalchemy import func

    from src.plugins.builtin.knowledge_base.models import KbJob

    async with SessionLocal() as db:
        cond = KbJob.user_id == user_id
        total = (
            await db.execute(select(func.count()).select_from(KbJob).where(cond))
        ).scalar_one()
        rows = list((
            await db.execute(
                select(KbJob).where(cond).order_by(KbJob.id.desc())
                .offset((page - 1) * page_size).limit(page_size)
            )
        ).scalars().all())
        return {
            "total": total,
            "items": [
                {
                    "id": r.id, "job_type": r.job_type, "status": r.status,
                    "step": r.step, "progress": r.progress, "space_id": r.space_id,
                    "result": json.loads(r.result or "{}"),
                    "llm_calls": r.llm_calls, "error": r.error,
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                    "finished_at": r.finished_at.isoformat() if r.finished_at else None,
                }
                for r in rows
            ],
        }


async def get_job(job_id: int, user_id: int) -> dict | None:
    """单任务详情（行级隔离）。"""
    from src.plugins.builtin.knowledge_base.models import KbJob

    async with SessionLocal() as db:
        job = await db.get(KbJob, job_id)
        if job is None or job.user_id != user_id:
            return None
        return {
            "id": job.id, "job_type": job.job_type, "status": job.status,
            "step": job.step, "progress": job.progress, "space_id": job.space_id,
            "result": json.loads(job.result or "{}"),
            "llm_calls": job.llm_calls, "error": job.error,
        }
