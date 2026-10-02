"""特殊教育支持计划合规领域规则与状态转换。"""
from typing import Any, Dict, Iterable, List, Tuple

from .domain import Actor, Conflict, ValidationError, boolean, choice, integer, number, optional_text, text, text_list


INITIAL_STATE = "draft"
CREATE_ROLES = {'case_manager'}
ACTION_ROLES = {'consent': {'parent_rep'}, 'activate': {'case_manager'}, 'log_service': {'case_manager', 'specialist'}, 'confirm_service': {'case_manager', 'specialist'}, 'review': {'administrator'}, 'amend': {'case_manager'}, 'close': {'administrator'}}
TRANSITIONS = {'consent': {'draft': 'consented'}, 'activate': {'consented': 'active'}, 'log_service': {'active': 'active'}, 'confirm_service': {'active': 'active'}, 'review': {'active': 'under_review'}, 'amend': {'under_review': 'active'}, 'close': {'active': 'closed', 'under_review': 'closed'}}


class DomainRules:
    INITIAL_STATE = INITIAL_STATE

    def known_role(self, role: str) -> bool:
        all_roles = set(CREATE_ROLES)
        for roles in ACTION_ROLES.values():
            all_roles.update(roles)
        return role == "admin" or role in all_roles

    def role_can_create(self, role: str) -> bool:
        return role == "admin" or role in CREATE_ROLES

    def role_can_action(self, role: str, action: str) -> bool:
        return role == "admin" or role in ACTION_ROLES.get(action, set())

    def validate_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = dict(payload)
        text(p, "student_id")
        text(p, "disability")
        integer(p, "service_minutes", 1)
        integer(p, "delivered_minutes", 0)
        integer(p, "review_due_days", 0)
        integer(p, "goals_count", 1)
        boolean(p, "consent")
        if p["delivered_minutes"] > p["service_minutes"]:
            raise ValidationError("已提供服务不能超过计划服务")
        return p

    def prepare_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = self.validate_create(payload)
        p["basis_version"] = 1
        p["baseline_minutes"] = int(p["delivered_minutes"])
        p["service_log"] = []
        p["log_seq"] = 0
        p["review_stale"] = False
        p["review_overdue"] = int(p["review_due_days"]) <= 0
        p["plan_status"] = "draft"
        self._recount(p)
        return p

    def check_create_conflicts(self, payload: Dict[str, Any], existing: Iterable[Dict[str, Any]]) -> None:
        for item in existing:
            if item["state"] in {"active", "under_review", "consented"} and item["payload"].get("student_id") == payload.get("student_id"):
                raise Conflict("该学生已有有效的支持计划")

    def require_transition(self, record: Dict[str, Any], action: str) -> str:
        allowed = TRANSITIONS.get(action, {}).get(record["state"])
        if allowed is None:
            raise Conflict("当前状态不允许执行%s" % action)
        return allowed

    def _recount(self, p: Dict[str, Any]) -> None:
        """按基线分钟与有效服务记录重算履约合计、缺口与合规率。"""
        delivered = int(p.get("baseline_minutes", 0))
        for entry in p.get("service_log", []):
            if entry.get("status") == "valid":
                delivered += int(entry["session_minutes"])
        p["delivered_minutes"] = delivered
        p["missing_minutes"] = max(0, int(p["service_minutes"]) - delivered)
        p["compliance_rate"] = round(delivered / int(p["service_minutes"]) * 100, 2)

    def _parse_service_entries(self, p: Dict[str, Any], data: Dict[str, Any]) -> List[Dict[str, Any]]:
        batch_id = optional_text(data, "batch_id")
        raw = data.get("entries")
        entries: List[Dict[str, Any]] = []
        if raw is not None:
            if not isinstance(raw, list) or not raw:
                raise ValidationError("entries必须是非空列表")
            for item in raw:
                if not isinstance(item, dict):
                    raise ValidationError("entries元素必须是对象")
                entries.append({
                    "entry_id": text(item, "entry_id"),
                    "session_minutes": integer(item, "session_minutes", 1),
                    "provider": text(item, "provider"),
                    "batch_id": batch_id,
                })
        else:
            entries.append({
                "entry_id": optional_text(data, "entry_id") or "auto-%d" % (int(p.get("log_seq", 0)) + 1),
                "session_minutes": integer(data, "session_minutes", 1),
                "provider": text(data, "provider"),
                "batch_id": batch_id,
            })
        return entries

    def _apply_log_service(self, p: Dict[str, Any], data: Dict[str, Any], changes: Dict[str, Any], extra: Dict[str, Any]) -> str:
        entries = self._parse_service_entries(p, data)
        known_ids = {item["entry_id"] for item in p.get("service_log", [])}
        log = [dict(item) for item in p.get("service_log", [])]
        seq = int(p.get("log_seq", 0))
        delivered = int(p["delivered_minutes"])
        applied: List[str] = []
        skipped: List[str] = []
        providers: Dict[str, str] = {}
        for entry in entries:
            providers[entry["entry_id"]] = entry["provider"]
            if entry["entry_id"] in known_ids:
                skipped.append(entry["entry_id"])
                continue
            if delivered + entry["session_minutes"] > int(p["service_minutes"]):
                raise ValidationError("记录服务超过计划分钟数")
            seq += 1
            item = {
                "entry_id": entry["entry_id"],
                "session_minutes": entry["session_minutes"],
                "provider": entry["provider"],
                "basis_version": int(p.get("basis_version", 1)),
                "quota": int(p["service_minutes"]),
                "confirmed": False,
                "status": "valid",
                "seq": seq,
            }
            if entry.get("batch_id"):
                item["batch_id"] = entry["batch_id"]
            log.append(item)
            known_ids.add(entry["entry_id"])
            delivered += entry["session_minutes"]
            applied.append(entry["entry_id"])
        changes["service_log"] = log
        changes["log_seq"] = seq
        if applied:
            changes["last_provider"] = providers[applied[-1]]
        extra["applied_entries"] = applied
        extra["skipped_duplicates"] = skipped
        return "服务记录已登记：新增%d条，跳过重复%d条" % (len(applied), len(skipped))

    def _apply_confirm_service(self, p: Dict[str, Any], data: Dict[str, Any], changes: Dict[str, Any], extra: Dict[str, Any]) -> str:
        confirm_all = boolean(data, "confirm_all", False)
        wanted = text_list(data, "entry_ids", 0) if data.get("entry_ids") is not None else []
        if not confirm_all and not wanted:
            raise ValidationError("请提供entry_ids或confirm_all")
        log = [dict(item) for item in p.get("service_log", [])]
        by_id = {item["entry_id"]: item for item in log}
        missing = [entry_id for entry_id in wanted if entry_id not in by_id]
        if missing:
            raise ValidationError("服务记录不存在：%s" % ",".join(sorted(missing)))
        for entry_id in wanted:
            if by_id[entry_id].get("status") != "valid":
                raise ValidationError("服务记录%s已失效，不能确认" % entry_id)
        confirmed: List[str] = []
        for item in log:
            if item.get("confirmed") or item.get("status") != "valid":
                continue
            if confirm_all or item["entry_id"] in wanted:
                item["confirmed"] = True
                item["confirmed_basis_version"] = int(p.get("basis_version", 1))
                confirmed.append(item["entry_id"])
        changes["service_log"] = log
        extra["confirmed_entries"] = confirmed
        return "服务记录已确认：%d条" % len(confirmed)

    def _rebase_unconfirmed(self, p: Dict[str, Any], new_quota: int, new_basis: int) -> Tuple[List[Dict[str, Any]], List[str]]:
        """服务时长改动后，未确认记录失效并按新额度重新计算；已确认记录保留当时快照。"""
        log = [dict(item) for item in p.get("service_log", [])]
        running = int(p.get("baseline_minutes", 0))
        for item in log:
            if item.get("confirmed"):
                running += int(item["session_minutes"])
        invalidated: List[str] = []
        for item in sorted(log, key=lambda entry: int(entry.get("seq", 0))):
            if item.get("confirmed"):
                continue
            if running + int(item["session_minutes"]) <= new_quota:
                item["status"] = "valid"
                item["basis_version"] = new_basis
                item["quota"] = new_quota
                item.pop("invalidated_reason", None)
                item.pop("invalidated_basis_version", None)
                running += int(item["session_minutes"])
            else:
                item["status"] = "invalid"
                item["invalidated_reason"] = "超出新服务额度"
                item["invalidated_basis_version"] = new_basis
                invalidated.append(item["entry_id"])
        return log, invalidated

    def _apply_amend(self, p: Dict[str, Any], data: Dict[str, Any], changes: Dict[str, Any], extra: Dict[str, Any]) -> str:
        changes["amendment_reason"] = text(data, "amendment_reason")
        changes["updated_goals"] = text_list(data, "updated_goals", 1)
        changes["goals_count"] = len(changes["updated_goals"])
        changes["plan_status"] = "active"
        summary = "计划已修订"
        if data.get("service_minutes") is not None:
            new_quota = integer(data, "service_minutes", 1)
            old_basis = int(p.get("basis_version", 1))
            if new_quota != int(p["service_minutes"]):
                new_basis = old_basis + 1
                changes["service_minutes"] = new_quota
                changes["basis_version"] = new_basis
                log, invalidated = self._rebase_unconfirmed(p, new_quota, new_basis)
                changes["service_log"] = log
                extra["basis_change"] = {"from": old_basis, "to": new_basis, "service_minutes": new_quota, "invalidated_entries": invalidated}
                conclusion = p.get("review_conclusion")
                if isinstance(conclusion, dict) and int(conclusion.get("basis_version", 1)) < new_basis:
                    changes["review_stale"] = True
                    changes["superseded_review"] = conclusion
                summary = "计划已修订，依据版本升至v%d" % new_basis
        return summary

    def apply_action(self, record: Dict[str, Any], action: str, data: Dict[str, Any]) -> Tuple[str, Dict[str, Any], str, Dict[str, Any]]:
        new_state = self.require_transition(record, action)
        data = dict(data or {})
        p = dict(record["payload"])
        changes: Dict[str, Any] = {}
        extra: Dict[str, Any] = {}
        summary = ""
        if action == "consent":
            if not boolean(data, "guardian_confirmed"):
                raise ValidationError("监护人尚未确认")
            if not text(data, "consent_scope"):
                raise ValidationError("同意范围不能为空")
            changes["consent"] = True
            changes["consent_scope"] = data["consent_scope"]
            changes["consent_basis_version"] = int(p.get("basis_version", 1))
            summary = "监护人同意已记录"
        elif action == "activate":
            if not p.get("consent"):
                raise ValidationError("缺少有效同意")
            if int(p["goals_count"]) <= 0:
                raise ValidationError("计划必须包含目标")
            changes["plan_status"] = "active"
            summary = "支持计划生效"
        elif action == "log_service":
            summary = self._apply_log_service(p, data, changes, extra)
        elif action == "confirm_service":
            summary = self._apply_confirm_service(p, data, changes, extra)
        elif action == "review":
            note = text(data, "progress_note")
            changes["progress_note"] = note
            changes["review_overdue"] = False
            changes["review_stale"] = False
            changes["review_conclusion"] = {
                "note": note,
                "basis_version": int(p.get("basis_version", 1)),
                "service_minutes": int(p["service_minutes"]),
                "delivered_minutes": int(p["delivered_minutes"]),
                "compliance_rate": p.get("compliance_rate", 0),
            }
            summary = "进入计划复查"
        elif action == "amend":
            summary = self._apply_amend(p, data, changes, extra)
        elif action == "close":
            if not boolean(data, "review_complete"):
                raise ValidationError("复查尚未完成")
            changes["plan_status"] = "closed"
            summary = "支持计划结束"
        p.update(changes)
        if action in {"log_service", "confirm_service", "amend"}:
            self._recount(p)
        return new_state, p, summary or ("已执行%s" % action), extra
