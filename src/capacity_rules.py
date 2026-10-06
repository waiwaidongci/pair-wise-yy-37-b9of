from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .domain import ValidationError

# 四类依据各记一套，统一落到同一张分时容量台
SOURCE_KINDS = ["maintenance", "inspection", "permit", "audit"]
SOURCE_LABELS = {
    "maintenance": "治理设备检修",
    "inspection": "现场检查",
    "permit": "排污许可",
    "audit": "审计记录",
}
# 检修/检查/许可由对口监管员登记，审计记录由审计角色登记
SOURCE_ROLES = {
    "maintenance": {"inspector", "compliance_manager"},
    "inspection": {"inspector", "compliance_manager"},
    "permit": {"applicant", "compliance_manager"},
    "audit": {"compliance_manager"},
}
DISPATCH_ROLES = {"inspector", "compliance_manager"}
FACILITY_ROLES = {"applicant", "inspector", "compliance_manager"}
LEDGER_VIEW_ROLES = {"applicant", "inspector", "compliance_manager", "viewer"}
VERIFIER_ROLES = {"compliance_manager"}

SLOT_MINUTES = 60
MAX_RANGE_SLOTS = 24 * 31  # 一次依据最多覆盖31天


def parse_ts(value, field):
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{field}必须是ISO时间")
    text = value.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValidationError(f"{field}不是有效ISO时间") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def slot_floor(dt):
    return dt.replace(minute=0, second=0, microsecond=0)


def slot_label(dt):
    return slot_floor(dt).strftime("%Y-%m-%dT%H:%MZ")


def normalize_slot(value, field):
    """槽点必须落在整点上，内部统一UTC标签。"""
    dt = parse_ts(value, field)
    floored = slot_floor(dt)
    if floored != dt:
        raise ValidationError(f"{field}必须是整点槽点")
    return floored, slot_label(floored)


def expand_slots(start_dt, end_dt):
    start = slot_floor(start_dt)
    end = slot_floor(end_dt)
    if end < start:
        raise ValidationError("结束时间不能早于开始时间")
    slots = []
    cur = start
    while cur <= end:
        slots.append(slot_label(cur))
        cur += timedelta(minutes=SLOT_MINUTES)
    if len(slots) > MAX_RANGE_SLOTS:
        raise ValidationError(f"时间范围不能超过{MAX_RANGE_SLOTS}个槽位")
    return slots


def covers(basis, slot):
    """槽点落在依据的[start,end]闭区间内即视为覆盖。"""
    return basis["slot_start"] <= slot <= basis["slot_end"]


def merge_requirement(active_bases):
    """同一设施同一时段：四份依据不叠加，取最大要求，只占一份减排容量。"""
    if not active_bases:
        return None
    chosen = max(active_bases, key=lambda b: (float(b["reduce_amount"]), -b["id"]))
    return {
        "reduce_amount": float(chosen["reduce_amount"]),
        "basis_id": chosen["id"],
        "basis_ids": sorted({int(b["id"]) for b in active_bases}),
        "sources": sorted({b["source_kind"] for b in active_bases}),
    }
