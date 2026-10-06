from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from .audit import make_entry, utc_now
from .domain import (CapacityConflictError, ConflictError, DispatchWriteError,
                     NotFoundError)
from .rules import (BASIS_KINDS, BASIS_STATUSES, BATCH_STATUSES, ID_PREFIX,
                    INSTRUCTION_STATUSES, OCCUPYING_STATUSES, STATES,
                    board_total_for, remaining_capacity)


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
        basis_kinds = ",".join("'" + k.replace("'", "''") + "'" for k in BASIS_KINDS)
        instr_statuses = ",".join("'" + s.replace("'", "''") + "'" for s in INSTRUCTION_STATUSES)
        basis_statuses = ",".join("'" + s.replace("'", "''") + "'" for s in BASIS_STATUSES)
        batch_statuses = ",".join("'" + s.replace("'", "''") + "'" for s in BATCH_STATUSES)
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
                CREATE TABLE IF NOT EXISTS facilities (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    code TEXT NOT NULL UNIQUE,
                    name TEXT NOT NULL,
                    baseline_capacity REAL NOT NULL DEFAULT 0,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS capacity_basis (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    facility_id INTEGER NOT NULL REFERENCES facilities(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL CHECK(kind IN ({basis_kinds})),
                    ref TEXT,
                    conclusion TEXT NOT NULL,
                    capacity REAL NOT NULL DEFAULT 0,
                    effective_from TEXT,
                    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','superseded')),
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_basis_facility ON capacity_basis(facility_id, id DESC);
                CREATE TABLE IF NOT EXISTS capacity_board (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    facility_id INTEGER NOT NULL REFERENCES facilities(id) ON DELETE CASCADE,
                    slot TEXT NOT NULL,
                    total_capacity REAL NOT NULL DEFAULT 0,
                    occupied_capacity REAL NOT NULL DEFAULT 0,
                    basis_status TEXT NOT NULL DEFAULT 'pending_verification'
                        CHECK(basis_status IN ({basis_statuses})),
                    basis_id INTEGER REFERENCES capacity_basis(id) ON DELETE SET NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(facility_id, slot)
                );
                CREATE TABLE IF NOT EXISTS dispatch_batches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_key TEXT NOT NULL UNIQUE,
                    facility_id INTEGER NOT NULL REFERENCES facilities(id) ON DELETE CASCADE,
                    slot TEXT NOT NULL,
                    amount REAL NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'pending'
                        CHECK(status IN ({batch_statuses})),
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS dispatch_instructions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_id INTEGER REFERENCES dispatch_batches(id) ON DELETE SET NULL,
                    facility_id INTEGER NOT NULL REFERENCES facilities(id) ON DELETE CASCADE,
                    slot TEXT NOT NULL,
                    amount REAL NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'planned'
                        CHECK(status IN ({instr_statuses})),
                    basis_status TEXT NOT NULL DEFAULT 'ok'
                        CHECK(basis_status IN ({basis_statuses})),
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_instructions_facility
                    ON dispatch_instructions(facility_id, slot, status);
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

    # ---- 设施 ----
    def create_facility(self, code: str, name: str, baseline_capacity: float,
                        actor: str) -> Dict[str, Any]:
        now = utc_now()
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO facilities(code,name,baseline_capacity,created_by,created_at)
                       VALUES(?,?,?,?,?)""",
                    (code, name, baseline_capacity, actor, now),
                )
                facility_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("设施编码已存在") from exc
        return self.get_facility(facility_id)

    def _get_facility_locked(self, facility_id: int) -> Dict[str, Any]:
        row = self.conn.execute("SELECT * FROM facilities WHERE id=?", (facility_id,)).fetchone()
        if row is None:
            raise NotFoundError("设施不存在")
        return dict(row)

    def get_facility(self, facility_id: int) -> Dict[str, Any]:
        with self._lock:
            return self._get_facility_locked(facility_id)

    def list_facilities(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute("SELECT * FROM facilities ORDER BY id").fetchall()
        return [dict(row) for row in rows]

    def list_facilities_without_basis(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                """SELECT f.* FROM facilities f
                   WHERE NOT EXISTS (
                       SELECT 1 FROM capacity_basis b
                       WHERE b.facility_id=f.id AND b.status='active')
                   ORDER BY f.id""").fetchall()
        return [dict(row) for row in rows]

    # ---- 容量依据（检修/检查/许可/审计，各记一套） ----
    def record_basis(self, facility_id: int, kind: str, ref: Optional[str],
                     conclusion: str, capacity: float, effective_from: Optional[str],
                     actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            self._get_facility_locked(facility_id)
            self.conn.execute(
                "UPDATE capacity_basis SET status='superseded', updated_at=? "
                "WHERE facility_id=? AND kind=? AND status='active'",
                (now, facility_id, kind))
            cur = self.conn.execute(
                """INSERT INTO capacity_basis(facility_id,kind,ref,conclusion,capacity,
                   effective_from,status,created_by,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,'active',?,?,?)""",
                (facility_id, kind, ref, conclusion, capacity, effective_from,
                 actor, now, now),
            )
            basis_id = int(cur.lastrowid)
            self._recalc_all_boards_locked(facility_id, actor)
            row = self.conn.execute("SELECT * FROM capacity_basis WHERE id=?", (basis_id,)).fetchone()
            return dict(row)

    def get_basis(self, basis_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute("SELECT * FROM capacity_basis WHERE id=?", (basis_id,)).fetchone()
        if row is None:
            raise NotFoundError("容量依据不存在")
        return dict(row)

    def list_basis(self, facility_id: int, kind: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._lock:
            self._get_facility_locked(facility_id)
            sql = "SELECT * FROM capacity_basis WHERE facility_id=?"
            params: tuple = (facility_id,)
            if kind:
                sql += " AND kind=?"
                params = (facility_id, kind)
            sql += " ORDER BY id DESC"
            rows = self.conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def _latest_basis_locked(self, facility_id: int) -> Optional[Dict[str, Any]]:
        row = self.conn.execute(
            """SELECT * FROM capacity_basis WHERE facility_id=? AND status='active'
               ORDER BY id DESC LIMIT 1""",
            (facility_id,)).fetchone()
        return dict(row) if row else None

    def update_basis_conclusion(self, basis_id: int, conclusion: str,
                                capacity: Optional[float],
                                actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            row = self.conn.execute("SELECT * FROM capacity_basis WHERE id=?", (basis_id,)).fetchone()
            if row is None:
                raise NotFoundError("容量依据不存在")
            new_capacity = capacity if capacity is not None else row["capacity"]
            self.conn.execute(
                "UPDATE capacity_basis SET conclusion=?, capacity=?, updated_at=? WHERE id=?",
                (conclusion, new_capacity, now, basis_id))
            row = self.conn.execute("SELECT * FROM capacity_basis WHERE id=?", (basis_id,)).fetchone()
            return dict(row)

    # ---- 分时容量台 ----
    def _get_board_by_id_locked(self, board_id: int) -> Dict[str, Any]:
        row = self.conn.execute("SELECT * FROM capacity_board WHERE id=?", (board_id,)).fetchone()
        return dict(row)

    def _get_or_create_board_locked(self, facility_id: int, slot: str,
                                    basis: Optional[Dict[str, Any]],
                                    facility: Dict[str, Any]) -> Dict[str, Any]:
        row = self.conn.execute(
            "SELECT * FROM capacity_board WHERE facility_id=? AND slot=?",
            (facility_id, slot)).fetchone()
        if row is not None:
            return dict(row)
        total, basis_status, basis_id = board_total_for(basis, facility)
        now = utc_now()
        cur = self.conn.execute(
            """INSERT INTO capacity_board(facility_id,slot,total_capacity,occupied_capacity,
               basis_status,basis_id,version,created_at,updated_at)
               VALUES(?,?,?,0,?,?,1,?,?)""",
            (facility_id, slot, total, basis_status, basis_id, now, now),
        )
        return self._get_board_by_id_locked(int(cur.lastrowid))

    def get_board(self, facility_id: int, slot: str) -> Dict[str, Any]:
        with self._lock, self.conn:
            facility = self._get_facility_locked(facility_id)
            basis = self._latest_basis_locked(facility_id)
            board = self._get_or_create_board_locked(facility_id, slot, basis, facility)
            result = dict(board)
            result["remaining"] = remaining_capacity(result["total_capacity"],
                                                     result["occupied_capacity"])
            return result

    def list_boards(self, facility_id: Optional[int] = None) -> List[Dict[str, Any]]:
        with self._lock:
            if facility_id is not None:
                self._get_facility_locked(facility_id)
                rows = self.conn.execute(
                    "SELECT * FROM capacity_board WHERE facility_id=? ORDER BY slot",
                    (facility_id,)).fetchall()
            else:
                rows = self.conn.execute(
                    "SELECT * FROM capacity_board ORDER BY facility_id, slot").fetchall()
        return [dict(row) for row in rows]

    def _recompute_board_occupied_locked(self, facility_id: int, slot: str) -> float:
        placeholders = ",".join("?" * len(OCCUPYING_STATUSES))
        row = self.conn.execute(
            f"""SELECT COALESCE(SUM(amount),0) AS s FROM dispatch_instructions
                WHERE facility_id=? AND slot=? AND status IN ({placeholders})""",
            (facility_id, slot, *OCCUPYING_STATUSES)).fetchone()
        occupied = float(row["s"] or 0)
        now = utc_now()
        self.conn.execute(
            "UPDATE capacity_board SET occupied_capacity=?, version=version+1, updated_at=? "
            "WHERE facility_id=? AND slot=?",
            (occupied, now, facility_id, slot))
        return occupied

    def _recalc_board_total_locked(self, facility_id: int, slot: str, actor: str) -> Dict[str, Any]:
        facility = self._get_facility_locked(facility_id)
        basis = self._latest_basis_locked(facility_id)
        total, basis_status, basis_id = board_total_for(basis, facility)
        now = utc_now()
        self.conn.execute(
            """UPDATE capacity_board SET total_capacity=?, basis_status=?, basis_id=?,
               version=version+1, updated_at=? WHERE facility_id=? AND slot=?""",
            (total, basis_status, basis_id, now, facility_id, slot))
        self._recompute_board_occupied_locked(facility_id, slot)
        row = self.conn.execute(
            "SELECT * FROM capacity_board WHERE facility_id=? AND slot=?",
            (facility_id, slot)).fetchone()
        return dict(row) if row else None

    def _recalc_all_boards_locked(self, facility_id: int, actor: str) -> List[Dict[str, Any]]:
        facility = self._get_facility_locked(facility_id)
        basis = self._latest_basis_locked(facility_id)
        total, basis_status, basis_id = board_total_for(basis, facility)
        now = utc_now()
        rows = self.conn.execute(
            "SELECT * FROM capacity_board WHERE facility_id=?", (facility_id,)).fetchall()
        result = []
        for board in rows:
            self.conn.execute(
                """UPDATE capacity_board SET total_capacity=?, basis_status=?, basis_id=?,
                   version=version+1, updated_at=? WHERE id=?""",
                (total, basis_status, basis_id, now, board["id"]))
            self._recompute_board_occupied_locked(facility_id, board["slot"])
            result.append(self._get_board_by_id_locked(board["id"]))
        return result

    def recalc_all_boards_for_facility(self, facility_id: int, actor: str) -> List[Dict[str, Any]]:
        with self._lock, self.conn:
            return self._recalc_all_boards_locked(facility_id, actor)

    def migrate_pending_verification(self, actor: str) -> int:
        """旧库缺容量依据的设施，其容量台升级为待核验基线。返回更新的台数。"""
        now = utc_now()
        count = 0
        with self._lock, self.conn:
            facilities = self.conn.execute(
                """SELECT f.* FROM facilities f
                   WHERE NOT EXISTS (
                       SELECT 1 FROM capacity_basis b
                       WHERE b.facility_id=f.id AND b.status='active')""").fetchall()
            for facility in facilities:
                boards = self.conn.execute(
                    "SELECT * FROM capacity_board WHERE facility_id=?", (facility["id"],)).fetchall()
                for board in boards:
                    self.conn.execute(
                        """UPDATE capacity_board SET total_capacity=?, basis_status='pending_verification',
                           basis_id=NULL, version=version+1, updated_at=? WHERE id=?""",
                        (facility["baseline_capacity"], now, board["id"]))
                    count += 1
        return count

    # ---- 抢占容量（并发：先到者占用，后到者看剩余） ----
    def seize_capacity(self, facility_id: int, slot: str, amount: float,
                       actor: str) -> Dict[str, Any]:
        with self._lock, self.conn:
            facility = self._get_facility_locked(facility_id)
            basis = self._latest_basis_locked(facility_id)
            board = self._get_or_create_board_locked(facility_id, slot, basis, facility)
            total = float(board["total_capacity"])
            occupied = float(board["occupied_capacity"])
            if occupied + amount > total + 1e-9:
                remaining = remaining_capacity(total, occupied)
                raise CapacityConflictError(
                    "该时段剩余容量不足，无法抢占",
                    remaining=remaining, total=total, slot=slot)
            now = utc_now()
            self.conn.execute(
                """INSERT INTO dispatch_instructions(batch_id,facility_id,slot,amount,
                   status,basis_status,created_by,created_at,updated_at)
                   VALUES(NULL,?,?,?, 'planned','ok', ?,?,?)""",
                (facility_id, slot, amount, actor, now, now),
            )
            self._recompute_board_occupied_locked(facility_id, slot)
            board = self._get_board_by_id_locked(board["id"])
            result = dict(board)
            result["remaining"] = remaining_capacity(
                float(board["total_capacity"]), float(board["occupied_capacity"]))
            return result

    # ---- 调度批次（写入失败按批次恢复，重提不重复占容量） ----
    def _find_batch_locked(self, batch_key: str) -> Optional[Dict[str, Any]]:
        row = self.conn.execute(
            "SELECT * FROM dispatch_batches WHERE batch_key=?", (batch_key,)).fetchone()
        return dict(row) if row else None

    def _create_batch_header_locked(self, batch_key: str, facility_id: int, slot: str,
                                    amount: float, actor: str) -> Dict[str, Any]:
        now = utc_now()
        try:
            cur = self.conn.execute(
                """INSERT INTO dispatch_batches(batch_key,facility_id,slot,amount,status,
                   created_by,created_at,updated_at)
                   VALUES(?,?,?,?, 'pending', ?,?,?)""",
                (batch_key, facility_id, slot, amount, actor, now, now),
            )
            self.conn.commit()
        except sqlite3.IntegrityError as exc:
            raise ConflictError("批次标识已存在") from exc
        return self._find_batch_locked(batch_key)

    def _recover_batch_locked(self, batch: Dict[str, Any], actor: str) -> Dict[str, Any]:
        with self.conn:
            facility = self._get_facility_locked(batch["facility_id"])
            basis = self._latest_basis_locked(batch["facility_id"])
            board = self._get_or_create_board_locked(
                batch["facility_id"], batch["slot"], basis, facility)
            total = float(board["total_capacity"])
            occupied = float(board["occupied_capacity"])
            amount = float(batch["amount"])
            if occupied + amount > total + 1e-9:
                remaining = remaining_capacity(total, occupied)
                raise CapacityConflictError(
                    "该时段剩余容量不足，无法恢复批次",
                    remaining=remaining, total=total, slot=batch["slot"])
            existing = self.conn.execute(
                "SELECT id FROM dispatch_instructions WHERE batch_id=?", (batch["id"],)).fetchone()
            if existing is None:
                now = utc_now()
                self.conn.execute(
                    """INSERT INTO dispatch_instructions(batch_id,facility_id,slot,amount,
                       status,basis_status,created_by,created_at,updated_at)
                       VALUES(?,?,?,?, 'issued','ok', ?,?,?)""",
                    (batch["id"], batch["facility_id"], batch["slot"], amount,
                     actor, now, now),
                )
                self._recompute_board_occupied_locked(batch["facility_id"], batch["slot"])
            now = utc_now()
            self.conn.execute(
                "UPDATE dispatch_batches SET status='committed', updated_at=? WHERE id=?",
                (now, batch["id"]))
        return self.get_batch(batch["batch_key"])

    def submit_batch(self, batch_key: str, facility_id: int, slot: str, amount: float,
                     actor: str, simulate_failure: bool = False) -> Dict[str, Any]:
        with self._lock:
            self._get_facility_locked(facility_id)
            existing = self._find_batch_locked(batch_key)
            if existing is not None:
                if existing["status"] == "committed":
                    return dict(existing)
                return self._recover_batch_locked(existing, actor)
            batch = self._create_batch_header_locked(
                batch_key, facility_id, slot, amount, actor)
            if simulate_failure:
                raise DispatchWriteError("调度写入失败，批次%s待恢复" % batch_key)
            return self._recover_batch_locked(batch, actor)

    def get_batch(self, batch_key: str) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM dispatch_batches WHERE batch_key=?", (batch_key,)).fetchone()
        if row is None:
            raise NotFoundError("调度批次不存在")
        return dict(row)

    def recover_batch(self, batch_key: str, actor: str) -> Dict[str, Any]:
        with self._lock:
            batch = self._find_batch_locked(batch_key)
            if batch is None:
                raise NotFoundError("调度批次不存在")
            if batch["status"] == "committed":
                return dict(batch)
            return self._recover_batch_locked(batch, actor)

    # ---- 调度指令 ----
    def list_instructions(self, facility_id: int,
                          status: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._lock:
            self._get_facility_locked(facility_id)
            sql = "SELECT * FROM dispatch_instructions WHERE facility_id=?"
            params: tuple = (facility_id,)
            if status:
                sql += " AND status=?"
                params = (facility_id, status)
            sql += " ORDER BY id"
            rows = self.conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def recalc_instruction_amount(self, instruction_id: int, amount: float,
                                  actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT * FROM dispatch_instructions WHERE id=?", (instruction_id,)).fetchone()
            if row is None:
                raise NotFoundError("调度指令不存在")
            self.conn.execute(
                "UPDATE dispatch_instructions SET amount=?, updated_at=? WHERE id=?",
                (amount, now, instruction_id))
            self._recompute_board_occupied_locked(row["facility_id"], row["slot"])
            row = self.conn.execute(
                "SELECT * FROM dispatch_instructions WHERE id=?", (instruction_id,)).fetchone()
            return dict(row)

    def mark_instruction_basis_status(self, instruction_id: int, basis_status: str,
                                       actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT * FROM dispatch_instructions WHERE id=?", (instruction_id,)).fetchone()
            if row is None:
                raise NotFoundError("调度指令不存在")
            self.conn.execute(
                "UPDATE dispatch_instructions SET basis_status=?, updated_at=? WHERE id=?",
                (basis_status, now, instruction_id))
            row = self.conn.execute(
                "SELECT * FROM dispatch_instructions WHERE id=?", (instruction_id,)).fetchone()
            return dict(row)

    def close(self) -> None:
        with self._lock:
            self.conn.close()
