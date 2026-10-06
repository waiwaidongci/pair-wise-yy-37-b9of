from __future__ import annotations

from typing import Any, Dict, Optional

from .domain import ensure_role, normalize_severity, require_number, require_text
from .repository import Repository
from .rules import (AUDIT_ROLES, BASIS_RECORD_ROLES, BASIS_UPDATE_ROLES,
                    CREATE_ROLES, DISPATCH_ROLES, ENTITY, FACILITY_ROLES,
                    RECORD_ROLES, TITLE, VIEW_ROLES, completion_blockers,
                    escalation_required, normalize_basis_kind, normalize_batch_key,
                    normalize_conclusion, normalize_instruction_status, normalize_slot,
                    priority_score, response_deadline_hours, role_for_transition,
                    validate_transition)


class Service:
    def __init__(self, repository: Repository):
        self.repository = repository

    def _view(self, role: str) -> None:
        ensure_role(role, VIEW_ROLES)

    def create_item(self, payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, CREATE_ROLES)
        actor = require_text(actor, "actor", 100)
        title = require_text(payload.get("title"), "title", 200)
        description = require_text(payload.get("description"), "description")
        severity = normalize_severity(payload.get("severity"))
        quantity = require_number(payload.get("quantity", 0), "quantity")
        threshold = require_number(payload.get("threshold", 1), "threshold", 0.000001)
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        item = self.repository.create_item(title, description, severity, quantity,
                                           threshold, external_ref, actor)
        self.repository.append_audit("create", ENTITY, item["id"], actor, {
            "title": title, "severity": severity, "quantity": quantity,
            "priority": priority_score(severity, quantity, threshold),
        })
        return self.enrich(item)

    def add_record(self, item_id: int, payload: Dict[str, Any], actor: str,
                   role: str) -> Dict[str, Any]:
        ensure_role(role, RECORD_ROLES)
        actor = require_text(actor, "actor", 100)
        kind = require_text(payload.get("kind"), "kind", 100)
        detail = require_text(payload.get("detail"), "detail")
        status = payload.get("status", "open")
        if status not in ("open", "closed"):
            raise ValueError("status必须是open或closed")
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        record = self.repository.add_record(item_id, kind, detail, status,
                                            external_ref, actor)
        self.repository.append_audit("record", ENTITY, item_id, actor, {
            "record_id": record["id"], "kind": kind, "status": status,
        })
        return record

    def transition(self, item_id: int, target: str, expected_version: int,
                   actor: str, role: str) -> Dict[str, Any]:
        actor = require_text(actor, "actor", 100)
        item = self.repository.get_item(item_id)
        validate_transition(item["status"], target)
        ensure_role(role, role_for_transition(target))
        if not isinstance(expected_version, int) or expected_version < 1:
            raise ValueError("expected_version必须是正整数")
        blockers = completion_blockers(target, self.repository.open_record_count(item_id))
        if blockers:
            from .domain import ConflictError
            raise ConflictError("；".join(blockers))
        updated = self.repository.transition_item(item_id, target, expected_version, actor)
        self.repository.append_audit("transition", ENTITY, item_id, actor, {
            "from": item["status"], "to": target,
            "escalation_required": escalation_required(
                item["severity"], item["quantity"], item["threshold"]),
        })
        return self.enrich(updated)

    def get_item(self, item_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        return self.enrich(self.repository.get_item(item_id))

    def list_items(self, role: str, status: Optional[str] = None) -> list:
        self._view(role)
        return [self.enrich(item) for item in self.repository.list_items(status)]

    def list_records(self, item_id: int, role: str) -> list:
        self._view(role)
        return self.repository.list_records(item_id)

    def audit(self, role: str, item_id: Optional[int] = None) -> list:
        ensure_role(role, AUDIT_ROLES)
        return self.repository.list_audit(item_id)

    # ---- 设施与分时容量调度 ----
    def create_facility(self, payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, FACILITY_ROLES)
        actor = require_text(actor, "actor", 100)
        code = require_text(payload.get("code"), "code", 64)
        name = require_text(payload.get("name"), "name", 200)
        baseline = require_number(payload.get("baseline_capacity", 0), "baseline_capacity", 0)
        facility = self.repository.create_facility(code, name, baseline, actor)
        self.repository.append_audit("create_facility", "设施", facility["id"], actor, {
            "code": code, "name": name, "baseline_capacity": baseline,
        })
        return facility

    def list_facilities(self, role: str) -> list:
        self._view(role)
        return self.repository.list_facilities()

    def get_facility(self, facility_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        return self.repository.get_facility(facility_id)

    def record_basis(self, facility_id: int, payload: Dict[str, Any], actor: str,
                     role: str) -> Dict[str, Any]:
        ensure_role(role, BASIS_RECORD_ROLES)
        actor = require_text(actor, "actor", 100)
        kind = normalize_basis_kind(payload.get("kind"))
        conclusion = normalize_conclusion(payload.get("conclusion"))
        capacity = require_number(payload.get("capacity"), "capacity", 0)
        ref = payload.get("ref")
        if ref is not None:
            ref = require_text(ref, "ref", 100)
        effective_from = payload.get("effective_from")
        if effective_from is not None:
            effective_from = require_text(effective_from, "effective_from", 32)
        basis = self.repository.record_basis(
            facility_id, kind, ref, conclusion, capacity, effective_from, actor)
        self.repository.append_audit("record_basis", "容量依据", basis["id"], actor, {
            "facility_id": facility_id, "kind": kind, "capacity": capacity,
        })
        return basis

    def list_basis(self, facility_id: int, role: str,
                   kind: Optional[str] = None) -> list:
        self._view(role)
        if kind is not None:
            kind = normalize_basis_kind(kind)
        return self.repository.list_basis(facility_id, kind)

    def update_basis_conclusion(self, basis_id: int, payload: Dict[str, Any],
                                actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, BASIS_UPDATE_ROLES)
        actor = require_text(actor, "actor", 100)
        basis = self.repository.get_basis(basis_id)
        conclusion = normalize_conclusion(payload.get("conclusion"))
        capacity = payload.get("capacity")
        if capacity is not None:
            capacity = require_number(capacity, "capacity", 0)
        updated = self.repository.update_basis_conclusion(
            basis_id, conclusion, capacity, actor)
        if basis["kind"] in ("maintenance", "inspection"):
            facility_id = basis["facility_id"]
            # 检修或检查结论更新后：未执行指令按新依据重算，已下达指令保留待补依据。
            new_capacity = float(updated["capacity"])
            for instr in self.repository.list_instructions(facility_id):
                if instr["status"] == "planned":
                    self.repository.recalc_instruction_amount(instr["id"], new_capacity, actor)
                elif instr["status"] == "issued":
                    self.repository.mark_instruction_basis_status(
                        instr["id"], "pending_supplement", actor)
            self.repository.recalc_all_boards_for_facility(facility_id, actor)
        self.repository.append_audit("update_conclusion", "容量依据", basis_id, actor, {
            "facility_id": basis["facility_id"], "kind": basis["kind"],
        })
        return updated

    def get_capacity_board(self, facility_id: int, slot: str, role: str) -> Dict[str, Any]:
        self._view(role)
        slot = normalize_slot(slot)
        return self.repository.get_board(facility_id, slot)

    def list_boards(self, role: str, facility_id: Optional[int] = None) -> list:
        self._view(role)
        return self.repository.list_boards(facility_id)

    def seize_capacity(self, facility_id: int, payload: Dict[str, Any], actor: str,
                        role: str) -> Dict[str, Any]:
        ensure_role(role, DISPATCH_ROLES)
        actor = require_text(actor, "actor", 100)
        slot = normalize_slot(payload.get("slot"))
        amount = require_number(payload.get("amount"), "amount", 0.000001)
        board = self.repository.seize_capacity(facility_id, slot, amount, actor)
        self.repository.append_audit("seize", "容量台", facility_id, actor, {
            "slot": slot, "amount": amount, "remaining": board["remaining"],
        })
        return board

    def submit_batch(self, payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, DISPATCH_ROLES)
        actor = require_text(actor, "actor", 100)
        batch_key = normalize_batch_key(payload.get("batch_key"))
        facility_id = payload.get("facility_id")
        if not isinstance(facility_id, int) or isinstance(facility_id, bool) or facility_id < 1:
            raise ValueError("facility_id必须是正整数")
        slot = normalize_slot(payload.get("slot"))
        amount = require_number(payload.get("amount"), "amount", 0.000001)
        simulate_failure = bool(payload.get("simulate_failure", False))
        batch = self.repository.submit_batch(
            batch_key, facility_id, slot, amount, actor, simulate_failure)
        self.repository.append_audit("submit_batch", "调度批次", facility_id, actor, {
            "batch_key": batch_key, "slot": slot, "amount": amount,
            "status": batch["status"],
        })
        return batch

    def get_batch(self, batch_key: str, role: str) -> Dict[str, Any]:
        self._view(role)
        batch_key = normalize_batch_key(batch_key)
        return self.repository.get_batch(batch_key)

    def recover_batch(self, batch_key: str, actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, DISPATCH_ROLES)
        actor = require_text(actor, "actor", 100)
        batch_key = normalize_batch_key(batch_key)
        batch = self.repository.recover_batch(batch_key, actor)
        self.repository.append_audit("recover_batch", "调度批次", batch["facility_id"], actor, {
            "batch_key": batch_key, "status": batch["status"],
        })
        return batch

    def list_instructions(self, facility_id: int, role: str,
                          status: Optional[str] = None) -> list:
        self._view(role)
        if status is not None:
            status = normalize_instruction_status(status)
        return self.repository.list_instructions(facility_id, status)

    def migrate_pending_verification(self, actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, FACILITY_ROLES)
        actor = require_text(actor, "actor", 100)
        count = self.repository.migrate_pending_verification(actor)
        self.repository.append_audit("migrate_pending_verification", "设施", 0, actor, {
            "updated_boards": count,
        })
        return {"updated_boards": count}

    @staticmethod
    def enrich(item: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(item)
        result["priority"] = priority_score(
            item["severity"], item["quantity"], item["threshold"])
        result["deadline_hours"] = response_deadline_hours(
            item["severity"], item["quantity"], item["threshold"])
        result["escalation_required"] = escalation_required(
            item["severity"], item["quantity"], item["threshold"])
        return result
