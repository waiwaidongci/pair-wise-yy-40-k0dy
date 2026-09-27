from __future__ import annotations

from typing import Any, Dict, List, Optional

from .domain import (ConflictError, ensure_role, normalize_severity,
                     require_number, require_text)
from .repository import Repository
from .rules import (ASSESSMENT_KIND, AUDIT_ROLES, CREATE_ROLES, ENTITY,
                    PLAN_APPROVED, PLAN_ENTITY, PLAN_RECORD_ROLES,
                    PLAN_REVIEW_ROLES, PLAN_STATUS_LABELS, PLAN_SUBMIT_ROLES,
                    RECORD_ROLES, VIEW_ROLES, completion_blockers,
                    design_gate_reason, escalation_required,
                    plan_blocking_reason, priority_score,
                    response_deadline_hours, role_for_transition,
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

    @staticmethod
    def _validate_record_status(value: str) -> str:
        if value not in ("open", "closed"):
            from .domain import ValidationError
            raise ValidationError("status必须是open或closed")
        return value

    @staticmethod
    def _validate_external_ref(value: Any) -> Optional[str]:
        if value is None:
            return None
        return require_text(value, "external_ref", 100)

    def add_record(self, item_id: int, payload: Dict[str, Any], actor: str,
                   role: str) -> Dict[str, Any]:
        ensure_role(role, RECORD_ROLES)
        actor = require_text(actor, "actor", 100)
        kind = require_text(payload.get("kind"), "kind", 100)
        detail = require_text(payload.get("detail"), "detail")
        status = self._validate_record_status(payload.get("status", "open"))
        external_ref = self._validate_external_ref(payload.get("external_ref"))
        record, invalidated = self.repository.add_record(
            item_id, kind, detail, status, external_ref, actor)
        self.repository.append_audit("record", ENTITY, item_id, actor, {
            "record_id": record["id"], "kind": kind, "status": status,
        })
        if invalidated:
            self._audit_invalidated(item_id, invalidated, actor,
                                    "assessment_item_added" if kind == ASSESSMENT_KIND else "record_added")
        return record

    def update_record(self, record_id: int, payload: Dict[str, Any], actor: str,
                      role: str) -> Dict[str, Any]:
        ensure_role(role, PLAN_RECORD_ROLES)
        actor = require_text(actor, "actor", 100)
        kind = require_text(payload.get("kind"), "kind", 100)
        detail = require_text(payload.get("detail"), "detail")
        status = self._validate_record_status(payload.get("status", "open"))
        external_ref = self._validate_external_ref(payload.get("external_ref"))
        record, invalidated = self.repository.update_record(
            record_id, kind, detail, status, external_ref, actor)
        self.repository.append_audit("record_update", ENTITY, record["item_id"], actor, {
            "record_id": record_id, "kind": kind, "status": status,
        })
        if invalidated:
            self._audit_invalidated(record["item_id"], invalidated, actor,
                                    "assessment_item_updated")
        return record

    def transition(self, item_id: int, target: str, expected_version: int,
                   actor: str, role: str) -> Dict[str, Any]:
        actor = require_text(actor, "actor", 100)
        item = self.repository.get_item(item_id)
        validate_transition(item["status"], target)
        ensure_role(role, role_for_transition(target))
        if not isinstance(expected_version, int) or expected_version < 1:
            from .domain import ValidationError
            raise ValidationError("expected_version必须是正整数")
        blockers = completion_blockers(target, self.repository.open_record_count(item_id))
        if blockers:
            raise ConflictError("；".join(blockers))
        if target == "design":
            reason = design_gate_reason(self.repository.get_latest_plan(item_id))
            if reason:
                raise ConflictError(reason)
        updated = self.repository.transition_item(item_id, target, expected_version, actor)
        self.repository.append_audit("transition", ENTITY, item_id, actor, {
            "from": item["status"], "to": target,
            "escalation_required": escalation_required(
                item["severity"], item["quantity"], item["threshold"]),
        })
        return self.enrich(updated)

    # ---- 加固方案 ----

    @staticmethod
    def _validate_basis_ids(value: Any) -> List[int]:
        from .domain import ValidationError
        if not isinstance(value, list) or not value:
            raise ValidationError("basis_record_ids必须是非空数组，且只能引用已关闭的评估事项")
        basis_ids: List[int] = []
        for raw in value:
            if isinstance(raw, bool) or not isinstance(raw, int) or raw < 1:
                raise ValidationError("依据事项id必须是正整数")
            if raw not in basis_ids:
                basis_ids.append(raw)
        return basis_ids

    def submit_plan(self, item_id: int, payload: Dict[str, Any], actor: str,
                    role: str) -> Dict[str, Any]:
        ensure_role(role, PLAN_SUBMIT_ROLES)
        actor = require_text(actor, "actor", 100)
        content = require_text(payload.get("content"), "content")
        basis_ids = self._validate_basis_ids(payload.get("basis_record_ids"))
        plan, superseded = self.repository.create_plan(item_id, content, basis_ids, actor)
        if superseded:
            self._audit_invalidated(item_id, superseded, actor, "plan_superseded")
        self.repository.append_audit("plan_submit", PLAN_ENTITY, item_id, actor, {
            "plan_id": plan["id"], "version": plan["version"],
            "basis_record_ids": basis_ids,
        })
        return self.plan_detail(plan["id"], role)

    def review_plan(self, plan_id: int, payload: Dict[str, Any], actor: str,
                    role: str) -> Dict[str, Any]:
        ensure_role(role, PLAN_REVIEW_ROLES)
        actor = require_text(actor, "actor", 100)
        from .domain import ValidationError
        decision = payload.get("decision")
        if decision not in ("approved", "rejected"):
            raise ValidationError("decision必须是approved或rejected")
        expected_version = payload.get("expected_version")
        if not isinstance(expected_version, int) or expected_version < 1:
            raise ValidationError("expected_version必须是正整数")
        comment = payload.get("comment")
        if comment is not None:
            comment = require_text(comment, "comment")
        reject_reason = None
        if decision == "rejected":
            reject_reason = require_text(payload.get("reject_reason"), "reject_reason")
        plan = self.repository.review_plan(
            plan_id, decision == "approved", comment, reject_reason,
            expected_version, actor)
        self.repository.append_audit("plan_review", PLAN_ENTITY, plan["item_id"], actor, {
            "plan_id": plan_id, "version": plan["version"], "decision": decision,
            "reject_reason": reject_reason,
        })
        return self.plan_detail(plan_id, role)

    def _audit_invalidated(self, item_id: int, invalidated: Dict[str, Any],
                           actor: str, reason_kind: str) -> None:
        self.repository.append_audit("plan_invalidated", PLAN_ENTITY, item_id, actor, {
            "plan_id": invalidated["id"], "version": invalidated["version"],
            "invalid_reason": invalidated.get("invalid_reason"),
            "item_regressed_to_assessed": bool(invalidated.get("regressed")),
            "reason_kind": reason_kind,
        })

    def _plan_dto(self, plan: Dict[str, Any], current_version: int) -> Dict[str, Any]:
        result = dict(plan)
        result["status_label"] = PLAN_STATUS_LABELS.get(plan["status"], plan["status"])
        result["is_current"] = plan["version"] == current_version
        result["blocking_reason"] = plan_blocking_reason(plan) if result["is_current"] else None
        result["basis_record_ids"] = self.repository.plan_basis_ids(plan["id"])
        return result

    def plan_detail(self, plan_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        plan = self.repository.get_plan(plan_id)
        latest = self.repository.get_latest_plan(plan["item_id"])
        return self._plan_dto(plan, latest["version"])

    def list_plans(self, item_id: int, role: str) -> List[Dict[str, Any]]:
        self._view(role)
        plans = self.repository.list_plans(item_id)
        current_version = plans[-1]["version"] if plans else 0
        return [self._plan_dto(plan, current_version) for plan in plans]

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

    def enrich(self, item: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(item)
        result["priority"] = priority_score(
            item["severity"], item["quantity"], item["threshold"])
        result["deadline_hours"] = response_deadline_hours(
            item["severity"], item["quantity"], item["threshold"])
        result["escalation_required"] = escalation_required(
            item["severity"], item["quantity"], item["threshold"])
        result["reinforcement"] = self.plan_gate(item["id"])
        return result

    def plan_gate(self, item_id: int) -> Dict[str, Any]:
        """列表与详情中统一展示当前版本、复核人与阻挡原因。"""
        plan = self.repository.get_latest_plan(item_id)
        if plan is None:
            return {"current_version": None, "plan_status": None,
                    "plan_status_label": None, "reviewed_by": None,
                    "blocking_reason": design_gate_reason(None),
                    "design_unlocked": False}
        return {
            "plan_id": plan["id"],
            "current_version": plan["version"],
            "plan_status": plan["status"],
            "plan_status_label": PLAN_STATUS_LABELS.get(plan["status"], plan["status"]),
            "reviewed_by": plan["reviewed_by"],
            "submitted_by": plan["submitted_by"],
            "reject_reason": plan["reject_reason"],
            "invalid_reason": plan["invalid_reason"],
            "blocking_reason": plan_blocking_reason(plan),
            "design_unlocked": plan["status"] == PLAN_APPROVED,
        }
