from __future__ import annotations
from .domain import ConflictError, ValidationError
TITLE='建筑抗震鉴定与加固排序'; ENTITY='抗震鉴定'; ID_PREFIX='SR'
SEVERITIES=['low', 'medium', 'high', 'severe']; STATES=['proposed', 'assessed', 'design', 'construction', 'accepted', 'rejected']; TRANSITIONS={'proposed': ['assessed'], 'assessed': ['design', 'rejected'], 'design': ['construction'], 'construction': ['accepted'], 'accepted': ['rejected'], 'rejected': []}; TRANSITION_ROLES={'assessed': ['assessor'], 'design': ['structural_engineer'], 'construction': ['structural_engineer'], 'accepted': ['review_board'], 'rejected': ['review_board']}
CREATE_ROLES=set(['assessor']); RECORD_ROLES=set(['assessor', 'structural_engineer']); AUDIT_ROLES=set(['review_board', 'viewer']); VIEW_ROLES=set(['assessor', 'structural_engineer', 'review_board', 'viewer'])
SEVERITY_WEIGHT={'low': 1.0, 'medium': 3.0, 'high': 6.0, 'severe': 9.0}; DEADLINE_HOURS={'low': 72, 'medium': 24, 'high': 8, 'severe': 4}; TERMINAL_STATES=set(['accepted', 'rejected'])
PLAN_ENTITY='加固方案'; PLAN_STATES=['pending', 'approved', 'rejected', 'invalidated']; PLAN_SUBMIT_ROLES=set(['structural_engineer']); PLAN_REVIEW_ROLES=set(['review_board']); PLAN_SUBMIT_ITEM_STATES=set(['assessed', 'design'])
PLAN_INVALIDATE_NEW_VERSION='方案换版'; PLAN_INVALIDATE_RECORD_AMENDED='评估事项补改'; REVIEW_DECISIONS={'approve': 'approved', 'reject': 'rejected'}
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
def validate_plan_submission(item_status):
    if item_status not in PLAN_SUBMIT_ITEM_STATES: raise ConflictError(f"当前状态不允许提交加固方案：{item_status}")
def normalize_decision(value):
    if value not in REVIEW_DECISIONS: raise ValidationError("decision必须是approve或reject")
    return REVIEW_DECISIONS[value]
def validate_review(decision,reason):
    if decision=='rejected' and not (isinstance(reason,str) and reason.strip()): raise ValidationError("驳回必须填写原因")
def design_blockers(latest_plan):
    if not latest_plan: return ["尚未提交加固方案"]
    status=latest_plan.get("status")
    if status=='approved': return []
    if status=='pending': return ["加固方案待评审"]
    if status=='rejected': return ["加固方案已驳回："+(latest_plan.get("review_reason") or "未填写原因")]
    if status=='invalidated': return ["加固方案已失效："+(latest_plan.get("invalidated_reason") or "原因未记录")]
    return ["加固方案状态异常"]
def transition_blockers(target,open_records,latest_plan):
    blockers=completion_blockers(target,open_records)
    if target=='design': blockers=blockers+design_blockers(latest_plan)
    return blockers
