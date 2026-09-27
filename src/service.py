from __future__ import annotations

from typing import Any, Dict, Optional

from .domain import (ConflictError, ValidationError, ensure_role,
                     normalize_severity, require_number, require_text)
from .repository import Repository
from .rules import (AUDIT_ROLES, CREATE_ROLES, ENTITY, PLAN_ENTITY,
                    PLAN_INVALIDATE_NEW_VERSION, PLAN_INVALIDATE_RECORD_AMENDED,
                    PLAN_REVIEW_ROLES, PLAN_SUBMIT_ROLES, RECORD_ROLES, TITLE,
                    VIEW_ROLES, design_blockers, escalation_required,
                    normalize_decision, priority_score, response_deadline_hours,
                    role_for_transition, transition_blockers,
                    validate_plan_submission, validate_review, validate_transition)


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

    def amend_record(self, item_id: int, record_id: int, payload: Dict[str, Any],
                     actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, RECORD_ROLES)
        actor = require_text(actor, "actor", 100)
        existing = self.repository.get_record(item_id, record_id)
        has_detail = "detail" in payload
        has_status = "status" in payload
        if not has_detail and not has_status:
            raise ValidationError("补改必须包含detail或status")
        detail = require_text(payload["detail"], "detail") if has_detail \
            else existing["detail"]
        status = existing["status"]
        if has_status:
            status = payload["status"]
            if status not in ("open", "closed"):
                raise ValidationError("status必须是open或closed")
        record, invalidated = self.repository.amend_record(
            item_id, record_id, detail, status, actor)
        self.repository.append_audit("record_amend", ENTITY, item_id, actor, {
            "record_id": record_id, "version": record["version"], "status": status,
        })
        for plan_id in invalidated:
            self.repository.append_audit("plan_invalidate", PLAN_ENTITY, plan_id,
                                         actor, {
                                             "item_id": item_id,
                                             "record_id": record_id,
                                             "reason": PLAN_INVALIDATE_RECORD_AMENDED,
                                         })
        return {"record": record, "invalidated_plan_ids": invalidated}

    def submit_plan(self, item_id: int, payload: Dict[str, Any], actor: str,
                    role: str) -> Dict[str, Any]:
        ensure_role(role, PLAN_SUBMIT_ROLES)
        actor = require_text(actor, "actor", 100)
        content = require_text(payload.get("content"), "content")
        record_ids = payload.get("record_ids")
        if not isinstance(record_ids, list) or not record_ids:
            raise ValidationError("record_ids必须是非空数组")
        normalized: list = []
        for raw in record_ids:
            if isinstance(raw, bool) or not isinstance(raw, int) or raw < 1:
                raise ValidationError("record_ids必须是正整数")
            if raw not in normalized:
                normalized.append(raw)
        item = self.repository.get_item(item_id)
        validate_plan_submission(item["status"])
        records = {row["id"]: row
                   for row in self.repository.get_records_by_ids(item_id, normalized)}
        missing = [str(rid) for rid in normalized if rid not in records]
        if missing:
            raise ValidationError("评估事项不存在或不属于该项目：" + ",".join(missing))
        open_ids = [str(rid) for rid in normalized
                    if records[rid]["status"] != "closed"]
        if open_ids:
            raise ConflictError("依据的评估事项未关闭：" + ",".join(open_ids))
        refs = [(rid, records[rid]["version"]) for rid in normalized]
        plan, invalidated = self.repository.create_plan(item_id, content, refs, actor)
        self.repository.append_audit("plan_submit", PLAN_ENTITY, plan["id"], actor, {
            "item_id": item_id, "version_no": plan["version_no"],
            "record_ids": normalized,
        })
        for plan_id in invalidated:
            self.repository.append_audit("plan_invalidate", PLAN_ENTITY, plan_id,
                                         actor, {
                                             "item_id": item_id,
                                             "reason": PLAN_INVALIDATE_NEW_VERSION,
                                         })
        return self.plan_view(plan)

    def review_plan(self, plan_id: int, payload: Dict[str, Any], actor: str,
                    role: str) -> Dict[str, Any]:
        ensure_role(role, PLAN_REVIEW_ROLES)
        actor = require_text(actor, "actor", 100)
        decision = normalize_decision(payload.get("decision"))
        reason = payload.get("reason")
        if reason is not None:
            reason = require_text(reason, "reason", 1000)
        validate_review(decision, reason)
        plan = self.repository.get_plan(plan_id)
        if plan["status"] != "pending":
            raise ConflictError("方案当前状态不允许评审")
        if decision == "approved" \
                and self.repository.plan_stale_record_count(plan_id) > 0:
            raise ConflictError("依据的评估事项已补改，请重新提交方案")
        updated = self.repository.review_plan(plan_id, decision, reason, actor)
        self.repository.append_audit("plan_review", PLAN_ENTITY, plan_id, actor, {
            "item_id": updated["item_id"], "version_no": updated["version_no"],
            "decision": decision, "reason": reason,
        })
        return self.plan_view(updated)

    def transition(self, item_id: int, target: str, expected_version: int,
                   actor: str, role: str) -> Dict[str, Any]:
        actor = require_text(actor, "actor", 100)
        item = self.repository.get_item(item_id)
        validate_transition(item["status"], target)
        ensure_role(role, role_for_transition(target))
        if not isinstance(expected_version, int) or expected_version < 1:
            raise ValueError("expected_version必须是正整数")
        blockers = transition_blockers(target,
                                       self.repository.open_record_count(item_id),
                                       self.repository.latest_plan(item_id))
        if blockers:
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

    def list_plans(self, item_id: int, role: str) -> list:
        self._view(role)
        return [self.plan_view(plan) for plan in self.repository.list_plans(item_id)]

    def get_plan(self, plan_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        return self.plan_view(self.repository.get_plan(plan_id))

    def audit(self, role: str, item_id: Optional[int] = None) -> list:
        ensure_role(role, AUDIT_ROLES)
        return self.repository.list_audit(item_id)

    def plan_view(self, plan: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(plan)
        records = self.repository.plan_records(plan["id"])
        for record in records:
            record["amended"] = (record["current_version"] != record["record_version"]
                                 or record["status"] != "closed")
        result["records"] = records
        result["stale"] = any(record["amended"] for record in records)
        return result

    def enrich(self, item: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(item)
        result["priority"] = priority_score(
            item["severity"], item["quantity"], item["threshold"])
        result["deadline_hours"] = response_deadline_hours(
            item["severity"], item["quantity"], item["threshold"])
        result["escalation_required"] = escalation_required(
            item["severity"], item["quantity"], item["threshold"])
        latest = self.repository.latest_plan(item["id"])
        result["plan_version"] = latest["version_no"] if latest else None
        result["plan_status"] = latest["status"] if latest else None
        result["plan_reviewed_by"] = latest["reviewed_by"] if latest else None
        result["design_blockers"] = design_blockers(latest) \
            if item["status"] == "assessed" else []
        return result
