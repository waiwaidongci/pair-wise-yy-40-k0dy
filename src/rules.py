from __future__ import annotations
from .domain import ConflictError, ValidationError
TITLE='建筑抗震鉴定与加固排序'; ENTITY='抗震鉴定'; ID_PREFIX='SR'
SEVERITIES=['low', 'medium', 'high', 'severe']; STATES=['proposed', 'assessed', 'design', 'construction', 'accepted', 'rejected']; TRANSITIONS={'proposed': ['assessed'], 'assessed': ['design', 'rejected'], 'design': ['construction'], 'construction': ['accepted'], 'accepted': ['rejected'], 'rejected': []}; TRANSITION_ROLES={'assessed': ['assessor'], 'design': ['structural_engineer'], 'construction': ['structural_engineer'], 'accepted': ['review_board'], 'rejected': ['review_board']}
CREATE_ROLES=set(['assessor']); RECORD_ROLES=set(['assessor', 'structural_engineer']); AUDIT_ROLES=set(['review_board', 'viewer']); VIEW_ROLES=set(['assessor', 'structural_engineer', 'review_board', 'viewer'])
SEVERITY_WEIGHT={'low': 1.0, 'medium': 3.0, 'high': 6.0, 'severe': 9.0}; DEADLINE_HOURS={'low': 72, 'medium': 24, 'high': 8, 'severe': 4}; TERMINAL_STATES=set(['accepted', 'rejected'])
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

# 加固方案
ASSESSMENT_KIND='assessment'
PLAN_ENTITY='加固方案'
PLAN_PENDING='pending'; PLAN_APPROVED='approved'; PLAN_REJECTED='rejected'; PLAN_INVALIDATED='invalidated'
PLAN_STATES=[PLAN_PENDING, PLAN_APPROVED, PLAN_REJECTED, PLAN_INVALIDATED]
PLAN_STATUS_LABELS={PLAN_PENDING:'待复核', PLAN_APPROVED:'已同意', PLAN_REJECTED:'已驳回', PLAN_INVALIDATED:'同意失效'}
PLAN_SUBMIT_ROLES=set(['structural_engineer']); PLAN_REVIEW_ROLES=set(['review_board'])
PLAN_RECORD_ROLES=set(['assessor','structural_engineer'])
PLAN_SUBMIT_ITEM_STATES=set(['assessed','design','construction'])
# 只有处于设计阶段会因同意失效被退回；施工及以后不得倒退
REGRESS_ON_INVALIDATE=set(['design'])
def supersede_reason(new_version): return f"方案换版：v{new_version}提交后，原同意立即失效"
def record_change_reason(kind,change):
    if kind!=ASSESSMENT_KIND: return None
    return "评估事项事后补充登记，原同意立即失效" if change=='add' else "评估事项事后补改，原同意立即失效"
def plan_blocking_reason(plan):
    if plan is None: return "尚未提交加固方案，评审委员会同意前不能进入设计"
    status=plan['status']; version=plan['version']
    if status==PLAN_PENDING: return f"加固方案v{version}待评审委员会复核"
    if status==PLAN_REJECTED: return f"加固方案v{version}已被驳回：{plan.get('reject_reason') or '未填写原因'}"
    if status==PLAN_INVALIDATED: return f"加固方案v{version}的同意已失效：{plan.get('invalid_reason') or ''}"
    return None
def design_gate_reason(plan):
    if plan is not None and plan['status']==PLAN_APPROVED: return None
    return plan_blocking_reason(plan)
