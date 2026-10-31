"""Task Queue — V4.0 Phase 2.1

Redis-backed job queue พร้อม retry policy
Fallback เป็น threading ถ้าไม่มี Redis

ออกแบบให้ทำงานร่วมกับ Gunicorn workers:
- Redis mode: jobs ถูกเก็บใน Redis list, workers ดึงทำ
- Thread mode: fire-and-forget เหมือนเดิม (backward compatible)

ไม่เก็บข้อมูลผู้ป่วยใน queue (PDPA)

Usage:
    from task_queue import TaskQueue, TaskResult

    queue = TaskQueue(redis_url="redis://...")
    queue.enqueue("process_image", {"msg_id": "abc", "user_hash": "def"})
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable

logger = logging.getLogger(__name__)


# ─── Task Status ──────────────────────────────────────────


class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    RETRYING = "retrying"
    DEAD = "dead"  # หมด retry แล้ว → ส่งเข้า dead-letter


# ─── Task Result ──────────────────────────────────────────


@dataclass
class TaskResult:
    """ผลลัพธ์ของ task."""
    task_id: str
    status: TaskStatus
    result: Any = None
    error: str = ""
    duration_ms: int = 0
    attempts: int = 0


# ─── Retry Policy ─────────────────────────────────────────


@dataclass
class RetryPolicy:
    """นโยบาย retry สำหรับ task ที่ล้มเหลว."""
    max_retries: int = 3
    backoff_base: float = 2.0  # exponential backoff base (seconds)
    max_backoff: float = 60.0  # maximum backoff (seconds)

    def get_delay(self, attempt: int) -> float:
        """คำนวณ delay ก่อน retry ครั้งถัดไป."""
        delay = self.backoff_base ** attempt
        return min(delay, self.max_backoff)


# ─── Default Retry Policies ──────────────────────────────

RETRY_POLICIES: dict[str, RetryPolicy] = {
    "process_image": RetryPolicy(max_retries=2, backoff_base=3.0),
    "process_file": RetryPolicy(max_retries=2, backoff_base=3.0),
    "lab_extraction": RetryPolicy(max_retries=3, backoff_base=2.0),
    "alert_delivery": RetryPolicy(max_retries=5, backoff_base=5.0),
    "drive_backup": RetryPolicy(max_retries=2, backoff_base=10.0),
    "sheets_sync": RetryPolicy(max_retries=2, backoff_base=10.0),
    "default": RetryPolicy(max_retries=3, backoff_base=2.0),
}


# ─── Task Definition ────────────────────────────────────


@dataclass
class Task:
    """คำจำกัดความของ task ที่รอประมวลผล."""
    task_id: str = field(default_factory=lambda: f"TASK-{uuid.uuid4().hex[:8]}")
    task_type: str = ""
    payload: dict = field(default_factory=dict)
    status: TaskStatus = TaskStatus.PENDING
    attempts: int = 0
    max_retries: int = 3
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    error: str = ""
    job_id: str = ""  # V4.0 job_id สำหรับ tracing

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "task_type": self.task_type,
            "payload": self.payload,
            "status": self.status.value,
            "attempts": self.attempts,
            "max_retries": self.max_retries,
            "created_at": self.created_at,
            "error": self.error,
            "job_id": self.job_id,
        }

    @classmethod
    def from_dict(cls, d: dict) -> Task:
        t = cls()
        t.task_id = d.get("task_id", t.task_id)
        t.task_type = d.get("task_type", "")
        t.payload = d.get("payload", {})
        t.status = TaskStatus(d.get("status", "pending"))
        t.attempts = d.get("attempts", 0)
        t.max_retries = d.get("max_retries", 3)
        t.created_at = d.get("created_at", t.created_at)
        t.error = d.get("error", "")
        t.job_id = d.get("job_id", "")
        return t


# ─── Task Queue ──────────────────────────────────────────


class TaskQueue:
    """Redis-backed task queue พร้อม thread fallback.

    ใช้ Redis list เป็น queue + hash เก็บ task status
    ถ้าไม่มี Redis → fallback เป็น threading.Thread
    """

    QUEUE_KEY = "taskq:pending"
    TASK_PREFIX = "taskq:task:"
    STATS_KEY = "taskq:stats"
    DLQ_KEY = "taskq:dead"

    def __init__(self, redis_url: str = ""):
        self._redis = None
        self._handlers: dict[str, Callable] = {}
        self._stats = {
            "enqueued": 0,
            "completed": 0,
            "failed": 0,
            "dead": 0,
        }
        self._mode = "thread"  # "redis" or "thread"

        if redis_url:
            self._connect_redis(redis_url)

    def _connect_redis(self, url: str) -> None:
        try:
            import redis as redis_lib
            client = redis_lib.Redis.from_url(
                url,
                socket_connect_timeout=2,
                socket_timeout=5,
                decode_responses=True,
            )
            client.ping()
            self._redis = client
            self._mode = "redis"
            logger.info("TaskQueue: Redis connected — persistent queue enabled")
        except ImportError:
            logger.warning("TaskQueue: redis package not installed — using thread fallback")
        except Exception as e:
            logger.warning(
                "TaskQueue: Redis connection failed (%s) — using thread fallback",
                type(e).__name__,
            )

    def register_handler(self, task_type: str, handler: Callable) -> None:
        """ลงทะเบียน handler สำหรับ task type."""
        self._handlers[task_type] = handler
        logger.info("TaskQueue: registered handler for '%s'", task_type)

    def enqueue(
        self,
        task_type: str,
        payload: dict,
        job_id: str = "",
    ) -> str:
        """เพิ่ม task เข้า queue.

        Args:
            task_type: ประเภท task (ต้อง register handler ก่อน)
            payload: ข้อมูลสำหรับ handler (ห้ามมี PHI)
            job_id: V4.0 job_id สำหรับ tracing

        Returns:
            task_id
        """
        policy = RETRY_POLICIES.get(task_type, RETRY_POLICIES["default"])

        task = Task(
            task_type=task_type,
            payload=payload,
            max_retries=policy.max_retries,
            job_id=job_id,
        )

        self._stats["enqueued"] += 1

        if self._mode == "redis" and self._redis:
            return self._enqueue_redis(task)
        else:
            return self._enqueue_thread(task)

    def _enqueue_redis(self, task: Task) -> str:
        """เพิ่ม task เข้า Redis queue."""
        try:
            # เก็บ task data
            self._redis.set(
                f"{self.TASK_PREFIX}{task.task_id}",
                json.dumps(task.to_dict(), ensure_ascii=False),
                ex=86400,  # TTL 24 ชั่วโมง
            )
            # เพิ่มเข้า queue
            self._redis.lpush(self.QUEUE_KEY, task.task_id)

            logger.info(
                "TaskQueue: enqueued %s (type=%s, job=%s)",
                task.task_id, task.task_type, task.job_id[:16],
            )

            # ประมวลผลใน thread (ยังไม่มี dedicated worker)
            threading.Thread(
                target=self._process_redis_task,
                args=(task.task_id,),
                daemon=True,
            ).start()

            return task.task_id
        except Exception as e:
            logger.warning(
                "TaskQueue: Redis enqueue failed (%s) — falling back to thread",
                type(e).__name__,
            )
            return self._enqueue_thread(task)

    def _enqueue_thread(self, task: Task) -> str:
        """Fallback: ประมวลผลใน daemon thread."""
        handler = self._handlers.get(task.task_type)
        if not handler:
            logger.warning(
                "TaskQueue: no handler for '%s'", task.task_type,
            )
            return task.task_id

        def _run():
            self._execute_task(task, handler)

        threading.Thread(target=_run, daemon=True).start()
        logger.info(
            "TaskQueue: thread-enqueued %s (type=%s)",
            task.task_id, task.task_type,
        )
        return task.task_id

    def _process_redis_task(self, task_id: str) -> None:
        """ดึง task จาก Redis แล้วประมวลผล."""
        try:
            raw = self._redis.get(f"{self.TASK_PREFIX}{task_id}")
            if not raw:
                return
            task = Task.from_dict(json.loads(raw))
            handler = self._handlers.get(task.task_type)
            if not handler:
                logger.warning("TaskQueue: no handler for '%s'", task.task_type)
                return
            self._execute_task(task, handler)

            # อัปเดตสถานะใน Redis
            self._redis.set(
                f"{self.TASK_PREFIX}{task_id}",
                json.dumps(task.to_dict(), ensure_ascii=False),
                ex=86400,
            )
        except Exception as e:
            logger.error(
                "TaskQueue: process failed for %s: %s",
                task_id, type(e).__name__,
            )

    def _execute_task(self, task: Task, handler: Callable) -> TaskResult:
        """ประมวลผล task พร้อม retry."""
        policy = RETRY_POLICIES.get(task.task_type, RETRY_POLICIES["default"])
        start = time.monotonic()

        while task.attempts <= task.max_retries:
            task.attempts += 1
            task.status = TaskStatus.RUNNING

            try:
                result = handler(task.payload)
                duration_ms = int((time.monotonic() - start) * 1000)
                task.status = TaskStatus.COMPLETED
                self._stats["completed"] += 1

                logger.info(
                    "TaskQueue: %s completed (type=%s, attempts=%d, %dms)",
                    task.task_id, task.task_type, task.attempts, duration_ms,
                )

                return TaskResult(
                    task_id=task.task_id,
                    status=TaskStatus.COMPLETED,
                    result=result,
                    duration_ms=duration_ms,
                    attempts=task.attempts,
                )
            except Exception as e:
                task.error = f"{type(e).__name__}: {str(e)[:100]}"
                logger.warning(
                    "TaskQueue: %s attempt %d failed: %s",
                    task.task_id, task.attempts, task.error,
                )

                if task.attempts <= task.max_retries:
                    delay = policy.get_delay(task.attempts)
                    task.status = TaskStatus.RETRYING
                    logger.info(
                        "TaskQueue: retrying %s in %.1fs (attempt %d/%d)",
                        task.task_id, delay, task.attempts + 1, task.max_retries + 1,
                    )
                    time.sleep(delay)
                else:
                    # หมด retry → dead letter
                    task.status = TaskStatus.DEAD
                    self._stats["dead"] += 1
                    self._stats["failed"] += 1
                    self._send_to_dlq(task)

        duration_ms = int((time.monotonic() - start) * 1000)
        return TaskResult(
            task_id=task.task_id,
            status=TaskStatus.DEAD,
            error=task.error,
            duration_ms=duration_ms,
            attempts=task.attempts,
        )

    def _send_to_dlq(self, task: Task) -> None:
        """ส่ง task ที่หมด retry เข้า dead-letter queue."""
        logger.error(
            "TaskQueue: %s DEAD after %d attempts (type=%s, error=%s)",
            task.task_id, task.attempts, task.task_type, task.error,
        )
        if self._redis:
            try:
                self._redis.lpush(
                    self.DLQ_KEY,
                    json.dumps(task.to_dict(), ensure_ascii=False),
                )
            except Exception:
                pass  # DLQ ล้มเหลวไม่กระทบระบบหลัก

    # ─── Query ───────────────────────────────────────────

    def get_stats(self) -> dict:
        """สถิติ queue."""
        stats = dict(self._stats)
        stats["mode"] = self._mode
        stats["registered_handlers"] = list(self._handlers.keys())
        if self._redis:
            try:
                stats["queue_length"] = self._redis.llen(self.QUEUE_KEY)
                stats["dlq_length"] = self._redis.llen(self.DLQ_KEY)
            except Exception:
                stats["queue_length"] = -1
                stats["dlq_length"] = -1
        return stats

    def get_dlq_items(self, limit: int = 20) -> list[dict]:
        """ดู dead-letter items."""
        if not self._redis:
            return []
        try:
            items = self._redis.lrange(self.DLQ_KEY, 0, limit - 1)
            return [json.loads(item) for item in items]
        except Exception:
            return []

    def retry_dlq_item(self, task_id: str) -> bool:
        """Retry task จาก dead-letter queue."""
        if not self._redis:
            return False
        try:
            items = self._redis.lrange(self.DLQ_KEY, 0, -1)
            for i, item_raw in enumerate(items):
                item = json.loads(item_raw)
                if item.get("task_id") == task_id:
                    self._redis.lrem(self.DLQ_KEY, 1, item_raw)
                    # Re-enqueue
                    self.enqueue(
                        item["task_type"],
                        item.get("payload", {}),
                        job_id=item.get("job_id", ""),
                    )
                    return True
            return False
        except Exception:
            return False
