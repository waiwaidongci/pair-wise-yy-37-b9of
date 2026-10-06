from __future__ import annotations

import json
import sqlite3
import threading
from typing import Any, Dict, List, Optional

from .audit import utc_now
from .domain import CapacityConflictError, ConflictError, NotFoundError


class LedgerStore:
    """分时容量台存储：设施、四类依据、调度指令、批次和槽位占用。"""

    def __init__(self, conn: sqlite3.Connection, lock: threading.RLock):
        self.conn = conn
        self._lock = lock
        self._create_schema()

    def _create_schema(self) -> None:
        with self._lock, self.conn:
            self.conn.executescript("""
                CREATE TABLE IF NOT EXISTS facilities (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    code TEXT NOT NULL UNIQUE,
                    name TEXT NOT NULL,
                    capacity REAL NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS capacity_bases (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    facility_id INTEGER NOT NULL REFERENCES facilities(id),
                    source_kind TEXT NOT NULL,
                    conclusion TEXT NOT NULL,
                    reduce_amount REAL NOT NULL,
                    slot_start TEXT NOT NULL,
                    slot_end TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'active'
                        CHECK(state IN ('active','superseded','revoked')),
                    supersedes_id INTEGER REFERENCES capacity_bases(id),
                    basis_version INTEGER NOT NULL DEFAULT 1,
                    external_ref TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS dispatch_batches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    client_token TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL DEFAULT 'open'
                        CHECK(status IN ('open','completed','partial','failed')),
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS dispatch_directives (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    facility_id INTEGER NOT NULL REFERENCES facilities(id),
                    slot_start TEXT NOT NULL,
                    reduce_amount REAL NOT NULL,
                    status TEXT NOT NULL
                        CHECK(status IN ('planned','issued','executed','cancelled',
                                        'conflicted','baseline')),
                    basis_snapshot TEXT,
                    batch_id INTEGER REFERENCES dispatch_batches(id),
                    pending_basis INTEGER NOT NULL DEFAULT 0,
                    verified INTEGER NOT NULL DEFAULT 1,
                    external_ref TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ux_directive_external_ref
                    ON dispatch_directives(external_ref) WHERE external_ref IS NOT NULL;
                CREATE INDEX IF NOT EXISTS ix_directive_live
                    ON dispatch_directives(facility_id, slot_start, status);
                CREATE TABLE IF NOT EXISTS capacity_slot (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    facility_id INTEGER NOT NULL REFERENCES facilities(id),
                    slot_start TEXT NOT NULL,
                    capacity_total REAL NOT NULL,
                    occupied_amount REAL NOT NULL DEFAULT 0,
                    holder_directive_id INTEGER REFERENCES dispatch_directives(id),
                    UNIQUE(facility_id, slot_start)
                );
            """)

    # ---------- 设施 ----------
    def create_facility(self, code: str, name: str, capacity: float,
                        actor: str) -> Dict[str, Any]:
        now = utc_now()
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO facilities(code,name,capacity,created_by,created_at)
                       VALUES(?,?,?,?,?)""",
                    (code, name, capacity, actor, now))
                fid = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("设施编码已存在") from exc
        return self.get_facility(fid)

    def get_facility(self, facility_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM facilities WHERE id=?", (facility_id,)).fetchone()
        if row is None:
            raise NotFoundError("设施不存在")
        return dict(row)

    def find_facility_by_code(self, code: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM facilities WHERE code=?", (code,)).fetchone()
        return dict(row) if row else None

    def list_facilities(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM facilities ORDER BY id").fetchall()
        return [dict(r) for r in rows]

    # ---------- 依据 ----------
    def insert_basis(self, facility_id: int, source_kind: str, conclusion: str,
                     reduce_amount: float, slot_start: str, slot_end: str,
                     supersedes_id: Optional[int], external_ref: Optional[str],
                     actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """INSERT INTO capacity_bases(facility_id,source_kind,conclusion,
                   reduce_amount,slot_start,slot_end,state,supersedes_id,basis_version,
                   external_ref,created_by,created_at,updated_at)
                   VALUES(?,?,?,?,?,?, 'active', ?, 1, ?, ?, ?, ?)""",
                (facility_id, source_kind, conclusion, reduce_amount, slot_start,
                 slot_end, supersedes_id, external_ref, actor, now, now))
            bid = int(cur.lastrowid)
        return self.get_basis(bid)

    def update_basis(self, basis_id: int, conclusion: str, reduce_amount: float,
                     slot_start: str, slot_end: str, expected_version: int,
                     actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """UPDATE capacity_bases SET conclusion=?, reduce_amount=?, slot_start=?,
                   slot_end=?, basis_version=basis_version+1, updated_at=?
                   WHERE id=? AND basis_version=? AND state='active'""",
                (conclusion, reduce_amount, slot_start, slot_end, now,
                 basis_id, expected_version))
            if cur.rowcount == 0:
                row = self.conn.execute(
                    "SELECT basis_version,state FROM capacity_bases WHERE id=?",
                    (basis_id,)).fetchone()
                if row is None:
                    raise NotFoundError("依据不存在")
                if row["state"] != "active":
                    raise ConflictError("依据已失效，不能更新")
                raise ConflictError("依据版本冲突，请刷新后重试")
        return self.get_basis(basis_id)

    def set_basis_state(self, basis_id: int, state: str) -> None:
        with self._lock, self.conn:
            self.conn.execute(
                "UPDATE capacity_bases SET state=?, updated_at=? WHERE id=?",
                (state, utc_now(), basis_id))

    def get_basis(self, basis_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM capacity_bases WHERE id=?", (basis_id,)).fetchone()
        if row is None:
            raise NotFoundError("依据不存在")
        return dict(row)

    def list_active_bases(self, facility_id: int, slot_from: str,
                          slot_to: str) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                """SELECT * FROM capacity_bases WHERE facility_id=? AND state='active'
                   AND slot_end>=? AND slot_start<=? ORDER BY id""",
                (facility_id, slot_from, slot_to)).fetchall()
        return [dict(r) for r in rows]

    def list_bases(self, facility_id: Optional[int] = None) -> List[Dict[str, Any]]:
        if facility_id is None:
            sql = "SELECT * FROM capacity_bases ORDER BY id"
            params: tuple = ()
        else:
            sql = "SELECT * FROM capacity_bases WHERE facility_id=? ORDER BY id"
            params = (facility_id,)
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    # ---------- 批次 ----------
    def get_or_create_batch(self, client_token: str, actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM dispatch_batches WHERE client_token=?",
                (client_token,)).fetchone()
            if row is not None:
                return dict(row)
            with self.conn:
                cur = self.conn.execute(
                    """INSERT INTO dispatch_batches(client_token,status,created_by,
                       created_at,updated_at) VALUES(?, 'open', ?, ?, ?)""",
                    (client_token, actor, now, now))
        return self.get_batch(int(cur.lastrowid))

    def get_batch(self, batch_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM dispatch_batches WHERE id=?", (batch_id,)).fetchone()
        if row is None:
            raise NotFoundError("批次不存在")
        return dict(row)

    def find_batch_by_token(self, client_token: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM dispatch_batches WHERE client_token=?",
                (client_token,)).fetchone()
        return dict(row) if row else None

    def set_batch_status(self, batch_id: int, status: str) -> None:
        with self._lock, self.conn:
            self.conn.execute(
                "UPDATE dispatch_batches SET status=?, updated_at=? WHERE id=?",
                (status, utc_now(), batch_id))

    def list_batch_directives(self, batch_id: int) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM dispatch_directives WHERE batch_id=? ORDER BY id",
                (batch_id,)).fetchall()
        return [self._directive(r) for r in rows]

    # ---------- 指令 ----------
    @staticmethod
    def _directive(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["basis_snapshot"] = (json.loads(item["basis_snapshot"])
                                  if item["basis_snapshot"] else None)
        return item

    def insert_directive(self, facility_id: int, slot_start: str,
                         reduce_amount: float, status: str,
                         basis_snapshot: Optional[dict], batch_id: Optional[int],
                         pending_basis: bool, verified: bool,
                         external_ref: Optional[str], actor: str) -> Dict[str, Any]:
        now = utc_now()
        raw = json.dumps(basis_snapshot, ensure_ascii=False, sort_keys=True) \
            if basis_snapshot else None
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO dispatch_directives(facility_id,slot_start,
                       reduce_amount,status,basis_snapshot,batch_id,pending_basis,
                       verified,external_ref,created_by,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (facility_id, slot_start, reduce_amount, status, raw, batch_id,
                     1 if pending_basis else 0, 1 if verified else 0, external_ref,
                     actor, now, now))
                did = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("指令外部标识已存在") from exc
        return self.get_directive(did)

    def get_directive(self, directive_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM dispatch_directives WHERE id=?",
                (directive_id,)).fetchone()
        if row is None:
            raise NotFoundError("指令不存在")
        return self._directive(row)

    def find_directive_by_ref(self, external_ref: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM dispatch_directives WHERE external_ref=?",
                (external_ref,)).fetchone()
        return self._directive(row) if row else None

    def live_directive(self, facility_id: int, slot_start: str) -> Optional[Dict[str, Any]]:
        """占用槽位的在途指令：已下达/已执行优先，其次独立基线和计划。"""
        with self._lock:
            rows = self.conn.execute(
                """SELECT * FROM dispatch_directives
                   WHERE facility_id=? AND slot_start=?
                     AND status IN ('issued','executed','baseline','planned')
                   ORDER BY CASE status WHEN 'issued' THEN 0 WHEN 'executed' THEN 1
                                        WHEN 'baseline' THEN 2 ELSE 3 END, id""",
                (facility_id, slot_start)).fetchall()
        return self._directive(rows[0]) if rows else None

    def list_slot_directives(self, facility_id: int,
                             slot_start: str) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                """SELECT * FROM dispatch_directives WHERE facility_id=? AND slot_start=?
                   ORDER BY id""", (facility_id, slot_start)).fetchall()
        return [self._directive(r) for r in rows]

    def update_directive_fields(self, directive_id: int, *,
                                reduce_amount: Optional[float] = None,
                                status: Optional[str] = None,
                                basis_snapshot=... ,
                                pending_basis: Optional[bool] = None,
                                verified: Optional[bool] = None,
                                batch_id: Optional[int] = None,
                                external_ref: Optional[str] = ...) -> None:
        sets, params = ["updated_at=?"], [utc_now()]
        if reduce_amount is not None:
            sets.append("reduce_amount=?"); params.append(reduce_amount)
        if status is not None:
            sets.append("status=?"); params.append(status)
        if basis_snapshot is not ...:
            raw = json.dumps(basis_snapshot, ensure_ascii=False, sort_keys=True) \
                if basis_snapshot else None
            sets.append("basis_snapshot=?"); params.append(raw)
        if pending_basis is not None:
            sets.append("pending_basis=?"); params.append(1 if pending_basis else 0)
        if verified is not None:
            sets.append("verified=?"); params.append(1 if verified else 0)
        if batch_id is not None:
            sets.append("batch_id=?"); params.append(batch_id)
        if external_ref is not ...:
            sets.append("external_ref=?"); params.append(external_ref)
        params.append(directive_id)
        with self._lock, self.conn:
            self.conn.execute(
                f"UPDATE dispatch_directives SET {','.join(sets)} WHERE id=?", params)

    def list_directives(self, facility_id: Optional[int] = None,
                        status: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM dispatch_directives WHERE 1=1"
        params: List[Any] = []
        if facility_id is not None:
            sql += " AND facility_id=?"; params.append(facility_id)
        if status is not None:
            sql += " AND status=?"; params.append(status)
        sql += " ORDER BY id"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [self._directive(r) for r in rows]

    # ---------- 槽位占用 ----------
    def get_slot(self, facility_id: int, slot_start: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM capacity_slot WHERE facility_id=? AND slot_start=?",
                (facility_id, slot_start)).fetchone()
        return dict(row) if row else None

    def allocate(self, facility_id: int, slot_start: str, capacity_total: float,
                 amount: float, directive_id: int) -> Dict[str, Any]:
        """在单个事务内按先到先得占容量；容量不足或被他人占用即回滚。"""
        now = utc_now()
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT * FROM capacity_slot WHERE facility_id=? AND slot_start=?",
                (facility_id, slot_start)).fetchone()
            if row is None:
                self.conn.execute(
                    """INSERT INTO capacity_slot(facility_id,slot_start,capacity_total,
                       occupied_amount,holder_directive_id) VALUES(?,?,?,?,?)""",
                    (facility_id, slot_start, capacity_total, amount, directive_id))
                remaining = capacity_total - amount
            else:
                held = self.conn.execute(
                    "SELECT status FROM dispatch_directives WHERE id=?",
                    (row["holder_directive_id"],)).fetchone()
                holder_live = held is not None and held["status"] in (
                    "issued", "executed", "baseline")
                if holder_live and row["holder_directive_id"] != directive_id:
                    # 同一设施同一时段只有一份容量，后到者看到剩余容量
                    self.conn.execute(
                        """UPDATE dispatch_directives SET status='conflicted',
                           updated_at=? WHERE id=? AND status NOT IN
                           ('issued','executed','cancelled')""",
                        (now, directive_id))
                    raise CapacityConflictError(
                        "该时段容量已被其他监管员占用",
                        remaining=row["capacity_total"] - row["occupied_amount"],
                        facility_id=facility_id, slot_start=slot_start)
                new_occupied = row["occupied_amount"] + amount
                if new_occupied > row["capacity_total"] + 1e-9:
                    self.conn.execute(
                        """UPDATE dispatch_directives SET status='conflicted',
                           updated_at=? WHERE id=? AND status NOT IN
                           ('issued','executed','cancelled')""",
                        (now, directive_id))
                    raise CapacityConflictError(
                        "分时容量不足",
                        remaining=row["capacity_total"] - row["occupied_amount"],
                        facility_id=facility_id, slot_start=slot_start)
                self.conn.execute(
                    """UPDATE capacity_slot SET occupied_amount=?,
                       holder_directive_id=?, capacity_total=?
                       WHERE facility_id=? AND slot_start=?""",
                    (new_occupied, directive_id, max(row["capacity_total"],
                     capacity_total), facility_id, slot_start))
                remaining = max(row["capacity_total"], capacity_total) - new_occupied
            self.conn.execute(
                """UPDATE dispatch_directives SET status='issued', updated_at=?
                   WHERE id=? AND status IN ('planned','conflicted')""",
                (now, directive_id))
            slot = self.conn.execute(
                "SELECT * FROM capacity_slot WHERE facility_id=? AND slot_start=?",
                (facility_id, slot_start)).fetchone()
            result = dict(slot)
            result["remaining"] = remaining
            return result

    def slots_between(self, slot_from: str, slot_to: str,
                      facility_id: Optional[int] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM capacity_slot WHERE slot_start BETWEEN ? AND ?"
        params: List[Any] = [slot_from, slot_to]
        if facility_id is not None:
            sql += " AND facility_id=?"; params.append(facility_id)
        sql += " ORDER BY facility_id, slot_start"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]
