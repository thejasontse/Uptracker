"""Asyncio scheduler.

Every few seconds the loop finds enabled datasets whose ``next_run_at`` is due
(or null) and runs them in a bounded threadpool (collection is blocking:
subprocess/socket/HTTP). Concurrency is capped so a fleet of hung scripts
cannot exhaust the host. The scheduler can be toggled via the
``scheduler_enabled`` setting.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import time
from datetime import datetime, timezone

from . import collector, db

_executor = concurrent.futures.ThreadPoolExecutor(max_workers=8, thread_name_prefix="collect")
_task: asyncio.Task | None = None
_TICK_S = 5.0


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _due_datasets(conn) -> list[int]:
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    rows = conn.execute(
        """SELECT id FROM datasets
           WHERE enabled=1 AND (next_run_at IS NULL OR next_run_at <= ?)
           ORDER BY next_run_at NULLS FIRST LIMIT 32""",
        (now,),
    ).fetchall()
    return [r["id"] for r in rows]


def _claim(conn, dataset_id: int) -> bool:
    """Atomically push next_run_at forward so the same dataset isn't double-run."""
    row = conn.execute("SELECT interval_s FROM datasets WHERE id=? AND enabled=1", (dataset_id,)).fetchone()
    if not row:
        return False
    nxt = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + max(1, row["interval_s"])))
    cur = conn.execute(
        "UPDATE datasets SET next_run_at=? WHERE id=? AND enabled=1", (nxt, dataset_id),
    )
    return cur.rowcount == 1


async def _run_one(dataset_id: int) -> None:
    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(_executor, _collect, dataset_id)
    except Exception as exc:  # pragma: no cover - defensive
        print(f"[scheduler] dataset {dataset_id} failed: {exc}")


def _collect(dataset_id: int) -> None:
    with db.db_conn() as conn:
        collector.collect_dataset(conn, dataset_id)


async def _loop() -> None:
    while True:
        try:
            ids: list[int] = []
            with db.db_conn() as conn:
                if db.get_setting(conn, "scheduler_enabled", "1") == "1":
                    due = _due_datasets(conn)
                    ids = [ds for ds in due if _claim(conn, ds)]
            for ds_id in ids:
                asyncio.create_task(_run_one(ds_id))
        except Exception as exc:  # pragma: no cover - defensive
            print(f"[scheduler] tick error: {exc}")
        await asyncio.sleep(_TICK_S)


def start() -> None:
    global _task
    if _task is None or _task.done():
        _task = asyncio.create_task(_loop())


async def stop() -> None:
    global _task
    if _task and not _task.done():
        _task.cancel()
        try:
            await _task
        except asyncio.CancelledError:
            pass
    _task = None
