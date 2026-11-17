"""SQLite 持久任务、逐文件操作回执和 Telegram 更新去重。"""

import json
import os
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from enum import Enum

ACTIVE = {"queued", "inspecting", "saving", "confirming", "saved", "planning", "executing", "uploading"}


def encode(value):
    return json.dumps(value, ensure_ascii=False, default=lambda x: x.value if isinstance(x, Enum) else str(x))


class Database:
    """每个事务独立连接，持久副作用边界不跨网络等待。"""

    def __init__(self, path):
        self.path = path
        self.lock = threading.RLock()
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, kind TEXT NOT NULL, state TEXT NOT NULL,
                    payload TEXT NOT NULL, result TEXT NOT NULL DEFAULT '{}', message TEXT NOT NULL DEFAULT '',
                    dedupe TEXT UNIQUE, recipient TEXT, created REAL NOT NULL, updated REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS operations (
                    id INTEGER PRIMARY KEY, job_id TEXT NOT NULL, source TEXT NOT NULL,
                    target TEXT NOT NULL, state TEXT NOT NULL, detail TEXT NOT NULL,
                    UNIQUE(job_id,source,target));
                CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS updates (id INTEGER PRIMARY KEY);
                CREATE TABLE IF NOT EXISTS notifications (
                    id INTEGER PRIMARY KEY, dedupe TEXT UNIQUE, recipient TEXT NOT NULL, text TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0, next_try REAL NOT NULL DEFAULT 0, sent INTEGER NOT NULL DEFAULT 0);
            """)
        os.chmod(self.path, 0o600)

    @contextmanager
    def connect(self):
        with self.lock:
            db = sqlite3.connect(self.path, timeout=15)
            db.row_factory = sqlite3.Row
            try:
                with db:
                    yield db
            finally:
                db.close()

    def create(self, kind, payload, dedupe=None, recipient=None, job_id=None, state="queued"):
        with self.connect() as db:
            if dedupe:
                existing = db.execute("SELECT id,state FROM jobs WHERE dedupe=?", (dedupe,)).fetchone()
                if existing:
                    return {**dict(existing), "duplicate": True}
            running = db.execute(
                "SELECT count(*) FROM jobs WHERE state IN ('queued','inspecting','saving','confirming','saved','planning','executing','uploading')"
            ).fetchone()[0]
            if running >= 100:
                raise ValueError("待处理任务已达 100 个")
            now = time.time()
            job_id = job_id or uuid.uuid4().hex[:12]
            db.execute(
                "INSERT INTO jobs(id,kind,state,payload,dedupe,recipient,created,updated) VALUES(?,?,?,?,?,?,?,?)",
                (job_id, kind, state, encode(payload), dedupe, encode(recipient) if recipient else None, now, now),
            )
            return {"id": job_id, "state": state, "duplicate": False}

    def get(self, job_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            raise ValueError("任务不存在")
        result = dict(row)
        for key in ("payload", "result", "recipient"):
            result[key] = json.loads(result[key]) if result[key] else None
        return result

    def list(self, limit=100):
        """轮询列表只读取摘要；完整识别资料与逐文件计划在详情请求中读取。"""
        with self.connect() as db:
            rows = db.execute(
                "SELECT id,kind,state,message,created,updated,payload FROM jobs ORDER BY created DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self.public(dict(row) | {"payload": json.loads(row["payload"]), "result": {}}) for row in rows]

    @staticmethod
    def public(job):
        payload = job["payload"] or {}
        result = dict(job["result"] or {})
        # 完整 TMDB 详情只在内部计划中保存，前端只读取可审核的路径和识别结果。
        if "plans" in result:
            result["plans"] = [
                {k: v for k, v in plan.items() if k not in {"tmdb", "identity"}} for plan in result["plans"]
            ]
        return {key: job[key] for key in ("id", "kind", "state", "message", "created", "updated")} | {
            "source": payload.get("source") or payload.get("staging_dir") or "",
            "target": payload.get("target") or "",
            "result": result,
        }

    def set(self, job_id, state, message, result=None, payload=None):
        with self.connect() as db:
            db.execute("UPDATE jobs SET state=?,message=?,updated=? WHERE id=?", (state, message, time.time(), job_id))
            if result is not None:
                db.execute("UPDATE jobs SET result=? WHERE id=?", (encode(result), job_id))
            if payload is not None:
                db.execute("UPDATE jobs SET payload=? WHERE id=?", (encode(payload), job_id))

    def next(self):
        with self.connect() as db:
            row = db.execute(
                "SELECT id FROM jobs WHERE state IN ('queued','saved') ORDER BY created LIMIT 1"
            ).fetchone()
        return self.get(row[0]) if row else None

    def recover(self):
        """只重排无副作用的规划；写入中的任务须人工复核，禁止盲目重放。"""
        with self.connect() as db:
            db.execute("UPDATE jobs SET state='queued' WHERE state IN ('inspecting','planning')")
            db.execute(
                "UPDATE jobs SET state='needs_review',message='服务中断，写入结果需核对；不会自动重放' WHERE state IN ('saving','confirming','executing','uploading')"
            )

    def operation(self, job_id, source, target, state, detail):
        with self.connect() as db:
            db.execute(
                "INSERT INTO operations(job_id,source,target,state,detail) VALUES(?,?,?,?,?) "
                "ON CONFLICT(job_id,source,target) DO UPDATE SET state=excluded.state,detail=excluded.detail",
                (job_id, source, target, state, encode(detail)),
            )

    def operations(self, job_id):
        with self.connect() as db:
            return [
                dict(row) | {"detail": json.loads(row["detail"])}
                for row in db.execute("SELECT * FROM operations WHERE job_id=? ORDER BY id", (job_id,))
            ]

    def accept_update(self, update_id):
        with self.connect() as db:
            try:
                db.execute("INSERT INTO updates(id) VALUES(?)", (update_id,))
                return True
            except sqlite3.IntegrityError:
                return False

    def value(self, key, default=None):
        with self.connect() as db:
            row = db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
            return json.loads(row[0]) if row else default

    def save_value(self, key, value):
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO kv(key,value) VALUES(?,?)", (key, encode(value)))

    def queue_notification(self, dedupe, recipient, text):
        with self.connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO notifications(dedupe,recipient,text) VALUES(?,?,?)",
                (dedupe, str(recipient), text),
            )

    def pending_notifications(self):
        with self.connect() as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM notifications WHERE sent=0 AND attempts<5 AND next_try<=? ORDER BY id LIMIT 3",
                    (time.time(),),
                )
            ]

    def notification_result(self, notification_id, success):
        with self.connect() as db:
            db.execute(
                "UPDATE notifications SET attempts=attempts+1,sent=?,next_try=? WHERE id=?",
                (int(success), time.time() + 60, notification_id),
            )

    def retry_failed_share(self, job_id, code):
        """仅允许在转存写入前失败的任务由用户重发更正提取码。"""
        with self.connect() as db:
            row = db.execute("SELECT kind,state,payload FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row or row["kind"] != "share" or row["state"] != "failed":
                return False
            payload = json.loads(row["payload"])
            if payload.get("saved_verified"):
                return False
            payload["code"] = code
            cursor = db.execute(
                "UPDATE jobs SET state='queued',payload=?,message='已重新接收，等待转存',updated=? WHERE id=? AND state='failed'",
                (encode(payload), time.time(), job_id),
            )
            return cursor.rowcount == 1
