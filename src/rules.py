from __future__ import annotations
import re
from .domain import ConflictError, ValidationError, require_text
TITLE='空气污染源许可与合规检查'; ENTITY='排污许可'; ID_PREFIX='AQ'
SEVERITIES=['low', 'medium', 'high', 'critical']; STATES=['draft', 'submitted', 'inspection', 'correction', 'approved']; TRANSITIONS={'draft': ['submitted'], 'submitted': ['inspection'], 'inspection': ['correction'], 'correction': ['approved'], 'approved': []}; TRANSITION_ROLES={'submitted': ['applicant'], 'inspection': ['inspector'], 'correction': ['inspector'], 'approved': ['compliance_manager']}
CREATE_ROLES=set(['applicant']); RECORD_ROLES=set(['applicant', 'inspector']); AUDIT_ROLES=set(['compliance_manager', 'viewer']); VIEW_ROLES=set(['applicant', 'inspector', 'compliance_manager', 'viewer'])
SEVERITY_WEIGHT={'low': 1.0, 'medium': 3.0, 'high': 6.0, 'critical': 9.0}; DEADLINE_HOURS={'low': 72, 'medium': 24, 'high': 8, 'critical': 4}; TERMINAL_STATES=set(['approved'])
def priority_score(severity,quantity=0.0,threshold=1.0,open_records=0):
    if severity not in SEVERITY_WEIGHT: raise ValidationError("unknown severity")
    ratio=quantity/threshold if threshold>0 else 1.0
    return max(0,min(10,int(round(SEVERITY_WEIGHT[severity]+min(4.0,ratio*4.0)+min(3.0,float(open_records))))))
def response_deadline_hours(severity,quantity=0.0,threshold=1.0):
    if severity not in DEADLINE_HOURS: raise ValidationError("unknown severity")
    ratio=quantity/threshold if threshold>0 else 1.0
    return max(1,int(DEADLINE_HOURS[severity]/max(1.0,ratio)))
def escalation_required(severity,quantity=0.0,threshold=1.0):
    return severity==SEVERITIES[-1] or (threshold>0 and quantity>=threshold)
def can_transition(current,target): return target in TRANSITIONS.get(current,[])
def validate_transition(current,target):
    if current not in STATES or target not in STATES: raise ValidationError("未知状态")
    if not can_transition(current,target): raise ConflictError(f"不能从{current}转换到{target}")
def completion_blockers(target,open_records): return ["仍有未关闭事项"] if target in TERMINAL_STATES and open_records>0 else []
def role_for_transition(target): return set(TRANSITION_ROLES.get(target,[]))

# ---- 分时容量调度 ----
# 四类容量依据：治理设备检修、现场检查、排污许可、审计记录，各记一套。
BASIS_KINDS=['maintenance','inspection','permit','audit']
BASIS_KIND_LABELS={'maintenance':'治理设备检修','inspection':'现场检查','permit':'排污许可','audit':'审计记录'}
# 调度指令状态：未执行(planned) / 已下达(issued) / 已执行(executed) / 已取消(cancelled)
INSTRUCTION_STATUSES=['planned','issued','executed','cancelled']
# 依据状态：正常(ok) / 已下达待补依据(pending_supplement) / 旧库待核验基线(pending_verification)
BASIS_STATUSES=['ok','pending_supplement','pending_verification']
# 调度批次状态：待提交(pending) / 已提交(committed) / 失败待恢复(failed)
BATCH_STATUSES=['pending','committed','failed']
# 占用容量的指令状态（计入已占容量）
OCCUPYING_STATUSES=('planned','issued','executed')
# 角色
FACILITY_ROLES=set(['compliance_manager'])
BASIS_RECORD_ROLES=set(['inspector','compliance_manager'])
BASIS_UPDATE_ROLES=set(['inspector','compliance_manager'])
DISPATCH_ROLES=set(['inspector','compliance_manager'])
SLOT_RE=re.compile(r'^\d{4}-\d{2}-\d{2}T\d{2}$')
def normalize_slot(value):
    slot=require_text(value,'slot',32)
    if not SLOT_RE.match(slot): raise ValidationError("slot格式应为YYYY-MM-DDTHH")
    return slot
def normalize_basis_kind(value):
    kind=require_text(value,'kind',32)
    if kind not in BASIS_KINDS: raise ValidationError("kind必须是maintenance/inspection/permit/audit之一")
    return kind
def normalize_instruction_status(value):
    if value not in INSTRUCTION_STATUSES: raise ValidationError("status必须是planned/issued/executed/cancelled之一")
    return value
def normalize_conclusion(value):
    return require_text(value,'conclusion',2000)
def normalize_batch_key(value):
    return require_text(value,'batch_key',100)
def board_total_for(basis,facility):
    """根据依据给出容量台总量与依据状态；无依据时升级为待核验基线。"""
    if basis is not None:
        return float(basis['capacity']), 'ok', basis['id']
    return float(facility['baseline_capacity']), 'pending_verification', None
def remaining_capacity(total,occupied):
    return max(0.0,float(total)-float(occupied))
