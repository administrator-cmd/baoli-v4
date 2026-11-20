"""人工复核裁决与依赖结果的重算。

复核分批（不可打乱）：存在字段确认待办时本批只包含字段事实，否则包含全部待办。判卷、提交与
裁决必须用同一集合，否则用户会先裁决一条即将被字段纠错失效的规则。材料类型待复核不参与这个
分批——它是独立事项，捆进字段批次会让两条互不相干的业务问题互相决定对方批次的哈希。

裁决后按实际事实引用使受影响的规则执行批次失效，保留人工确认值，重新执行生效规则。

失效一律保留历史：旧批次标 INVALIDATED，旧事实与旧复核仍可追溯，不原地清空。"""

from typing import Any

from sqlalchemy.orm import Session

from app.constants.execution import (
    FACT_KIND_DERIVED,
    FACT_KIND_EXTRACTED,
    NODE_RULE_EXECUTION,
    ORIGIN_DOCUMENT_EXTRACTION,
)
from app.constants.status import FIELD_STATUS_REVIEWED
from app.domain.configuration.manifest import enabled_extraction_field_keys
from app.domain.configuration.rule_library import active, execution_container_of
from app.modules.audit.execution.crud import AuditExecutionCRUD
from app.modules.audit.execution.service.execution_history import AuditExecutionHistoryService
from app.modules.audit.field.crud import AuditFieldCRUD
from app.modules.audit.field.service.value_store import AuditFieldValueStoreService
from app.modules.audit.rule.crud import AuditRuleCRUD
from app.modules.audit.shared import _current_upload_map, _task_manifest
from app.modules.audit.task.model import AuditTask
from app.modules.system.operation.crud import SystemOperationCRUD
from app.modules.system.operation.service.operation_types import OBJECT_AUDIT_TASK


