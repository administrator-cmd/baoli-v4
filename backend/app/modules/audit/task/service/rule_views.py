"""任务规则结果的只读展示：名称、比较来源与已保存的业务结论。"""

from typing import Any

from sqlalchemy.orm import Session

from app.domain.configuration.rule_library import is_risk_rule
from app.modules.audit.field.service.evidence import AuditFieldEvidenceService
from app.modules.audit.field.service.value_store import RULE_PAYLOAD_KEY
from app.modules.audit.rule.model import RuleResult
from app.modules.audit.rule.service.rule_results import AuditRuleResultsService
from app.modules.audit.task.model import AuditTask
from app.modules.material.document.model import TaskDocument


class AuditTaskRuleViewsService:
    def __init__(self, db: Session | None = None) -> None:
        self.db = db

    def _named_rule_result(self, value: dict[str, Any], names: dict[str, str]) -> dict[str, Any]:
        """把历史结果里的内部规则键替换成规则名再展示。

        组合汇总的来源规则在早期记录里只保存了 `rule_...` 键，页面无法据字面理解；
        名称取自任务冻结的 manifest，只影响展示，不改写已保存的结果。
        """
        def named(text: Any) -> Any:
            if not isinstance(text, str) or "rule_" not in text:
                return text
            for key, name in names.items():
                if key in text:
                    text = text.replace(key, name)
            return text

        result = dict(value)
        basis = result.get("basis")
        if isinstance(basis, list):
            result["basis"] = [
                {**row, "label": named(row.get("label"))} if isinstance(row, dict) else row
                for row in basis
            ]
        missing = result.get("missing_items")
        if isinstance(missing, list):
            result["missing_items"] = [named(item) for item in missing]
        return result


    def comparison_sources(self, result: dict[str, Any], *, task: AuditTask, documents_by_material_key: dict[str, TaskDocument],
    ) -> dict[str, Any]:
        """为实际取值表和比较结果还原业务名称，与是否抽取成功无关。

        比较结果里的左右下标指向本次真实输入；名称取自任务冻结配置与材料位。
        早期结果保存时没有带上名称，这里只补展示字段，不改写已经算出的取值与算式。
        """
        field_names = {field["field_key"]: field["name"] for field in task.task_schema_json["fields"] if field.get("name")}
        material_names = {key: document.material_name for key, document in documents_by_material_key.items()}

        def named_input(row):
            if not isinstance(row, dict):
                return row
            labels = {}
            if not row.get("field_name") and (name := field_names.get(str(row.get("field_key") or ""))):
                labels["field_name"] = name
            if not row.get("material_name") and (name := material_names.get(str(row.get("material_key") or ""))):
                labels["material_name"] = name
            return {**row, **labels} if labels else row

        # 实际取值直接读取 inputs（旧版抽取结果也可能使用 extracted_values），不是 tool_findings。
        # 只补缺失名称；不按金额或行序猜测身份，也不查当前事实来覆盖历史取值。
        result = {**result, **{key: [named_input(row) for row in result[key]]
                               for key in ("inputs", "extracted_values") if isinstance(result.get(key), list)}}
        findings = result.get("tool_findings")
        if not isinstance(findings, list):
            return result
        changed = False
        updated: list[Any] = []
        for finding in findings:
            comparison = finding.get("comparison") if isinstance(finding, dict) else None
            snapshot = finding.get("input_snapshot") if isinstance(finding, dict) else None
            if not isinstance(comparison, dict) or not isinstance(snapshot, list):
                updated.append(finding)
                continue
            fixed = dict(comparison)
            for side in ("left", "right"):
                index = fixed.get(f"{side}_index")
                if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < len(snapshot):
                    continue
                source = snapshot[index] if isinstance(snapshot[index], dict) else {}
                if not fixed.get(f"{side}_label"):
                    label = " · ".join(name for name in (
                        material_names.get(str(source.get("material_key") or "")),
                        field_names.get(str(source.get("field_key") or "")),
                    ) if name)
                    if label:
                        fixed[f"{side}_label"] = label
                        changed = True
                if fixed.get(f"{side}_raw_value") is None and source.get("raw_value") is not None:
                    fixed[f"{side}_raw_value"] = source.get("raw_value")
                    changed = True
            if fixed.get("addends") and not any(fixed.get("addend_labels") or ()):
                # 加总比较的加数按输入顺序排列：第一个输入是左侧合计，其余为加数。
                # 早期结果保存的是空字符串占位，列表非空但没有任何可用标签，这里同样补齐。
                labels = [
                    " · ".join(name for name in (
                        material_names.get(str(source.get("material_key") or "")),
                        field_names.get(str(source.get("field_key") or "")),
                    ) if name)
                    for source in (item if isinstance(item, dict) else {} for item in snapshot[1:])
                ]
                if any(labels):
                    fixed["addend_labels"] = labels
                    changed = True
            updated.append({**finding, "comparison": fixed})
        return {**result, "tool_findings": updated} if changed else result


    def rule_result_view(self, item: RuleResult,
        *,
        task: AuditTask,
        rule_definitions: dict[tuple[str, str], dict[str, Any]],
        execution_node_by_id: dict[int, str],
        node_type_by_code: dict[str, str],
        documents_by_material_key: dict[str, TaskDocument],
        rule_name_by_key: dict[str, str],
        rule_value_of: dict[int, dict[str, Any]],
        rule_sources: dict[int, list[dict[str, Any]]],
    ) -> dict[str, Any]:
        """单条规则结果的展示视图；规则定义只按节点与规则键读取一次。"""
        node_code = execution_node_by_id[item.node_execution_id]
        definition = rule_definitions.get((node_code, item.rule_key), {})
        return {
            "rule_key": item.rule_key,
            "name": item.rule_name,
            "source_ref": definition.get("source_ref"),
            "kind": definition.get("kind"),
            # 执行方式决定模型上下文与允许动作；旧快照没有该字段时前端显示未判定。
            "execution_mode": definition.get("execution_mode"),
            "is_risk_rule": is_risk_rule(definition),
            # 规则来源工作表：页面按它分组，不同场景使用各自的 Sheet 名。
            "source_sheet": (definition.get("origin") or {}).get("sheet_name"),
            "material_names": [
                documents_by_material_key[key].material_name
                for key in definition.get("materials") or []
                if key in documents_by_material_key
            ],
            "status": item.status,
            "message": item.message,
            "turn_count": item.turn_count,
            "result": self.comparison_sources(
                self._named_rule_result({
                    # 业务结论通过统一读取入口提供，诊断不重复展开存储载荷。
                    **rule_value_of[item.id],
                    "status": item.status,
                    "message": item.message,
                    "risk_level": item.risk_level,
                    **AuditRuleResultsService(self.db).restore_diagnostic_inputs(
                         task,
                        AuditFieldEvidenceService().result_content_view({
                            key: value for key, value in (item.result_json or {}).items() if key != RULE_PAYLOAD_KEY
                        } if isinstance(item.result_json, dict) else {}),
                    ),
                    # 规则结果的节点归属必须来自实际执行记录；node 使用能力类型供页面阶段筛选，
                    # node_code 保留用户配置的实例码，两个字段都不由规则名称推断。
                    "node": node_type_by_code.get(node_code),
                    "node_type": node_type_by_code.get(node_code),
                    "node_code": node_code,
                }, rule_name_by_key),
                task=task, documents_by_material_key=documents_by_material_key,
            ),
            "evidence": rule_sources[item.id],
            # 旧结果没有 standard 快照时，单独返回任务绑定配置的要求，不改写执行结果。
            "configured_requirement": definition.get("requirement"),
            # 解析阶段记录的未绑定输入；页面据此解释计算为何不能形成正式结果。
            "input_gaps": list(definition.get("input_gaps") or []),
        }

