from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from .audit import make_entry, utc_now
from .domain import ConflictError, NotFoundError
from .rules import (ID_PREFIX, PLAN_INVALIDATE_NEW_VERSION,
                    PLAN_INVALIDATE_RECORD_AMENDED, PLAN_STATES, STATES)


class Repository:
    def __init__(self, db_path: str):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self._create_schema()
        self._migrate()

    def _create_schema(self) -> None:
        statuses = ",".join("'" + s.replace("'", "''") + "'" for s in STATES)
        plan_statuses = ",".join("'" + s.replace("'", "''") + "'" for s in PLAN_STATES)
        with self.conn:
            self.conn.executescript(f"""
                CREATE TABLE IF NOT EXISTS items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    quantity REAL NOT NULL DEFAULT 0,
                    threshold REAL NOT NULL DEFAULT 1,
                    status TEXT NOT NULL CHECK(status IN ({statuses})),
                    version INTEGER NOT NULL DEFAULT 1,
                    external_ref TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ux_items_external_ref
                    ON items(external_ref) WHERE external_ref IS NOT NULL;
                CREATE TABLE IF NOT EXISTS records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open'
                        CHECK(status IN ('open','closed')),
                    version INTEGER NOT NULL DEFAULT 1,
                    external_ref TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT,
                    UNIQUE(item_id, external_ref)
                );
                CREATE TABLE IF NOT EXISTS plans (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    version_no INTEGER NOT NULL,
                    content TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending'
                        CHECK(status IN ({plan_statuses})),
                    review_reason TEXT,
                    reviewed_by TEXT,
                    reviewed_at TEXT,
                    invalidated_reason TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(item_id, version_no)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ux_plans_one_pending
                    ON plans(item_id) WHERE status='pending';
                CREATE TABLE IF NOT EXISTS plan_records (
                    plan_id INTEGER NOT NULL REFERENCES plans(id) ON DELETE CASCADE,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    record_version INTEGER NOT NULL,
                    PRIMARY KEY (plan_id, record_id)
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    action TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    entity_id INTEGER NOT NULL,
                    actor TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    previous_hash TEXT NOT NULL,
                    entry_hash TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL
                );
            """)

    def _migrate(self) -> None:
        columns = {row["name"] for row in
                   self.conn.execute("PRAGMA table_info(records)")}
        with self.conn:
            if "version" not in columns:
                self.conn.execute(
                    "ALTER TABLE records ADD COLUMN version INTEGER NOT NULL DEFAULT 1")
            if "updated_at" not in columns:
                self.conn.execute("ALTER TABLE records ADD COLUMN updated_at TEXT")

    @staticmethod
    def _item(row: sqlite3.Row) -> Dict[str, Any]:
        return dict(row)

    def create_item(self, title: str, description: str, severity: str,
                    quantity: float, threshold: float, external_ref: Optional[str],
                    actor: str) -> Dict[str, Any]:
        now = utc_now()
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO items(title, description, severity, quantity, threshold,
                       status, version, external_ref, created_by, created_at, updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (title, description, severity, quantity, threshold, STATES[0], 1,
                     external_ref, actor, now, now),
                )
                item_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("external_ref已存在") from exc
        return self.get_item(item_id)

    def get_item(self, item_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        if row is None:
            raise NotFoundError("项目不存在")
        return self._item(row)

    def list_items(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM items"
        params: tuple = ()
        if status:
            sql += " WHERE status=?"
            params = (status,)
        sql += " ORDER BY id DESC"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [self._item(row) for row in rows]

    def transition_item(self, item_id: int, target: str, expected_version: int,
                        actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """UPDATE items SET status=?, version=version+1, updated_at=?
                   WHERE id=? AND version=?""",
                (target, now, item_id, expected_version),
            )
            if cur.rowcount == 0:
                exists = self.conn.execute("SELECT 1 FROM items WHERE id=?", (item_id,)).fetchone()
                if exists is None:
                    raise NotFoundError("项目不存在")
                raise ConflictError("版本冲突，请刷新后重试")
        return self.get_item(item_id)

    def add_record(self, item_id: int, kind: str, detail: str, status: str,
                   external_ref: Optional[str], actor: str) -> Dict[str, Any]:
        now = utc_now()
        self.get_item(item_id)
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO records(item_id, kind, detail, status, external_ref,
                       created_by, created_at) VALUES(?,?,?,?,?,?,?)""",
                    (item_id, kind, detail, status, external_ref, actor, now),
                )
                record_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("记录唯一标识已存在") from exc
        with self._lock:
            row = self.conn.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        return dict(row)

    def list_records(self, item_id: int) -> List[Dict[str, Any]]:
        self.get_item(item_id)
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM records WHERE item_id=? ORDER BY id", (item_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def open_record_count(self, item_id: int) -> int:
        with self._lock:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM records WHERE item_id=? AND status='open'",
                (item_id,),
            ).fetchone()
        return int(row["n"])

    def get_record(self, item_id: int, record_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM records WHERE id=? AND item_id=?",
                (record_id, item_id),
            ).fetchone()
        if row is None:
            raise NotFoundError("评估事项不存在")
        return dict(row)

    def get_records_by_ids(self, item_id: int, record_ids: List[int]) -> List[Dict[str, Any]]:
        if not record_ids:
            return []
        marks = ",".join("?" for _ in record_ids)
        with self._lock:
            rows = self.conn.execute(
                f"SELECT * FROM records WHERE item_id=? AND id IN ({marks})",
                (item_id, *record_ids),
            ).fetchall()
        return [dict(row) for row in rows]

    def amend_record(self, item_id: int, record_id: int, detail: str, status: str,
                     actor: str) -> tuple:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """UPDATE records SET detail=?, status=?, version=version+1, updated_at=?
                   WHERE id=? AND item_id=?""",
                (detail, status, now, record_id, item_id),
            )
            if cur.rowcount == 0:
                raise NotFoundError("评估事项不存在")
            rows = self.conn.execute(
                """SELECT id FROM plans WHERE status IN ('pending','approved') AND id IN
                   (SELECT plan_id FROM plan_records WHERE record_id=?)""",
                (record_id,),
            ).fetchall()
            invalidated = [int(row["id"]) for row in rows]
            if invalidated:
                self.conn.execute(
                    """UPDATE plans SET status='invalidated', invalidated_reason=?
                       WHERE status IN ('pending','approved') AND id IN
                       (SELECT plan_id FROM plan_records WHERE record_id=?)""",
                    (PLAN_INVALIDATE_RECORD_AMENDED, record_id),
                )
            row = self.conn.execute(
                "SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        return dict(row), invalidated

    def create_plan(self, item_id: int, content: str, record_refs: List[tuple],
                    actor: str) -> tuple:
        now = utc_now()
        try:
            with self._lock, self.conn:
                rows = self.conn.execute(
                    "SELECT id FROM plans WHERE item_id=? AND status='approved'",
                    (item_id,),
                ).fetchall()
                invalidated = [int(row["id"]) for row in rows]
                if invalidated:
                    self.conn.execute(
                        """UPDATE plans SET status='invalidated', invalidated_reason=?
                           WHERE item_id=? AND status='approved'""",
                        (PLAN_INVALIDATE_NEW_VERSION, item_id),
                    )
                row = self.conn.execute(
                    "SELECT COALESCE(MAX(version_no),0)+1 AS v FROM plans WHERE item_id=?",
                    (item_id,),
                ).fetchone()
                version_no = int(row["v"])
                cur = self.conn.execute(
                    """INSERT INTO plans(item_id, version_no, content, status, created_by,
                       created_at) VALUES(?,?,?,'pending',?,?)""",
                    (item_id, version_no, content, actor, now),
                )
                plan_id = int(cur.lastrowid)
                self.conn.executemany(
                    """INSERT INTO plan_records(plan_id, record_id, record_version)
                       VALUES(?,?,?)""",
                    [(plan_id, record_id, record_version)
                     for record_id, record_version in record_refs],
                )
        except sqlite3.IntegrityError as exc:
            raise ConflictError("该项目已存在待审方案") from exc
        return self.get_plan(plan_id), invalidated

    def get_plan(self, plan_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute("SELECT * FROM plans WHERE id=?", (plan_id,)).fetchone()
        if row is None:
            raise NotFoundError("加固方案不存在")
        return dict(row)

    def list_plans(self, item_id: int) -> List[Dict[str, Any]]:
        self.get_item(item_id)
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM plans WHERE item_id=? ORDER BY version_no DESC",
                (item_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def latest_plan(self, item_id: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self.conn.execute(
                """SELECT * FROM plans WHERE item_id=?
                   ORDER BY version_no DESC LIMIT 1""",
                (item_id,),
            ).fetchone()
        return dict(row) if row else None

    def review_plan(self, plan_id: int, status: str, reason: Optional[str],
                    actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """UPDATE plans SET status=?, review_reason=?, reviewed_by=?, reviewed_at=?
                   WHERE id=? AND status='pending'""",
                (status, reason, actor, now, plan_id),
            )
            if cur.rowcount == 0:
                exists = self.conn.execute(
                    "SELECT 1 FROM plans WHERE id=?", (plan_id,)).fetchone()
                if exists is None:
                    raise NotFoundError("加固方案不存在")
                raise ConflictError("方案当前状态不允许评审")
        return self.get_plan(plan_id)

    def plan_records(self, plan_id: int) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                """SELECT pr.record_id, pr.record_version, r.item_id, r.kind, r.detail,
                          r.status, r.version AS current_version
                   FROM plan_records pr JOIN records r ON r.id=pr.record_id
                   WHERE pr.plan_id=? ORDER BY pr.record_id""",
                (plan_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def plan_stale_record_count(self, plan_id: int) -> int:
        with self._lock:
            row = self.conn.execute(
                """SELECT COUNT(*) AS n FROM plan_records pr
                   JOIN records r ON r.id=pr.record_id
                   WHERE pr.plan_id=? AND (r.version<>pr.record_version
                        OR r.status<>'closed')""",
                (plan_id,),
            ).fetchone()
        return int(row["n"])

    def append_audit(self, action: str, entity_type: str, entity_id: int,
                     actor: str, detail: dict) -> Dict[str, Any]:
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT entry_hash FROM audit_events ORDER BY id DESC LIMIT 1"
            ).fetchone()
            previous = row["entry_hash"] if row else "GENESIS"
            event = make_entry(action, entity_type, entity_id, actor, detail, previous)
            cur = self.conn.execute(
                """INSERT INTO audit_events(action, entity_type, entity_id, actor, detail,
                   previous_hash, entry_hash, created_at) VALUES(?,?,?,?,?,?,?,?)""",
                (event["action"], event["entity_type"], event["entity_id"], event["actor"],
                 json.dumps(event["detail"], ensure_ascii=False, sort_keys=True),
                 event["previous_hash"], event["entry_hash"], event["created_at"]),
            )
            event_id = int(cur.lastrowid)
        event["id"] = event_id
        return event

    def list_audit(self, entity_id: Optional[int] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM audit_events"
        params: tuple = ()
        if entity_id is not None:
            sql += " WHERE entity_id=?"
            params = (entity_id,)
        sql += " ORDER BY id"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["detail"] = json.loads(item["detail"])
            result.append(item)
        return result

    def verify_audit_chain(self) -> bool:
        from .audit import calculate_hash
        with self._lock:
            rows = self.conn.execute("SELECT * FROM audit_events ORDER BY id").fetchall()
        previous = "GENESIS"
        for row in rows:
            if row["previous_hash"] != previous:
                return False
            payload = {
                "action": row["action"], "entity_type": row["entity_type"],
                "entity_id": row["entity_id"], "actor": row["actor"],
                "detail": json.loads(row["detail"]), "created_at": row["created_at"],
            }
            if calculate_hash(previous, payload) != row["entry_hash"]:
                return False
            previous = row["entry_hash"]
        return True

    def close(self) -> None:
        with self._lock:
            self.conn.close()
