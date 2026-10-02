"""SQLite 表结构与事务访问。"""
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .domain import Conflict, NotFound


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Repository:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 15000")
        return connection

    def _init_schema(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    reference TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    payload TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    updated_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    action TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_records_state ON records(state);
                CREATE INDEX IF NOT EXISTS idx_audit_record ON audit_events(record_id, id);
                CREATE TABLE IF NOT EXISTS service_batches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_id TEXT NOT NULL UNIQUE,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    state TEXT NOT NULL,
                    expected_version INTEGER NOT NULL,
                    total_items INTEGER NOT NULL,
                    applied_items INTEGER NOT NULL DEFAULT 0,
                    applied_keys TEXT NOT NULL,
                    last_error TEXT NOT NULL DEFAULT '',
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_batches_record ON service_batches(record_id);
                """
            )

    @staticmethod
    def _row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        return item

    def create(self, reference: str, state: str, payload: Dict[str, Any], actor_id: str) -> Dict[str, Any]:
        now = _now()
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    "INSERT INTO records(reference,state,version,payload,created_by,updated_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                    (reference, state, 1, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, actor_id, now, now),
                )
                record_id = int(cursor.lastrowid)
                connection.execute(
                    "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                    (record_id, "created", actor_id, 1, json.dumps({"state": state, "basis": {"plan_version": int(payload.get("plan_version", 1)), "service_minutes": payload.get("service_minutes"), "record_version": 1}}, ensure_ascii=False, sort_keys=True), now),
                )
                row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        except sqlite3.IntegrityError as exc:
            raise Conflict("reference已存在") from exc
        return self._row(row)

    def get(self, record_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        if row is None:
            raise NotFound("记录不存在")
        return self._row(row)

    def list_records(self, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._connect() as connection:
            if state:
                rows = connection.execute("SELECT * FROM records WHERE state=? ORDER BY id DESC LIMIT ?", (state, limit)).fetchall()
            else:
                rows = connection.execute("SELECT * FROM records ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [self._row(row) for row in rows]

    def mutate(self, record_id: int, expected_version: int, state: str, payload: Dict[str, Any], actor_id: str, action: str, details: Dict[str, Any]) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT version, payload FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("记录不存在")
            if int(row["version"]) != int(expected_version):
                connection.rollback()
                current_payload = json.loads(row["payload"])
                raise Conflict(
                    "版本冲突，请刷新后重试",
                    {
                        "expected_version": int(row["version"]),
                        "plan_version": int(current_payload.get("plan_version", 1)),
                        "current_service_minutes": current_payload.get("service_minutes"),
                    },
                )
            version = int(expected_version) + 1
            connection.execute(
                "UPDATE records SET state=?,version=?,payload=?,updated_by=?,updated_at=? WHERE id=?",
                (state, version, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, now, record_id),
            )
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, action, actor_id, version, json.dumps(details, ensure_ascii=False, sort_keys=True), now),
            )
            result = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            connection.commit()
        return self._row(result)

    def add_audit(self, record_id: int, actor_id: str, action: str, details: Dict[str, Any]) -> None:
        with self._connect() as connection:
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                raise NotFound("记录不存在")
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, action, actor_id, int(row["version"]), json.dumps(details, ensure_ascii=False, sort_keys=True), _now()),
            )

    def audit_timeline(self, record_id: int) -> List[Dict[str, Any]]:
        self.get(record_id)
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM audit_events WHERE record_id=? ORDER BY id", (record_id,)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["details"] = json.loads(item["details"])
            result.append(item)
        return result

    def stats(self) -> Dict[str, Any]:
        with self._connect() as connection:
            rows = connection.execute("SELECT state, COUNT(*) AS total FROM records GROUP BY state").fetchall()
            plan_rows = connection.execute(
                "SELECT json_extract(payload, '$.plan_version') AS plan_version, COUNT(*) AS total FROM records GROUP BY plan_version"
            ).fetchall()
        states = {str(row["state"]): int(row["total"]) for row in rows}
        plan_versions: Dict[str, int] = {}
        for row in plan_rows:
            plan_versions["v%s" % (int(row["plan_version"]) if row["plan_version"] is not None else 1)] = int(row["total"])
        return {"states": states, "plan_versions": plan_versions}

    def create_batch(self, batch_id: str, record_id: int, expected_version: int, total_items: int, actor_id: str) -> Dict[str, Any]:
        now = _now()
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                cursor = connection.execute(
                    "INSERT INTO service_batches(batch_id,record_id,state,expected_version,total_items,applied_items,applied_keys,created_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (batch_id, record_id, "processing", expected_version, total_items, 0, "[]", actor_id, now, now),
                )
                batch_pk = int(cursor.lastrowid)
                connection.commit()
                row = connection.execute("SELECT * FROM service_batches WHERE id=?", (batch_pk,)).fetchone()
        except sqlite3.IntegrityError as exc:
            raise Conflict("batch_id已存在") from exc
        return self._batch_row(row)

    def get_batch(self, batch_id: str) -> Optional[Dict[str, Any]]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM service_batches WHERE batch_id=?", (batch_id,)).fetchone()
        return self._batch_row(row) if row is not None else None

    def list_batches(self, record_id: int) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM service_batches WHERE record_id=? ORDER BY id", (record_id,)).fetchall()
        return [self._batch_row(row) for row in rows]

    def mark_batch_item(self, batch_pk: int, state: str, applied_items: int, applied_keys: List[str], last_error: str = "") -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE service_batches SET state=?,applied_items=?,applied_keys=?,last_error=?,updated_at=? WHERE id=?",
                (state, applied_items, json.dumps(applied_keys, ensure_ascii=False), last_error, _now(), batch_pk),
            )

    def complete_batch(self, batch_pk: int) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE service_batches SET state='completed', updated_at=? WHERE id=? AND state='processing'",
                (_now(), batch_pk),
            )

    @staticmethod
    def _batch_row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["applied_keys"] = json.loads(item["applied_keys"])
        return item

    def health(self) -> bool:
        try:
            with self._connect() as connection:
                connection.execute("SELECT 1").fetchone()
            return True
        except sqlite3.Error:
            return False
