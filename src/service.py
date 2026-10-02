"""业务用例编排、权限检查、依据版本审计与批次恢复。"""
from typing import Any, Dict, List, Optional

from .audit import AuditRecorder
from .domain import Actor, Conflict, PermissionDenied, ValidationError, integer, text
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
        self.rules.require_transition(record, action)
        event: Dict[str, Any] = {"plan_version": int(record["payload"].get("plan_version", 1))}
        new_state, new_payload, summary = self.rules.apply_action(record, action, data or {}, event)
        return self.repository.mutate(
            record_id=record_id,
            expected_version=int(expected_version),
            state=new_state,
            payload=new_payload,
            actor_id=actor.user_id,
            action=action,
            details={
                "summary": summary,
                "input": data or {},
                "from": record["state"],
                "to": new_state,
                # 每条审计都能看到该步依据的计划版本与服务时长快照。
                "basis": {
                    "plan_version": int(record["payload"].get("plan_version", 1)),
                    "service_minutes": record["payload"].get("service_minutes"),
                    "record_version": int(record["version"]),
                },
                "event": event,
            },
        )

    # ------------------------------------------------------------------
    # 服务批次：失败后用完整批次恢复，只补未完成条目，绝不重复累计分钟。
    # ------------------------------------------------------------------
    def submit_service_batch(
        self,
        actor: Actor,
        record_id: int,
        batch_id: str,
        expected_version: int,
        items: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        batch_id = text({"batch_id": batch_id}, "batch_id")
        if not isinstance(items, list) or not items:
            raise ValidationError("items必须是非空列表")
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                raise ValidationError("items[%d]必须是对象" % index)
            integer(item, "session_minutes", 1)
            text(item, "provider")
            ref = item.get("ref")
            if ref is not None and (not isinstance(ref, str) or not ref.strip()):
                raise ValidationError("items[%d].ref必须是文本" % index)
        if len({item.get("ref") or ("item-%d" % i) for i, item in enumerate(items)}) != len(items):
            raise ValidationError("批次内ref不能重复")

        # 幂等重放：已完成批次直接回放；失败/中断批次用完整批次续跑，只补未完成条目。
        existing = self.repository.get_batch(batch_id)
        if existing is not None:
            if int(existing["record_id"]) != int(record_id):
                raise Conflict("batch_id已用于其他计划")
            if existing["state"] == "completed":
                return self._batch_response(existing, recovered=True)
            record = self.repository.get(record_id)
            applied_keys = self._run_batch(
                actor, record_id, int(record["version"]), batch_id, items, existing, int(existing["id"])
            )
            return self._batch_response(self.repository.get_batch(batch_id), recovered=True, applied_keys=applied_keys)

        # 先校验状态与权限，再持久化批次。
        self.rules.require_transition(self.repository.get(record_id), "log_service")

        batch = self.repository.create_batch(batch_id, record_id, int(expected_version), len(items), actor.user_id)
        batch_pk = int(batch["id"])
        applied_keys = self._run_batch(actor, record_id, int(expected_version), batch_id, items, batch, batch_pk)
        latest = self.repository.get_batch(batch_id)
        return self._batch_response(latest, recovered=False, applied_keys=applied_keys)

    def _run_batch(self, actor, record_id, expected_version, batch_id, items, batch, batch_pk):
        current_version = expected_version
        applied_keys: List[str] = []
        for index, item in enumerate(items):
            item_ref = item.get("ref") or ("item-%d" % index)
            key = "%s#%s" % (batch_id, item_ref)
            # 只补未完成条目：批次已记录的键直接跳过。
            if key in batch["applied_keys"]:
                applied_keys.append(key)
                continue
            data = {
                "session_minutes": integer(item, "session_minutes", 1),
                "provider": text(item, "provider"),
                "ref": item_ref,
                "batch_id": batch_id,
            }
            try:
                self.act(actor, record_id, current_version, "log_service", data)
            except Conflict as exc:
                # 写入其实已成功、但批次状态未推进（崩溃在提交与记账之间）：
                # 条目已在记录中，按幂等跳过，且不消耗版本号。
                record = self.repository.get(record_id)
                if any(entry["key"] == key for entry in record["payload"].get("services", [])):
                    pass
                else:
                    self.repository.mark_batch_item(batch_pk, "failed", len(applied_keys), applied_keys, str(exc))
                    raise
            else:
                current_version += 1
            applied_keys.append(key)
            # 以记录中的真实键集合为准回填，防止“已落库未记账”的漂移。
            record = self.repository.get(record_id)
            present = {entry["key"] for entry in record["payload"].get("services", [])}
            applied_keys = [k for k in applied_keys if k in present]
            self.repository.mark_batch_item(batch_pk, "processing", len(applied_keys), applied_keys)
        self.repository.complete_batch(batch_pk)
        return applied_keys

    def _batch_response(self, batch: Dict[str, Any], recovered: bool, applied_keys: List[str] = None) -> Dict[str, Any]:
        keys = applied_keys if applied_keys is not None else batch["applied_keys"]
        finished = batch["state"] == "completed"
        return {
            "batch_id": batch["batch_id"],
            "record_id": int(batch["record_id"]),
            "state": batch["state"],
            "total_items": int(batch["total_items"]),
            "applied_items": len(keys),
            "recovered": bool(recovered),
            "completed": finished,
            "message": "批次已完成" if finished else "批次执行失败，请用完整批次重试以恢复",
            "applied_keys": keys,
            "last_error": batch["last_error"],
        }

    def get_batch(self, actor: Actor, batch_id: str) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        batch = self.repository.get_batch(batch_id)
        if batch is None:
            from .domain import NotFound
            raise NotFound("批次不存在")
        return self._batch_response(batch, recovered=True)

    def timeline(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.audit.timeline(record_id)

    def stats(self, actor: Actor) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        overview = self.repository.stats()
        records = self.repository.list_records(limit=500)
        # 统计中展示每条记录采用的计划版本与服务依据。
        records_basis = []
        for record in records:
            payload = record["payload"]
            services = {
                "confirmed": sum(1 for e in payload.get("services", []) if e["status"] == "confirmed"),
                "pending": sum(1 for e in payload.get("services", []) if e["status"] == "pending"),
                "void": sum(1 for e in payload.get("services", []) if e["status"] == "void"),
                "versions": sorted({
                    int(e.get("confirmed_basis_version") or e.get("basis_version") or 1)
                    for e in payload.get("services", [])
                }),
            }
            records_basis.append({
                "id": record["id"],
                "reference": record["reference"],
                "state": record["state"],
                "record_version": record["version"],
                "plan_version": payload.get("plan_version", 1),
                "service_minutes": payload.get("service_minutes"),
                "consent_basis_version": payload.get("consent_basis_version"),
                "review_status": payload.get("review_status", "none"),
                "latest_review_version": payload["reviews"][-1]["plan_version"] if payload.get("reviews") else None,
                "services": services,
            })
        overview["records"] = records_basis
        return overview
