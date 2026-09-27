from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from .audit import make_entry, utc_now
from .domain import ConflictError, NotFoundError, ValidationError
from .rules import (ASSESSMENT_KIND, ID_PREFIX, PLAN_APPROVED,
                    PLAN_INVALIDATED, PLAN_PENDING, REGRESS_ON_INVALIDATE,
                    STATES, design_gate_reason, record_change_reason,
                    supersede_reason)


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

    def _create_schema(self) -> None:
        statuses = ",".join("'" + s.replace("'", "''") + "'" for s in STATES)
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
                    external_ref TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(item_id, external_ref)
                );
                CREATE TABLE IF NOT EXISTS plans (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    version INTEGER NOT NULL,
                    content TEXT NOT NULL,
                    status TEXT NOT NULL
                        CHECK(status IN ('pending','approved','rejected','invalidated')),
                    submitted_by TEXT NOT NULL,
                    submitted_at TEXT NOT NULL,
                    reviewed_by TEXT,
                    reviewed_at TEXT,
                    review_comment TEXT,
                    reject_reason TEXT,
                    invalid_reason TEXT,
                    UNIQUE(item_id, version)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ux_plans_one_pending
                    ON plans(item_id) WHERE status='pending';
                CREATE TABLE IF NOT EXISTS plan_basis (
                    plan_id INTEGER NOT NULL REFERENCES plans(id) ON DELETE CASCADE,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    PRIMARY KEY(plan_id, record_id)
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
            current = self.conn.execute(
                "SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
            if current is None:
                raise NotFoundError("项目不存在")
            if target == "design":
                reason = design_gate_reason(self.get_latest_plan(item_id))
                if reason:
                    raise ConflictError(reason)
            cur = self.conn.execute(
                """UPDATE items SET status=?, version=version+1, updated_at=?
                   WHERE id=? AND version=?""",
                (target, now, item_id, expected_version),
            )
            if cur.rowcount == 0:
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
                invalidated = self._invalidate_approved_locked(
                    item_id, record_change_reason(kind, "add"))
                row = self.conn.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        except sqlite3.IntegrityError as exc:
            raise ConflictError("记录唯一标识已存在") from exc
        return dict(row), invalidated

    def update_record(self, record_id: int, kind: str, detail: str, status: str,
                      external_ref: Optional[str], actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            row = self.conn.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                raise NotFoundError("评估事项不存在")
            self.conn.execute(
                """UPDATE records SET kind=?, detail=?, status=?, external_ref=?
                   WHERE id=?""",
                (kind, detail, status, external_ref, record_id),
            )
            invalidated = self._invalidate_approved_locked(
                row["item_id"], record_change_reason(kind, "update"))
            row = self.conn.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        return dict(row), invalidated

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

    # ---- 加固方案 ----

    def _invalidate_approved_locked(self, item_id: int,
                                    reason: Optional[str]) -> Optional[Dict[str, Any]]:
        """同一事务内让已同意方案失效；设计阶段退回评估，施工及以后不倒退。"""
        if not reason:
            return None
        row = self.conn.execute(
            "SELECT * FROM plans WHERE item_id=? AND status=?",
            (item_id, PLAN_APPROVED),
        ).fetchone()
        if row is None:
            return None
        self.conn.execute(
            "UPDATE plans SET status=?, invalid_reason=? WHERE id=?",
            (PLAN_INVALIDATED, reason, row["id"]),
        )
        item = self.conn.execute("SELECT status FROM items WHERE id=?", (item_id,)).fetchone()
        regressed = item["status"] in REGRESS_ON_INVALIDATE
        if regressed:
            self.conn.execute(
                """UPDATE items SET status='assessed', version=version+1, updated_at=?
                   WHERE id=?""",
                (utc_now(), item_id),
            )
        result = dict(row)
        result["status"] = PLAN_INVALIDATED
        result["invalid_reason"] = reason
        result["regressed"] = regressed
        return result

    def create_plan(self, item_id: int, content: str, basis_ids: list,
                    actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            item = self.conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
            if item is None:
                raise NotFoundError("项目不存在")
            if item["status"] not in ("assessed", "design", "construction"):
                raise ConflictError("当前项目状态不允许提交加固方案")
            for record_id in basis_ids:
                row = self.conn.execute(
                    "SELECT * FROM records WHERE id=? AND item_id=?",
                    (record_id, item_id),
                ).fetchone()
                if row is None:
                    raise NotFoundError(f"依据事项{record_id}不存在")
                if row["kind"] != ASSESSMENT_KIND:
                    raise ValidationError(f"事项{record_id}不是评估事项，不能作为方案依据")
                if row["status"] != "closed":
                    raise ConflictError(f"依据事项{record_id}尚未关闭，不能提交方案")
            pending = self.conn.execute(
                "SELECT 1 FROM plans WHERE item_id=? AND status=?",
                (item_id, PLAN_PENDING),
            ).fetchone()
            if pending is not None:
                raise ConflictError("同一项目只能保留一份待复核方案")
            ver_row = self.conn.execute(
                "SELECT COALESCE(MAX(version),0) AS v FROM plans WHERE item_id=?",
                (item_id,),
            ).fetchone()
            version = int(ver_row["v"]) + 1
            superseded = self._invalidate_approved_locked(
                item_id, supersede_reason(version))
            cur = self.conn.execute(
                """INSERT INTO plans(item_id, version, content, status, submitted_by,
                   submitted_at) VALUES(?,?,?,?,?,?)""",
                (item_id, version, content, PLAN_PENDING, actor, now),
            )
            plan_id = int(cur.lastrowid)
            self.conn.executemany(
                "INSERT INTO plan_basis(plan_id, record_id) VALUES(?,?)",
                [(plan_id, rid) for rid in basis_ids],
            )
        return self.get_plan(plan_id), superseded

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
                "SELECT * FROM plans WHERE item_id=? ORDER BY version", (item_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def get_latest_plan(self, item_id: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM plans WHERE item_id=? ORDER BY version DESC LIMIT 1",
                (item_id,),
            ).fetchone()
        return dict(row) if row else None

    def plan_basis_ids(self, plan_id: int) -> List[int]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT record_id FROM plan_basis WHERE plan_id=? ORDER BY record_id",
                (plan_id,),
            ).fetchall()
        return [int(r["record_id"]) for r in rows]

    def review_plan(self, plan_id: int, approved: bool, comment: Optional[str],
                    reject_reason: Optional[str], expected_version: int,
                    actor: str) -> Dict[str, Any]:
        now = utc_now()
        status = "approved" if approved else "rejected"
        with self._lock, self.conn:
            row = self.conn.execute("SELECT * FROM plans WHERE id=?", (plan_id,)).fetchone()
            if row is None:
                raise NotFoundError("加固方案不存在")
            if row["status"] != PLAN_PENDING:
                raise ConflictError("仅待复核方案可评审，当前状态：" + row["status"])
            if row["version"] != expected_version:
                raise ConflictError("版本冲突，请刷新后重试")
            cur = self.conn.execute(
                """UPDATE plans SET status=?, reviewed_by=?, reviewed_at=?,
                   review_comment=?, reject_reason=? WHERE id=? AND status=?""",
                (status, actor, now, comment, reject_reason, plan_id, PLAN_PENDING),
            )
            if cur.rowcount == 0:
                raise ConflictError("方案状态已变化，请刷新后重试")
        return self.get_plan(plan_id)

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
