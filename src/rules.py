"""特殊教育支持计划合规领域规则与状态转换。

依据链：支持计划(plan_version) -> 监护人同意(consent_basis_version)
-> 服务履约(services[*].basis_version / confirmed_basis_version)
-> 复查(reviews[*].plan_version)。

- 服务时长调整(adjust_plan)会使所有“未确认”的服务条目失效并按新额度重新计算；
  “已确认”条目保留确认当时的计划快照，不回溯、不改写。
- 复查结论按复查当时的版本固化；之后计划再改动，原结论继续可见，只标记为 stale。
"""
from typing import Any, Dict, Iterable, List, Optional, Tuple
import uuid

from .domain import Conflict, ValidationError, boolean, integer, text, text_list


INITIAL_STATE = "draft"
CREATE_ROLES = {'case_manager'}
ACTION_ROLES = {
    'consent': {'parent_rep'},
    'activate': {'case_manager'},
    'log_service': {'case_manager', 'specialist'},
    'confirm_services': {'case_manager', 'specialist'},
    'adjust_plan': {'case_manager'},
    'review': {'administrator'},
    'recheck': {'administrator'},
    'amend': {'case_manager'},
    'close': {'administrator'},
}
TRANSITIONS = {
    'consent': {'draft': 'consented'},
    'activate': {'consented': 'active'},
    'log_service': {'active': 'active', 'under_review': 'under_review'},
    'confirm_services': {'active': 'active', 'under_review': 'under_review'},
    'adjust_plan': {'active': 'active', 'under_review': 'under_review'},
    # 复查允许在 active 发起，也允许在 under_review 依据过期后用新版本重新复查。
    'review': {'active': 'under_review', 'under_review': 'under_review'},
    'recheck': {'under_review': 'under_review', 'active': 'under_review'},
    'amend': {'under_review': 'active'},
    'close': {'active': 'closed', 'under_review': 'closed'},
}


def _service_totals(p: Dict[str, Any]) -> Tuple[int, int, int]:
    confirmed = pending = voided = 0
    for entry in p.get("services", []):
        minutes = int(entry["minutes"])
        status = entry["status"]
        if status == "confirmed":
            confirmed += minutes
        elif status == "pending":
            pending += minutes
        else:
            voided += minutes
    return confirmed, pending, voided


