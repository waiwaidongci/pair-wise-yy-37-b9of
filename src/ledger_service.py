from __future__ import annotations

from typing import Any, Dict, List, Optional

from . import capacity_rules as cr
from .domain import (CapacityConflictError, ConflictError, DomainError,
                     ensure_role, require_number, require_text)
from .ledger_store import LedgerStore
from .repository import Repository

ENTITY = "分时容量台"


class LedgerService:
    def __init__(self, repository: Repository, store: LedgerStore):
        self.repository = repository
        self.store = store
        # 测试钩子：指定第几次分配尝试抛错，模拟"调度写入失败"
        self._fail_allocations: set = set()
        self._alloc_attempt = 0

    def _audit(self, action: str, entity_id: int, actor: str, detail: dict) -> None:
        self.repository.append_audit(action, ENTITY, entity_id, actor, detail)

    @staticmethod
    def _snapshot(merged: Optional[dict]) -> Optional[dict]:
        return None if merged is None else {
            "reduce_amount": merged["reduce_amount"],
            "basis_id": merged["basis_id"],
            "basis_ids": merged["basis_ids"],
            "sources": merged["sources"],
        }

    def _merged_at(self, facility_id: int, slot: str) -> Optional[dict]:
        return cr.merge_requirement(
            self.store.list_active_bases(facility_id, slot, slot))

    # ---------- 设施 ----------
    def create_facility(self, payload: Dict[str, Any], actor: str,
                        role: str) -> Dict[str, Any]:
        ensure_role(role, cr.FACILITY_ROLES)
        actor = require_text(actor, "actor", 100)
        code = require_text(payload.get("code"), "code", 64)
        name = require_text(payload.get("name"), "name", 200)
        capacity = require_number(payload.get("capacity"), "capacity", 0.000001)
        facility = self.store.create_facility(code, name, capacity, actor)
        self._audit("facility.create", facility["id"], actor,
                    {"code": code, "capacity": capacity})
        return facility

    def list_facilities(self, role: str) -> list:
        ensure_role(role, cr.LEDGER_VIEW_ROLES)
        return self.store.list_facilities()

    # ---------- 四类依据 ----------
    def register_basis(self, facility_id: int, payload: Dict[str, Any],
                       actor: str, role: str) -> Dict[str, Any]:
        actor = require_text(actor, "actor", 100)
        facility = self.store.get_facility(facility_id)
        source_kind = require_text(payload.get("source_kind"), "source_kind", 32)
        if source_kind not in cr.SOURCE_KINDS:
            from .domain import ValidationError
            raise ValidationError("source_kind必须是maintenance/inspection/permit/audit")
        ensure_role(role, cr.SOURCE_ROLES[source_kind])
        conclusion = require_text(payload.get("conclusion"), "conclusion")
        reduce_amount = require_number(payload.get("reduce_amount"), "reduce_amount")
        if reduce_amount > facility["capacity"] + 1e-9:
            from .domain import ValidationError
            raise ValidationError("减排要求不能超过设施容量")
        start_dt = cr.parse_ts(payload.get("effective_from"), "effective_from")
        end_dt = cr.parse_ts(payload.get("effective_to"), "effective_to")
        slots = cr.expand_slots(start_dt, end_dt)
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        supersedes_id = payload.get("supersedes_id")
        if supersedes_id is not None:
            if not isinstance(supersedes_id, int):
                from .domain import ValidationError
                raise ValidationError("supersedes_id必须是整数")
            old = self.store.get_basis(supersedes_id)
            if (old["facility_id"] != facility_id
                    or old["source_kind"] != source_kind):
                from .domain import ValidationError
                raise ValidationError("只能作废同设施同类型的旧依据")
            self.store.set_basis_state(supersedes_id, "superseded")
        basis = self.store.insert_basis(
            facility_id, source_kind, conclusion, reduce_amount,
            slots[0], slots[-1], supersedes_id, external_ref, actor)
        changes = self._replan_slots(facility_id, slots, actor)
        self._audit("basis.register", facility_id, actor, {
            "basis_id": basis["id"], "source_kind": source_kind,
            "slot_start": slots[0], "slot_end": slots[-1],
            "supersedes_id": supersedes_id,
            **{f"replan.{k}": len(v) for k, v in changes.items()},
        })
        result = dict(basis)
        result["replan"] = changes
        return result

    def update_basis(self, basis_id: int, payload: Dict[str, Any],
                     actor: str, role: str) -> Dict[str, Any]:
        actor = require_text(actor, "actor", 100)
        existing = self.store.get_basis(basis_id)
        ensure_role(role, cr.SOURCE_ROLES[existing["source_kind"]])
        facility = self.store.get_facility(existing["facility_id"])
        conclusion = require_text(payload.get("conclusion"), "conclusion")
        reduce_amount = require_number(payload.get("reduce_amount"), "reduce_amount")
        if reduce_amount > facility["capacity"] + 1e-9:
            from .domain import ValidationError
            raise ValidationError("减排要求不能超过设施容量")
        start_dt = cr.parse_ts(payload.get("effective_from"), "effective_from")
        end_dt = cr.parse_ts(payload.get("effective_to"), "effective_to")
        slots = cr.expand_slots(start_dt, end_dt)
        expected_version = payload.get("expected_version")
        if not isinstance(expected_version, int) or expected_version < 1:
            from .domain import ValidationError
            raise ValidationError("expected_version必须是正整数")
        before = set(cr.expand_slots(
            cr.parse_ts(existing["slot_start"], "slot_start"),
            cr.parse_ts(existing["slot_end"], "slot_end")))
        basis = self.store.update_basis(basis_id, conclusion, reduce_amount,
                                        slots[0], slots[-1], expected_version, actor)
        affected = sorted(before | set(slots))
        changes = self._replan_slots(existing["facility_id"], affected, actor)
        self._audit("basis.update", existing["facility_id"], actor, {
            "basis_id": basis_id, "version": basis["basis_version"],
            "slot_start": slots[0], "slot_end": slots[-1],
            **{f"replan.{k}": len(v) for k, v in changes.items()},
        })
        result = dict(basis)
        result["replan"] = changes
        return result

    def _replan_slots(self, facility_id: int, slots: List[str],
                      actor: str) -> Dict[str, List[dict]]:
        """检修或检查结论更新后的重算：未执行指令按新依据重算，已下达指令保留待补依据。"""
        changes: Dict[str, List[dict]] = {
            "planned_recomputed": [], "planned_cancelled": [],
            "planned_created": [], "held_pending_basis": [], "baseline_verified": [],
        }
        for slot in slots:
            merged = self._merged_at(facility_id, slot)
            directives = self.store.list_slot_directives(facility_id, slot)
            live = self.store.live_directive(facility_id, slot)
            planned = next((d for d in directives if d["status"] == "planned"), None)
            held = next((d for d in directives
                         if d["status"] in ("issued", "executed", "baseline")), None)
            if planned is not None and held is None:
                if merged is None:
                    self.store.update_directive_fields(
                        planned["id"], status="cancelled",
                        basis_snapshot=None, pending_basis=False)
                    changes["planned_cancelled"].append(
                        {"directive_id": planned["id"], "slot": slot})
                else:
                    self.store.update_directive_fields(
                        planned["id"], reduce_amount=merged["reduce_amount"],
                        basis_snapshot=self._snapshot(merged), pending_basis=False)
                    changes["planned_recomputed"].append(
                        {"directive_id": planned["id"], "slot": slot,
                         "reduce_amount": merged["reduce_amount"]})
            elif planned is None and held is None and merged is not None:
                directive = self.store.insert_directive(
                    facility_id, slot, merged["reduce_amount"], "planned",
                    self._snapshot(merged), None, False, True, None,
                    actor or "planner")
                changes["planned_created"].append(
                    {"directive_id": directive["id"], "slot": slot,
                     "reduce_amount": merged["reduce_amount"]})
            # 已下达/已执行/待核验基线：保留指令，按新依据决定是否待补依据
            if held is not None:
                sufficient = merged is not None and (
                    merged["reduce_amount"] + 1e-9 >= held["reduce_amount"])
                snapshot = self._snapshot(merged)
                if held["status"] == "baseline" and sufficient and not held["verified"]:
                    self.store.update_directive_fields(
                        held["id"], verified=True, pending_basis=False,
                        basis_snapshot=snapshot)
                    changes["baseline_verified"].append(
                        {"directive_id": held["id"], "slot": slot})
                elif sufficient and held["pending_basis"]:
                    self.store.update_directive_fields(
                        held["id"], pending_basis=False, basis_snapshot=snapshot)
                    changes["held_pending_basis"].append(
                        {"directive_id": held["id"], "slot": slot, "cleared": True})
                elif not sufficient and not held["pending_basis"]:
                    self.store.update_directive_fields(
                        held["id"], pending_basis=True, basis_snapshot=snapshot)
                    changes["held_pending_basis"].append(
                        {"directive_id": held["id"], "slot": slot, "cleared": False})
            del live
        return changes

    def refresh_plan(self, payload: Optional[Dict[str, Any]], actor: str,
                     role: str) -> Dict[str, Any]:
        ensure_role(role, cr.DISPATCH_ROLES)
        actor = require_text(actor, "actor", 100)
        payload = payload or {}
        facility_id = payload.get("facility_id")
        if facility_id is not None:
            if not isinstance(facility_id, int):
                from .domain import ValidationError
                raise ValidationError("facility_id必须是整数")
            facilities = [self.store.get_facility(facility_id)]
        else:
            facilities = self.store.list_facilities()
        start_dt = cr.parse_ts(payload.get("from"), "from")
        end_dt = cr.parse_ts(payload.get("to"), "to")
        slots = cr.expand_slots(start_dt, end_dt)
        all_changes: Dict[str, List[dict]] = {
            "planned_recomputed": [], "planned_cancelled": [],
            "planned_created": [], "held_pending_basis": [], "baseline_verified": [],
        }
        for facility in facilities:
            changes = self._replan_slots(facility["id"], slots, actor)
            for key in all_changes:
                all_changes[key].extend(changes[key])
        self._audit("plan.refresh", 0, actor, {
            "facility_id": facility_id, "from": slots[0], "to": slots[-1],
            **{f"{k}": len(v) for k, v in all_changes.items()},
        })
        return {"facility_id": facility_id, "from": slots[0], "to": slots[-1],
                "changes": all_changes}

    # ---------- 调度批次：抢占、写入失败、幂等重提 ----------
    def dispatch(self, payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, cr.DISPATCH_ROLES)
        actor = require_text(actor, "actor", 100)
        token = require_text(payload.get("client_token"), "client_token", 100)
        lines = payload.get("lines")
        if not isinstance(lines, list) or not lines:
            from .domain import ValidationError
            raise ValidationError("lines必须是非空数组")
        batch = self.store.get_or_create_batch(token, actor)
        results = self._process_lines(batch, lines, actor)
        batch = self.store.get_batch(batch["id"])
        return {"batch_id": batch["id"], "client_token": token,
                "batch_status": batch["status"], "lines": results}

    def recover_batch(self, batch_id: int, payload: Optional[Dict[str, Any]],
                      actor: str, role: str) -> Dict[str, Any]:
        """写入失败后按批次恢复：已下达的原样返回，未执行的重试，重提不重复占容量。"""
        ensure_role(role, cr.DISPATCH_ROLES)
        actor = require_text(actor, "actor", 100)
        batch = self.store.get_batch(batch_id)
        payload = payload or {}
        if payload.get("lines"):
            lines = payload["lines"]
        else:
            # 从已落库的批次行重建，崩溃在插入前的行需客户端带lines重提
            lines = [{"facility_id": d["facility_id"], "slot_start": d["slot_start"],
                      "reduce_amount": d["reduce_amount"],
                      "external_ref": d["external_ref"]}
                     for d in self.store.list_batch_directives(batch_id)]
        if not lines:
            from .domain import ValidationError
            raise ValidationError("批次中没有可恢复的行，请携带lines重提")
        results = self._process_lines(batch, lines, actor)
        batch = self.store.get_batch(batch_id)
        self._audit("batch.recover", batch_id, actor, {"lines": len(results)})
        return {"batch_id": batch_id, "client_token": batch["client_token"],
                "batch_status": batch["status"], "lines": results, "recovered": True}

    def _process_lines(self, batch: dict, lines: list, actor: str) -> List[dict]:
        results: List[dict] = []
        for index, line in enumerate(lines):
            if not isinstance(line, dict):
                from .domain import ValidationError
                raise ValidationError(f"第{index + 1}行必须是对象")
            facility_id = line.get("facility_id")
            if not isinstance(facility_id, int):
                from .domain import ValidationError
                raise ValidationError(f"第{index + 1}行facility_id必须是整数")
            facility = self.store.get_facility(facility_id)
            _dt, slot = cr.normalize_slot(line.get("slot_start"), "slot_start")
            external_ref = line.get("external_ref")
            if external_ref is not None:
                external_ref = require_text(external_ref, "external_ref", 100)
            explicit = "reduce_amount" in line and line["reduce_amount"] is not None
            wanted = require_number(line["reduce_amount"], "reduce_amount", 0.0) \
                if explicit else None
            merged = self._merged_at(facility_id, slot)

            # 幂等：同一外部标识重提，已占用的回显不重复占容量，未完成的重试
            ref = (self.store.find_directive_by_ref(external_ref)
                   if external_ref else None)
            if ref is not None:
                if ref["status"] in ("issued", "executed", "baseline"):
                    slot_row = self.store.get_slot(facility_id, slot)
                    results.append(self._line_result(ref, slot_row, retried=True))
                    continue
                if ref["status"] in ("planned", "conflicted"):
                    directive = ref
                    amount = wanted if wanted is not None else float(
                        directive["reduce_amount"])
                    if wanted is not None:
                        self.store.update_directive_fields(
                            directive["id"], reduce_amount=wanted)
                    directive = self.store.get_directive(directive["id"])
                else:  # 已取消的外部标识换一条新指令
                    directive = None
            else:
                directive = self.store.live_directive(facility_id, slot)
                if (directive is not None and directive["status"] == "planned"
                        and directive["batch_id"] not in (None, batch["id"])):
                    # 其他批次的计划行：不抢占，留给存储层按先到者裁决
                    directive = None

            held_statuses = {"issued", "executed", "baseline"}
            if directive is not None and directive["status"] in held_statuses:
                # 先到者已占用该设施该时段，后到者只看到剩余容量
                slot_row = self.store.get_slot(facility_id, slot)
                results.append({
                    "facility_id": facility_id, "slot_start": slot,
                    "reduce_amount": wanted if wanted is not None
                    else float(directive["reduce_amount"]),
                    "status": "conflicted",
                    "error": "该时段容量已被其他监管员占用",
                    "remaining": (slot_row["capacity_total"] - slot_row["occupied_amount"])
                    if slot_row else facility["capacity"],
                    "holder_directive_id": directive["id"]})
                continue

            if directive is None:
                if wanted is not None:
                    amount = wanted
                elif merged is not None:
                    amount = float(merged["reduce_amount"])
                else:
                    # 没有依据也没有显式容量，无法下达
                    directive = self.store.insert_directive(
                        facility_id, slot, 0.0, "conflicted", None, batch["id"],
                        True, True, external_ref, actor)
                    results.append({
                        "facility_id": facility_id, "slot_start": slot,
                        "reduce_amount": 0.0, "status": "conflicted",
                        "error": "缺少容量依据",
                        "remaining": facility["capacity"]})
                    continue
                pending = merged is None or merged["reduce_amount"] + 1e-9 < amount
                directive = self.store.insert_directive(
                    facility_id, slot, amount, "planned",
                    self._snapshot(merged), batch["id"], pending, True,
                    external_ref, actor)
            elif directive["status"] == "planned":
                if directive["batch_id"] not in (None, batch["id"]):
                    # 其他批次已先挂出该时段计划，视为先到者
                    slot_row = self.store.get_slot(facility_id, slot)
                    results.append({
                        "facility_id": facility_id, "slot_start": slot,
                        "reduce_amount": wanted if wanted is not None
                        else float(directive["reduce_amount"]),
                        "status": "conflicted",
                        "error": "该时段已被其他批次的计划占用",
                        "remaining": (slot_row["capacity_total"] - slot_row[
                            "occupied_amount"]) if slot_row else facility["capacity"],
                        "holder_directive_id": directive["id"]})
                    continue
                amount = wanted if wanted is not None else float(
                    directive["reduce_amount"])
                self.store.update_directive_fields(
                    directive["id"], batch_id=batch["id"],
                    reduce_amount=amount,
                    **({"external_ref": external_ref} if external_ref is not None
                       else {}))
                directive = self.store.get_directive(directive["id"])

            # 测试钩子：模拟调度写入失败，事务回滚后整行可按批次恢复
            self._alloc_attempt += 1
            if self._alloc_attempt in self._fail_allocations:
                self._fail_allocations.discard(self._alloc_attempt)
                raise ConflictError("调度写入失败，请按批次恢复")

            try:
                slot_row = self.store.allocate(
                    facility_id, slot, facility["capacity"], amount, directive["id"])
            except CapacityConflictError as exc:
                self.store.update_directive_fields(directive["id"], status="conflicted")
                slot_row = self.store.get_slot(facility_id, slot)
                results.append({
                    "facility_id": facility_id, "slot_start": slot,
                    "reduce_amount": amount, "status": "conflicted",
                    "error": str(exc), "remaining": exc.remaining,
                    "holder_directive_id": slot_row["holder_directive_id"]
                    if slot_row else None})
                continue
            pending = merged is None or merged["reduce_amount"] + 1e-9 < amount
            self.store.update_directive_fields(
                directive["id"], pending_basis=True if pending else False,
                basis_snapshot=self._snapshot(merged))
            directive = self.store.get_directive(directive["id"])
            results.append(self._line_result(directive, slot_row))
        statuses = {r["status"] for r in results}
        final = "completed" if statuses <= {"issued", "executed"} else (
            "partial" if statuses & {"issued", "executed"} else "failed")
        self.store.set_batch_status(batch["id"], final)
        self._audit("batch.dispatch", batch["id"], actor, {
            "lines": len(results), "status": final,
            "conflicts": sum(1 for r in results if r["status"] == "conflicted")})
        return results

    @staticmethod
    def _line_result(directive: dict, slot_row: Optional[dict],
                     retried: bool = False) -> dict:
        result = {
            "directive_id": directive["id"], "facility_id": directive["facility_id"],
            "slot_start": directive["slot_start"],
            "reduce_amount": directive["reduce_amount"],
            "status": directive["status"],
            "pending_basis": bool(directive["pending_basis"]),
            "verified": bool(directive["verified"]),
            "basis_snapshot": directive["basis_snapshot"],
        }
        if slot_row is not None:
            result["remaining"] = slot_row["capacity_total"] - slot_row["occupied_amount"]
        if retried:
            result["retried"] = True
        return result

    # ---------- 指令执行与补依据 ----------
    def execute_directive(self, directive_id: int, actor: str,
                          role: str) -> Dict[str, Any]:
        ensure_role(role, cr.DISPATCH_ROLES)
        actor = require_text(actor, "actor", 100)
        directive = self.store.get_directive(directive_id)
        if directive["status"] != "issued":
            raise ConflictError("只有已下达指令可以执行")
        self.store.update_directive_fields(directive_id, status="executed")
        updated = self.store.get_directive(directive_id)
        self._audit("directive.execute", directive["facility_id"], actor,
                    {"directive_id": directive_id, "slot": directive["slot_start"]})
        return updated

    def supplement_basis(self, directive_id: int, payload: Dict[str, Any],
                         actor: str, role: str) -> Dict[str, Any]:
        """已下达指令保留待补依据：核验员补充依据后按新依据重算该时段。"""
        ensure_role(role, cr.VERIFIER_ROLES)
        actor = require_text(actor, "actor", 100)
        basis_id = payload.get("basis_id")
        if not isinstance(basis_id, int):
            from .domain import ValidationError
            raise ValidationError("basis_id必须是整数")
        note = payload.get("note", "")
        if note is not None:
            note = require_text(note or "补依据", "note", 2000)
        directive = self.store.get_directive(directive_id)
        if directive["status"] not in ("issued", "executed", "baseline"):
            raise ConflictError("只有已下达或基线指令可以补依据")
        basis = self.store.get_basis(basis_id)
        if basis["facility_id"] != directive["facility_id"] \
                or not cr.covers(basis, directive["slot_start"]):
            raise ConflictError("依据不覆盖该设施时段")
        if basis["reduce_amount"] + 1e-9 < directive["reduce_amount"]:
            raise ConflictError("补充依据仍不足以支撑该指令容量")
        self._audit("directive.supplement_basis", directive["facility_id"], actor,
                    {"directive_id": directive_id, "basis_id": basis_id, "note": note})
        self._replan_slots(directive["facility_id"], [directive["slot_start"]], actor)
        return self.store.get_directive(directive_id)

    # ---------- 旧库导入：缺容量依据升级为待核验基线 ----------
    def legacy_import(self, payload: Dict[str, Any], actor: str,
                      role: str) -> Dict[str, Any]:
        ensure_role(role, cr.VERIFIER_ROLES)
        actor = require_text(actor, "actor", 100)
        lines = payload.get("lines")
        if not isinstance(lines, list) or not lines:
            from .domain import ValidationError
            raise ValidationError("lines必须是非空数组")
        results: List[dict] = []
        for index, line in enumerate(lines):
            if not isinstance(line, dict):
                from .domain import ValidationError
                raise ValidationError(f"第{index + 1}行必须是对象")
            code = require_text(line.get("facility_code"), "facility_code", 64)
            facility = self.store.find_facility_by_code(code)
            if facility is None:
                from .domain import ValidationError
                raise ValidationError(f"设施编码{code}不存在，请先登记设施")
            _dt, slot = cr.normalize_slot(line.get("slot_start"), "slot_start")
            amount = require_number(line.get("reduce_amount"), "reduce_amount", 0.0)
            external_ref = require_text(line.get("external_ref"),
                                        "external_ref", 100)
            existing = self.store.find_directive_by_ref(external_ref)
            if existing is not None:  # 重提不重复占容量
                slot_row = self.store.get_slot(facility["id"], slot)
                results.append(self._line_result(existing, slot_row, retried=True))
                continue
            merged = self._merged_at(facility["id"], slot)
            sufficient = merged is not None and merged["reduce_amount"] + 1e-9 >= amount
            verified = bool(sufficient)
            directive = self.store.insert_directive(
                facility["id"], slot, amount, "baseline",
                self._snapshot(merged), None, not verified, verified,
                external_ref, actor)
            try:
                slot_row = self.store.allocate(
                    facility["id"], slot, facility["capacity"], amount,
                    directive["id"])
            except CapacityConflictError as exc:
                results.append({
                    "facility_id": facility["id"], "slot_start": slot,
                    "reduce_amount": amount, "status": "conflicted",
                    "error": str(exc), "remaining": exc.remaining})
                continue
            directive = self.store.get_directive(directive["id"])
            self._audit("legacy.import", facility["id"], actor, {
                "directive_id": directive["id"], "slot": slot,
                "external_ref": external_ref,
                "verified": verified,
                "baseline": "pending_verification" if not verified else "verified"})
            results.append(self._line_result(directive, slot_row))
        return {"lines": results,
                "pending_verification": [r["directive_id"] for r in results
                                         if r.get("status") == "baseline"
                                         and not r.get("verified")]}

    # ---------- 容量台视图 ----------
    def ledger(self, role: str, facility_id: Optional[int] = None,
               slot_from: Optional[str] = None,
               slot_to: Optional[str] = None) -> Dict[str, Any]:
        ensure_role(role, cr.LEDGER_VIEW_ROLES)
        if slot_from is None or slot_to is None:
            from .domain import ValidationError
            raise ValidationError("必须提供from和to")
        from_dt = cr.parse_ts(slot_from, "from")
        to_dt = cr.parse_ts(slot_to, "to")
        slots = cr.expand_slots(from_dt, to_dt)
        facilities = ([self.store.get_facility(facility_id)] if facility_id is not None
                      else self.store.list_facilities())
        rows: List[dict] = []
        for facility in facilities:
            active = self.store.list_active_bases(facility["id"], slots[0], slots[-1])
            slot_rows = {s["slot_start"]: s
                         for s in self.store.slots_between(slots[0], slots[-1],
                                                           facility["id"])}
            for slot in slots:
                in_range = [b for b in active if cr.covers(b, slot)]
                merged = cr.merge_requirement(in_range)
                record = slot_rows.get(slot)
                held = self.store.live_directive(facility["id"], slot)
                occupied = float(record["occupied_amount"]) if record else 0.0
                rows.append({
                    "facility_id": facility["id"], "facility_code": facility["code"],
                    "slot_start": slot,
                    "capacity_total": facility["capacity"],
                    "occupied_amount": occupied,
                    "remaining": facility["capacity"] - occupied,
                    "holder_directive_id": record["holder_directive_id"] if record
                    else None,
                    "holder_status": held["status"] if held else None,
                    "pending_basis": bool(held["pending_basis"]) if held else False,
                    "verified": bool(held["verified"]) if held else True,
                    "merged_basis": self._snapshot(merged),
                    "basis_count": len(in_range),
                    "sources": sorted({b["source_kind"] for b in in_range}),
                })
        return {"from": slots[0], "to": slots[-1], "slots": rows}

    def list_directives(self, role: str, facility_id: Optional[int] = None,
                        status: Optional[str] = None) -> list:
        ensure_role(role, cr.LEDGER_VIEW_ROLES)
        return self.store.list_directives(facility_id, status)

    def list_bases(self, role: str, facility_id: Optional[int] = None) -> list:
        ensure_role(role, cr.LEDGER_VIEW_ROLES)
        return self.store.list_bases(facility_id)
