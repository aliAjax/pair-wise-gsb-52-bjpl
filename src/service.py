"""业务用例编排、权限检查与审计。"""
from typing import Any, Dict, List, Optional

from .audit import AuditRecorder
from .domain import Actor, Conflict, PermissionDenied, text
from .repository import Repository
from .rules import DomainRules


class Service:
    def __init__(self, repository: Repository, rules: DomainRules, audit: AuditRecorder = None) -> None:
        self.repository = repository
        self.rules = rules
        self.audit = audit or AuditRecorder(repository)

    @staticmethod
    def _actor(actor: Actor) -> Actor:
        if actor is None or not actor.user_id.strip() or not actor.role.strip():
            raise PermissionDenied("缺少调用身份")
        return actor

    def _ensure_known_role(self, actor: Actor) -> None:
        if not self.rules.known_role(actor.role):
            raise PermissionDenied("角色无权访问该服务")

    @staticmethod
    def _current_basis(record: Dict[str, Any]) -> Dict[str, Any]:
        payload = record.get("payload", {})
        return {
            "current_version": record.get("version"),
            "current_state": record.get("state"),
            "current_basis_version": int(payload.get("basis_version", 1)),
            "current_service_minutes": int(payload.get("service_minutes", 0)),
        }

    def create(self, actor: Actor, reference: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_create(actor.role):
            raise PermissionDenied("角色无权创建记录")
        reference = text({"reference": reference}, "reference")
        prepared = self.rules.prepare_create(payload or {})
        self.rules.check_create_conflicts(prepared, self.repository.list_records(limit=500))
        return self.repository.create(reference, self.rules.INITIAL_STATE, prepared, actor.user_id)

    def list_records(self, actor: Actor, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_records(state=state, limit=limit)

    def get_record(self, actor: Actor, record_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.get(record_id)

    def act(self, actor: Actor, record_id: int, expected_version: int, action: str, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        action = text({"action": action}, "action")
        if not self.rules.role_can_action(actor.role, action):
            raise PermissionDenied("角色无权执行该操作")
        record = self.repository.get(record_id)
        try:
            self.rules.require_transition(record, action)
        except Conflict as exc:
            raise Conflict(str(exc), details=self._current_basis(record)) from exc
        new_state, new_payload, summary, extra = self.rules.apply_action(record, action, data or {})
        details = {"summary": summary, "input": data or {}, "from": record["state"], "to": new_state, "basis_version": int(new_payload.get("basis_version", 1))}
        details.update(extra)
        return self.repository.mutate(
            record_id=record_id,
            expected_version=int(expected_version),
            state=new_state,
            payload=new_payload,
            actor_id=actor.user_id,
            action=action,
            details=details,
        )

    def timeline(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.audit.timeline(record_id)

    def stats(self, actor: Actor) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        records = self.repository.list_records(limit=500)
        states: Dict[str, int] = {}
        basis_versions: Dict[str, int] = {}
        items: List[Dict[str, Any]] = []
        for record in records:
            states[record["state"]] = states.get(record["state"], 0) + 1
            basis = int(record["payload"].get("basis_version", 1))
            basis_versions[str(basis)] = basis_versions.get(str(basis), 0) + 1
            items.append({"id": record["id"], "reference": record["reference"], "state": record["state"], "version": record["version"], "basis_version": basis})
        return {"states": states, "basis_versions": basis_versions, "records": items}