def recompute(p: Dict[str, Any]) -> Dict[str, Any]:
    """按各条目当前状态与所采用的计划版本，重新计算派生指标。"""
    quota = int(p["service_minutes"])
    confirmed, pending, voided = _service_totals(p)
    delivered = confirmed + pending
    p["confirmed_minutes"] = confirmed
    p["pending_minutes"] = pending
    p["voided_minutes"] = voided
    p["delivered_minutes"] = delivered
    p["missing_minutes"] = max(0, quota - delivered)
    p["compliance_rate"] = round(delivered / quota * 100, 2) if quota else 0.0
    return p


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
        p["plan_version"] = 1
        p["services"] = []
        p["reviews"] = []
        p["adjustments"] = []
        p["consent_basis_version"] = None
        p["activation_basis_version"] = None
        p["review_status"] = "none"
        # 建账时导入的已履约分钟视为在 v1 快照下已确认的历史记录。
        if int(p["delivered_minutes"]) > 0:
            p["services"].append({
                "key": "import@v1",
                "ref": "import",
                "provider": "建账导入",
                "minutes": int(p["delivered_minutes"]),
                "status": "confirmed",
                "basis_version": 1,
                "basis_minutes": int(p["service_minutes"]),
                "confirmed_basis_version": 1,
                "confirmed_basis_minutes": int(p["service_minutes"]),
            })
        recompute(p)
        p["review_overdue"] = int(p["review_due_days"]) <= 0
        p["plan_status"] = "draft"
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

    @staticmethod
    def _service_keys(p: Dict[str, Any]) -> set:
        return {entry["key"] for entry in p.get("services", [])}

    def apply_action(self, record: Dict[str, Any], action: str, data: Dict[str, Any], event: Optional[Dict[str, Any]] = None) -> Tuple[str, Dict[str, Any], str]:
        new_state = self.require_transition(record, action)
        data = dict(data or {})
        p = dict(record["payload"])
        changes: Dict[str, Any] = {}
        summary = ""
        if action == "consent":
            if not boolean(data, "guardian_confirmed"):
                raise ValidationError("监护人尚未确认")
            if not text(data, "consent_scope"):
                raise ValidationError("同意范围不能为空")
            changes["consent"] = True
            changes["consent_scope"] = data["consent_scope"]
            # 同意挂接到当时的计划版本。
            changes["consent_basis_version"] = int(p["plan_version"])
            summary = "监护人同意已记录（依据计划v%s）" % p["plan_version"]
        elif action == "activate":
            if not p.get("consent"):
                raise ValidationError("缺少有效同意")
            if int(p["goals_count"]) <= 0:
                raise ValidationError("计划必须包含目标")
            changes["plan_status"] = "active"
            changes["activation_basis_version"] = int(p["plan_version"])
            summary = "支持计划生效（依据计划v%s）" % p["plan_version"]
        elif action in ("log_service", "confirm_services", "adjust_plan", "review", "recheck"):
            if action == "log_service":
                session = integer(data, "session_minutes", 1)
                provider = text(data, "provider")
                quota = int(p["service_minutes"])
                confirmed, pending, _ = _service_totals(p)
                if confirmed + pending + session > quota:
                    raise ValidationError("记录服务超过当前计划分钟数（依据计划v%s）" % p["plan_version"])
                batch_id = data.get("batch_id")
                item_ref = data.get("ref") or ("log-%d" % (len(p["services"]) + 1))
                if batch_id:
                    key = "%s#%s" % (batch_id, item_ref)
                else:
                    key = "log@%s" % uuid.uuid4().hex
                if key in self._service_keys(p):
                    # 幂等保护：同一批次键重复提交不重复累计。
                    raise Conflict("该服务条目已登记：%s" % key)
                entry = {
                    "key": key,
                    "ref": str(item_ref),
                    "provider": provider,
                    "minutes": session,
                    "status": "pending",
                    # 登记时先按当前额度判定，确认时再固化快照。
                    "basis_version": int(p["plan_version"]),
                    "basis_minutes": quota,
                    "confirmed_basis_version": None,
                    "confirmed_basis_minutes": None,
                }
                p.setdefault("services", []).append(entry)
                # 出现新的履约证据，基于旧快照的复查结论转为过期（结论保留）。
                if p.get("review_status") == "current":
                    for review in p["reviews"]:
                        if review["status"] == "current":
                            review["status"] = "stale"
                    p["review_status"] = "stale"
                recompute(p)
                summary = "服务记录已登记（待确认，依据计划v%s）" % p["plan_version"]
                if event is not None:
                    event["service_entry"] = {"key": key, "ref": entry["ref"], "minutes": session, "status": "pending", "basis_version": entry["basis_version"]}
            elif action == "confirm_services":
                refs = data.get("service_refs")
                if refs is not None and (not isinstance(refs, list) or any(not isinstance(r, str) or not r.strip() for r in refs)):
                    raise ValidationError("service_refs必须是文本列表")
                wanted = {r.strip() for r in refs} if refs else None
                pending_entries = [e for e in p["services"] if e["status"] == "pending" and (wanted is None or e["ref"] in wanted or e["key"] in wanted)]
                if wanted is not None:
                    pending_keys = {e["ref"] for e in p["services"] if e["status"] == "pending"}
                    pending_keys |= {e["key"] for e in p["services"] if e["status"] == "pending"}
                    unknown = wanted - pending_keys
                    if unknown:
                        raise ValidationError("待确认服务不存在或已处理：%s" % ",".join(sorted(unknown)))
                if not pending_entries:
                    raise ValidationError("没有可确认的待确认服务")
                for entry in pending_entries:
                    entry["status"] = "confirmed"
                    # 已确认记录保留确认当时的计划快照，之后调整不回溯。
                    entry["confirmed_basis_version"] = int(p["plan_version"])
                    entry["confirmed_basis_minutes"] = int(p["service_minutes"])
                if p.get("review_status") == "current":
                    for review in p["reviews"]:
                        if review["status"] == "current":
                            review["status"] = "stale"
                    p["review_status"] = "stale"
                recompute(p)
                summary = "服务记录已确认（%d条，快照计划v%s）" % (len(pending_entries), p["plan_version"])
                if event is not None:
                    event["confirmed"] = [{"key": e["key"], "confirmed_basis_version": e["confirmed_basis_version"], "confirmed_basis_minutes": e["confirmed_basis_minutes"]} for e in pending_entries]
            elif action == "adjust_plan":
                new_minutes = integer(data, "service_minutes", 1)
                reason = text(data, "reason")
                if new_minutes == int(p["service_minutes"]):
                    raise ValidationError("服务时长未发生变化")
                old_version = int(p["plan_version"])
                new_version = old_version + 1
                # 未确认的服务一律失效；已确认的保留当时快照。
                voided = []
                for entry in p["services"]:
                    if entry["status"] == "pending":
                        entry["status"] = "void"
                        entry["voided_at_version"] = new_version
                        entry["voided_reason"] = reason
                        voided.append(entry["key"])
                # 旧复查结论继续保留，只标记为过期。
                for review in p["reviews"]:
                    if review["status"] == "current":
                        review["status"] = "stale"
                if p.get("review_status") == "current":
                    p["review_status"] = "stale"
                p["service_minutes"] = new_minutes
                p["plan_version"] = new_version
                p.setdefault("adjustments", []).append({
                    "from_version": old_version,
                    "to_version": new_version,
                    "from_minutes": int(record["payload"]["service_minutes"]),
                    "to_minutes": new_minutes,
                    "reason": reason,
                    "voided_services": voided,
                })
                recompute(p)
                summary = "计划服务时长调整 v%s->v%s（%s分钟），%d条未确认服务失效并重算" % (
                    old_version, new_version, new_minutes, len(voided))
                if event is not None:
                    event["adjustment"] = {"from_version": old_version, "to_version": new_version, "to_minutes": new_minutes, "voided": voided}
            else:  # review / recheck
                note = text(data, "progress_note")
                quota = int(p["service_minutes"])
                confirmed, pending, _ = _service_totals(p)
                delivered = confirmed + pending
                conclusion = "达标" if delivered >= quota else "未达标"
                snapshot = {
                    "index": len(p["reviews"]) + 1,
                    "kind": "review" if action == "review" else "recheck",
                    "progress_note": note,
                    "plan_version": int(p["plan_version"]),
                    "consent_basis_version": p.get("consent_basis_version"),
                    "service_minutes": quota,
                    "confirmed_minutes": confirmed,
                    "pending_minutes": pending,
                    "delivered_minutes": delivered,
                    "conclusion": conclusion,
                    "status": "current",
                }
                for review in p["reviews"]:
                    if review["status"] == "current":
                        review["status"] = "superseded"
                p.setdefault("reviews", []).append(snapshot)
                p["review_status"] = "current"
                changes["review_overdue"] = False
                summary = "复查完成（依据计划v%s，结论：%s）" % (p["plan_version"], conclusion)
                if event is not None:
                    event["review"] = snapshot
        elif action == "amend":
            changes["amendment_reason"] = text(data, "amendment_reason")
            changes["updated_goals"] = text_list(data, "updated_goals", 1)
            changes["goals_count"] = len(changes["updated_goals"])
            changes["plan_status"] = "active"
            summary = "计划已修订"
        elif action == "close":
            if not boolean(data, "review_complete"):
                raise ValidationError("复查尚未完成")
            if not p.get("reviews"):
                raise ValidationError("缺少复查结论，不能结束计划")
            latest = p["reviews"][-1]
            if latest["status"] != "current" or latest["plan_version"] != int(p["plan_version"]):
                raise ValidationError("复查依据已过期，请按当前计划版本重新复查")
            changes["plan_status"] = "closed"
            summary = "支持计划结束（依据复查#%s/计划v%s）" % (latest["index"], latest["plan_version"])
        p.update(changes)
        return new_state, p, summary or ("已执行%s" % action)