class AuditFieldReviewsService:
    """本实体的服务方法；db 由构造时注入。"""

    def __init__(self, db: Session | None = None) -> None:
        self.db = db

    def _decision_command_no(self, task: AuditTask) -> str | None:
        """本次裁决对应的后台命令编号。

        裁决由后台操作驱动：取该任务当前 RUNNING 的操作号用于对账；取不到就写 None，
        既不编造命令号，也不把最近一次已完成的操作号当成本次命令。
        """
        operation = SystemOperationCRUD(self.db).running_for_object(
            object_type=OBJECT_AUDIT_TASK, object_no=task.task_no,
        )
        return operation.operation_no if operation else None


    def _invalidate_validation_results(self, task: AuditTask,
        manifest: dict[str, Any],
        changed_field_keys: set[str],
    ) -> dict[str, Any]:
        """字段事实改变后只失效实际引用它的节点及 DAG 后继，并撤销派生事实。

        旧批次的待复核项同时失效：新结果批次是否需要人工判断由新结果自己决定，
        旧复核项不能继续挂在待办里，也不能自动贴到新结果上。
        返回真实失效数量，供纠错接口如实回报影响范围。

        普通规则按 field_key 建立引用，计算规则按字段 source_ref 建立引用；报告会展示
        任务字段，因此启用了报告规则时也是字段的直接消费者。之后只沿 DAG 向后传播。
        """
        affected_codes = self._field_change_affected_node_codes(
            manifest,
            changed_field_keys,
            set(task.task_schema_json["enabled_rule_keys"]),
        )
        counts: dict[str, Any] = {
            "invalidated_executions": 0,
            "invalidated_rule_results": 0,
            "invalidated_reviews": 0,
            "affected_node_codes": sorted(affected_codes),
        }
        if not affected_codes:
            return counts
        execution_ids = AuditExecutionCRUD(self.db).ids_for_nodes(task.id, affected_codes)
        if not execution_ids:
            return counts
        counts.update(self._invalidate_execution_records( task, execution_ids))
        return counts


    def _invalidate_execution_records(self, task: AuditTask,
        execution_ids: list[int],
    ) -> dict[str, int]:
        """失效指定执行及其规则、字段待办和该批次产生的全部当前事实。"""
        counts = {"invalidated_executions": 0, "invalidated_rule_results": 0, "invalidated_reviews": 0}
        if not execution_ids:
            return counts
        rule_result_ids = AuditRuleCRUD(self.db).ids_for_executions(execution_ids)
        counts["invalidated_rule_results"] = len(rule_result_ids)
        field_value_ids = AuditFieldCRUD(self.db).field_value_ids_for_executions(task.id, execution_ids)
        if field_value_ids:
            counts["invalidated_reviews"] += AuditFieldCRUD(self.db).invalidate_pending_reviews_by_field_values(field_value_ids)
            AuditFieldCRUD(self.db).clear_field_values_by_ids(field_value_ids)
        # 执行批次既可能产生计算派生事实，也可能产生抽取统一事实。只撤销 DERIVED 会让
        # 已失效抽取节点的旧字段继续留在 TaskFact 当前位，后续规则仍能读取到它。
        AuditFieldCRUD(self.db).clear_current_facts(task.id, execution_ids=execution_ids)
        AuditFieldValueStoreService(self.db).sync_current(task)
        counts["invalidated_executions"] = AuditExecutionHistoryService(self.db).invalidate_by_ids(execution_ids, "字段纠错后重算")
        return counts


    def _field_change_affected_node_codes(self, manifest: dict[str, Any], changed_field_keys: set[str], enabled_rule_keys: set[str]) -> set[str]:
        """字段被本任务实际生效项引用时，重新遍历规则库执行批次。"""
        if not changed_field_keys:
            return set()
        for rule in manifest.get("rules") or []:
            if rule["rule_key"] not in enabled_rule_keys or not active(rule):
                continue
            referenced = {i["field_key"] for i in rule.get("inputs") or []} | set(rule.get("outputs") or [])
            if referenced & changed_field_keys or rule.get("kind") == "REPORT":
                return {NODE_RULE_EXECUTION}
        return set()


    def _next_node_for_recalculation(self, task: AuditTask, manifest: dict[str, Any], nodes: list[dict[str, Any]], *, completed_node_code: str | None = None) -> dict[str, Any] | None:
        """重新执行失效的规则批次，不寻找业务阶段或人工复核节点。"""
        AuditFieldCRUD(self.db).flush()
        completed = AuditExecutionCRUD(self.db).succeeded_node_exists(task.id, NODE_RULE_EXECUTION)
        return None if completed is not None else next(item for item in nodes if item["node_code"] == NODE_RULE_EXECUTION)


    def _invalidate_retry_results(self, task: AuditTask,
        nodes: list[dict[str, Any]],
        current_index: int,
    ) -> None:
        """重试失效模型产出，保留人工确认和人工纠错的当前字段版本。"""
        manifest = _task_manifest(task)
        affected_nodes = {NODE_RULE_EXECUTION}
        execution_ids = AuditExecutionCRUD(self.db).ids_for_nodes(task.id, affected_nodes)
        if execution_ids:
            AuditExecutionHistoryService(self.db).invalidate_by_ids(execution_ids, "用户重新执行审核")
            AuditFieldCRUD(self.db).clear_current_facts(
                task.id, fact_kind=FACT_KIND_DERIVED, execution_ids=execution_ids,
            )
            AuditFieldValueStoreService(self.db).sync_current(task)

        # 节点重试已经改变了当前执行/待办集合，旧批次哈希无论重试的是哪类节点都不再有效。
        # 这一步不能放在字段抽取分支末尾，否则普通规则节点和无字段归属的历史节点会提前返回，
        # 页面继续携带旧哈希提交复核时只能得到过期批次冲突。

        # 统一容器重跑只失效方案生效规则的字段产出。
        extraction_codes = {execution_container_of(manifest)["node_code"]}
        affected_extraction_codes = extraction_codes & affected_nodes
        if not affected_extraction_codes:
            return
        # 执行契约包含保留的停用定义，不能直接作为重试取数范围。
        # 与实际抽取共用启停口径，只失效受影响节点在本任务中启用的抽取输出。
        enabled_rule_keys = set(task.task_schema_json["enabled_rule_keys"])
        affected_rule_keys = [
            rule["rule_key"] for rule in manifest["rules"]
            if rule["rule_key"] in enabled_rule_keys
            and rule["node"] in affected_extraction_codes
        ]
        affected_field_keys = enabled_extraction_field_keys(manifest, affected_rule_keys) & {
            field["field_key"] for field in task.task_schema_json["fields"]
        }
        if not affected_field_keys:
            return
        current_upload_ids = [item.id for item in _current_upload_map(self.db, task).values()]
        field_value_ids = AuditFieldCRUD(self.db).field_value_ids_for_fields(
            task_id=task.id,
            version_id=task.scenario_version_id,
            field_keys=affected_field_keys,
            upload_ids=current_upload_ids,
            origin=ORIGIN_DOCUMENT_EXTRACTION,
            exclude_status=FIELD_STATUS_REVIEWED,
        )
        if field_value_ids:
            # 重跑会新增事实版本；历史事实与历史证据不原地清空，当前候选和依据它的待复核项
            # 必须一起退出当前位。否则重试失败时，旧候选仍会被任务视图或后续节点读到。
            AuditFieldCRUD(self.db).invalidate_pending_reviews_by_field_values(field_value_ids)
            AuditFieldCRUD(self.db).clear_field_values_by_ids(field_value_ids)
        # 抽取型统一事实没有直接保存 TaskFieldValue 外键，因此按任务冻结字段定义把
        # 本次重试节点拥有的 field_key 转成 fact_code。两张事实表要独立失效：即使某张表
        # 因历史异常缺了当前行，另一张表里的旧值也不能继续作为规则或计算输入。
        affected_fact_codes = {
            str(field.get("source_ref") or field["field_key"])
            for field in (task.task_schema_json.get("fields") or [])
            if str(field.get("field_key") or "") in affected_field_keys
        }
        if affected_fact_codes:
            AuditFieldCRUD(self.db).clear_current_facts(
                task.id,
                fact_kind=FACT_KIND_EXTRACTED,
                fact_codes=affected_fact_codes,
                exclude_status=FIELD_STATUS_REVIEWED,
            )
            AuditFieldValueStoreService(self.db).sync_current(task)
