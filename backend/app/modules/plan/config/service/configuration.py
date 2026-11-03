"""方案定制配置的校验与保存。"""

from copy import deepcopy
from dataclasses import fields as config_fields
from typing import Any

from sqlalchemy.orm import Session

from app.common.exceptions import BusinessError
from app.constants.status import SCOPE_NOT_APPLICABLE
from app.domain import field_strategy
from app.domain import manifest as manifest_module
from app.domain.calculation_config import (
    calculation_store,
    calculations_referencing_field,
    remap_calculation_field_codes,
)
from app.domain.manifest import FORM_VALUE_TYPES
from app.domain.plan_calculations import plan_calculation_config
from app.domain.report_data import report_data_refs
from app.domain.rule_library import active
from app.modules.plan.config.crud import PlanConfigCRUD
from app.modules.plan.config.model import PlanField, PlanMaterial, PlanRule, PlanSection
from app.modules.plan.config.service.views import PlanConfigViewsService
from app.modules.plan.plan.crud import PlanCRUD
from app.modules.plan.plan.service.compilation import PlanCompilationService
from app.modules.plan.plan.service.lookup import PlanLookupService
from app.modules.system.operation.service.operation_log import OperationLogService
from app.modules.system.operation.service.operation_types import OBJECT_PLAN
from app.services.shared import configuration as config_store


class PlanConfigConfigurationService:
    """本实体的服务方法；db 由构造时注入。"""

    def __init__(self, db: Session | None = None) -> None:
        self.db = db

    def update_plan_config(self, plan_code: str, payload: dict[str, Any]) -> dict[str, Any]:
        """整体替换方案的配置并重新编译。

        整体提交而不是逐条增删：一次提交的是一份完整集合，因此「这次提交之后这个方案到底配了哪些
        材料 / 字段 / 规则 / 章节 / 计算规则」不取决于历史操作顺序。

        归属键（`material_key` / `field_key` / `rule_key` / `section_key`）由调用方给出并原样保留：
        字段与规则的互相引用就是靠它们，重新生成会让已有引用断掉。

        配置写完之后立刻重新编译：方案的范围与「能不能用」在同一次提交里算出来，不会留下一个配置
        已改、范围还是旧的中间态。
        """
        plan = PlanLookupService(self.db).plan_or_404( plan_code, lock=True)
        materials = self._require_list(payload, "materials")
        fields = self._require_list(payload, "fields")
        rules = self._require_list(payload, "rules")
        sections = self._require_list(payload, "sections")
        calculation_rules = self._require_list(payload, "calculation_rules")
        context = plan.context_json if isinstance(plan.context_json, dict) else {}
        previous_calculations, previous_calculation_enabled = plan_calculation_config(context)
        calculation_by_code = {str(item["rule_code"]): item for item in previous_calculations}
        calculation_definitions = []
        calculation_enabled: dict[str, bool] = {}
        for index, item in enumerate(calculation_rules, start=1):
            code = self._required_key(item, "rule_code", f"第 {index} 条计算规则")
            if code in calculation_enabled or code not in previous_calculation_enabled or type(item.get("enabled")) is not bool:
                raise BusinessError(
                    "PLAN_CALC_SELECTION_INVALID",
                    f"第 {index} 条计算规则的编号或启停值无效",
                    details={"rule_code": code},
                )
            calculation_enabled[code] = item["enabled"]
            definition = deepcopy(calculation_by_code[code])
            for key in (
                "rule_name", "formula", "requirement", "result_code", "result_type", "unit", "currency",
                "period_policy", "statement_scope", "status", "remark", "parameters",
                "depends_on_result_codes", "business_context", "source_id",
            ):
                definition[key] = deepcopy(item[key])
            definition["rule_name"] = self._required_key(definition, "rule_name", f"第 {index} 条计算规则")
            definition["parameters"] = self._require_list(definition, "parameters")
            calculation_definitions.append(definition)
        if set(calculation_enabled) != set(previous_calculation_enabled):
            raise BusinessError(
                "PLAN_CALC_SELECTION_INVALID",
                "方案必须提交完整的计算规则启停选择",
                details={"missing_rule_codes": sorted(set(previous_calculation_enabled) - set(calculation_enabled))},
            )

        # 来源是已存在配置行的审计事实，整体提交的页面表单不能改写它。
        previous = PlanConfigCRUD(self.db).plan_config_rows( plan.id)
        previous_rules = {item.rule_key: item for item in previous[2]}
        submitted_fields = {item.get("field_key"): item for item in fields}
        field_material: dict[str, str] = {}
        for index, item in enumerate(rules, start=1):
            rule_json = item.get("rule_json")
            if not isinstance(rule_json, dict):
                raise BusinessError(
                    "PLAN_RULE_JSON_INVALID",
                    f"第 {index} 条规则缺少业务定义（rule_json）",
                    details={"rule_key": item.get("rule_key")},
                )
            previous_rule = previous_rules.get(item.get("rule_key"))
            if rule_json.get("generated_from") == "FIELD":
                output_fields = [submitted_fields[key] for key in rule_json.get("outputs") or [] if key in submitted_fields]
                item["enabled"] = any(active(field) for field in output_fields)
                if output_fields:
                    rule_json["config_status"] = output_fields[0].get("config_status")
            elif rule_json.get("generated_from") == "REPORT":
                item["enabled"] = any(
                    active(section) for section in sections
                    if str(section.get("report_code") or section["section_key"]) == str(rule_json["report_ref"])
                )
            if item.get("enabled", True) and previous_rule is not None and not active({
                **(previous_rule.rule_json or {}), "enabled": previous_rule.enabled,
            }):
                # 用户明确打开开关即确认启用；仅保存其他内容不改变待确认状态。
                rule_json["config_status"] = "CLEAR"
                if rule_json.get("generated_from") == "FIELD":
                    for field_key in rule_json.get("outputs") or []:
                        if field_key in submitted_fields:
                            submitted_fields[field_key]["enabled"] = True
                            submitted_fields[field_key]["config_status"] = "CLEAR"
        previous_fields = {item.field_key: item for item in previous[1]}
        previous_code_owners: dict[str, set[str]] = {}
        for item in previous[1]:
            code = str(item.source_ref or "").strip() or item.field_key
            previous_code_owners.setdefault(code, set()).add(item.field_key)
        field_code_changes: dict[str, str] = {}
        for index, item in enumerate(fields, start=1):
            field_key = self._required_key(item, "field_key", f"第 {index} 个字段")
            previous_field = previous_fields.get(field_key)
            if previous_field is None:
                continue
            previous_code = str(previous_field.source_ref or "").strip() or field_key
            next_code = str(item.get("source_ref") or "").strip() or field_key
            if previous_code == next_code:
                continue
            if len(previous_code_owners[previous_code]) > 1 and calculations_referencing_field(
                context.get("calculation_json"), previous_code,
            ):
                raise BusinessError(
                    "PLAN_CALC_FIELD_CODE_AMBIGUOUS",
                    "原配置的多个字段共用同一编号，无法确定计算引用对应哪个字段，不能自动改写引用",
                    details={"field_code": previous_code, "field_keys": sorted(previous_code_owners[previous_code])},
                )
            if previous_code != field_key:
                # 计算按内部字段键引用时，修改业务编号不能改写执行身份。
                field_code_changes[previous_code] = next_code
        origins = [
            {getattr(row, key): row.origin_json for row in rows}
            for rows, key in zip(previous, ("material_key", "field_key", "rule_key", "section_key"), strict=True)
        ]
        extensions = {}
        for rows, model, identity in zip(previous, (PlanMaterial, PlanField, PlanRule, PlanSection),
                                         ("material_key", "field_key", "rule_key", "section_key"), strict=True):
            columns = {item.name for item in config_fields(model)}
            extensions[model.__name__] = {getattr(row, identity): deepcopy({key: value for key, value in row.data.items() if key not in columns}) for row in rows}
        for items, model, identity in ((materials, PlanMaterial, "material_key"), (fields, PlanField, "field_key"),
                                        (sections, PlanSection, "section_key")):
            for item in items:
                retained = extensions[model.__name__].setdefault(item[identity], {})
                for key in ("business_context", "enabled", "config_status"):
                    if key in item:
                        retained[key] = deepcopy(item[key])
        for rows in previous:
            for row in rows:
                config_store.delete(row)
        PlanCRUD(self.db).flush()

        for index, item in enumerate(materials, start=1):
            config_store.add(
                self.db, PlanMaterial(
                    plan_id=plan.id,
                    material_key=self._required_key(item, "material_key", f"第 {index} 个材料"),
                    name=self._required_key(item, "name", f"第 {index} 个材料"),
                    required=bool(item.get("required")),
                    description=str(item.get("description") or ""),
                    source_ref=item.get("source_ref"),
                    origin_json=origins[0].get(item.get("material_key")),
                    sort_no=index,
                )
            )
        for index, item in enumerate(fields, start=1):
            field_key = self._required_key(item, "field_key", f"第 {index} 个字段")
            if "node_code" in item:
                node_code = item["node_code"]
                if node_code is not None and (not isinstance(node_code, str) or not node_code.strip()):
                    raise BusinessError("PLAN_FIELD_NODE_INVALID", "字段抽取节点必须是节点编码或空值", details={"field_key": field_key})
                extensions[PlanField.__name__].setdefault(field_key, {})["node_code"] = node_code
            value_type = self._required_key(item, "value_type", f"第 {index} 个字段")
            if value_type not in FORM_VALUE_TYPES:
                raise BusinessError(
                    "PLAN_FIELD_TYPE_INVALID",
                    f"字段的数据类型“{value_type}”不在允许取值内",
                    details={"allowed": sorted(FORM_VALUE_TYPES), "value_type": value_type},
                )
            field_name = self._required_key(item, "name", f"第 {index} 个字段")
            material_key = self._required_key(item, "material_key", f"第 {index} 个字段")
            field_material[field_key] = material_key
            field_state = extensions[PlanField.__name__].get(field_key, {})
            producers = [rule for rule in rules if field_key in (rule.get("rule_json") or {}).get("outputs", [])]
            extraction_strategy, structure_schema = field_strategy.validate_field_strategy(
                value_type=value_type,
                extraction_strategy=item.get("extraction_strategy"),
                structure_schema=item.get("structure_schema"),
                field_name=field_name,
                enabled=active(field_state)
                        and (not producers or any(active({**(rule.get("rule_json") or {}), "enabled": rule.get("enabled", True)}) for rule in producers)),
            )
            config_store.add(
                self.db, PlanField(
                    plan_id=plan.id,
                    field_key=field_key,
                    material_key=material_key,
                    name=field_name,
                    value_type=value_type,
                    extraction_strategy=extraction_strategy,
                    structure_schema_json=structure_schema,
                    unit=item.get("unit") or None,
                    currency=item.get("currency") or None,
                    artifact_source=item.get("artifact_source") or None,
                    # 期间与报表口径只在业务涉及时填写；空值统一落成不适用，满足事实表的非空定位键。
                    period_scope=str(item.get("period_scope") or "").strip() or SCOPE_NOT_APPLICABLE,
                    statement_scope=str(item.get("statement_scope") or "").strip() or SCOPE_NOT_APPLICABLE,
                    entity_scope=str(item.get("entity_scope") or "").strip() or "SELF",
                    required=bool(item.get("required")),
                    instruction=str(item.get("instruction") or ""),
                    source_ref=item.get("source_ref"),
                    origin_json=origins[1].get(item.get("field_key")),
                    sort_no=index,
                )
            )
        for index, item in enumerate(rules, start=1):
            rule_json = item.get("rule_json")
            # 字段换了所属材料时，规则输入仍写着旧材料；按本次提交的字段重新对齐引用，
            # 否则编译只能把它记成依赖错误，而页面上没有任何地方能改回来。
            for entry in rule_json.get("inputs") or []:
                field_key = entry.get("field_key")
                if field_key in field_material and entry.get("material_key") != field_material[field_key]:
                    entry["material_key"] = field_material[field_key]
            config_store.add(
                self.db, PlanRule(
                    plan_id=plan.id,
                    rule_key=self._required_key(item, "rule_key", f"第 {index} 条规则"),
                    name=self._required_key(item, "name", f"第 {index} 条规则"),
                    node_code=self._required_key(item, "node_code", f"第 {index} 条规则"),
                    operator_code=self._required_key(item, "operator_code", f"第 {index} 条规则"),
                    enabled=bool(item.get("enabled", True)),
                    rule_json=rule_json,
                    source_ref=item.get("source_ref"),
                    origin_json=origins[2].get(item.get("rule_key")),
                    sort_no=index,
                )
            )
        for index, item in enumerate(sections, start=1):
            config_store.add(
                self.db, PlanSection(
                    plan_id=plan.id,
                    section_key=self._required_key(item, "section_key", f"第 {index} 个章节"),
                    title=self._required_key(item, "title", f"第 {index} 个章节"),
                    content_instruction=str(item.get("content_instruction") or ""),
                    field_keys=[ref for ref in report_data_refs(item)
                                if ref in {field["field_key"] for field in fields}],
                    data_refs=report_data_refs(item),
                    report_code=item.get("report_code"),
                    report_name=item.get("report_name"),
                    material_keys=[str(key) for key in (item.get("material_keys") or [])] or None,
                    formats=[str(key) for key in (item.get("formats") or [])] or None,
                    section_order=item.get("section_order"),
                    content_type=item.get("content_type"),
                    result_sort=item.get("result_sort"),
                    risk_level_order=list(item.get("risk_level_order") or []),
                    config_status=item.get("config_status"),
                    origin_json=origins[3].get(item.get("section_key")),
                    sort_no=index,
                )
            )
        PlanCRUD(self.db).flush()
        for model, identity in ((PlanMaterial, "material_key"), (PlanField, "field_key"), (PlanRule, "rule_key"), (PlanSection, "section_key")):
            for row in config_store.rows(self.db, model, plan.id):
                for key, value in extensions[model.__name__].get(getattr(row, identity), {}).items():
                    setattr(row, key, value)
        calculation_json = remap_calculation_field_codes(calculation_definitions, field_code_changes)
        for calculation in calculation_store(calculation_json):
            code = str(calculation["rule_code"])
            if calculation_enabled[code] and not previous_calculation_enabled[code] and calculation.get("status") == calculation_by_code[code].get("status"):
                calculation["status"] = "CLEAR"
        calculation_json = manifest_module.refresh_calculation_references(calculation_json)
        plan.context_json = {
            **context,
            "calculation_enabled": calculation_enabled,
            "calculation_json": calculation_json,
        }
        # 配置改完立刻重算：范围与「能不能用」在同一次提交里确定。
        PlanCompilationService(self.db)._recompile_plan( plan)
        OperationLogService(self.db).record_operation(

            object_type=OBJECT_PLAN,
            object_no=plan_code,
            object_name=plan.plan_name,
            operation_type="SAVE",
            detail={
                "plan_code": plan_code,
                "material_count": len(materials),
                "field_count": len(fields),
                "rule_count": len(rules),
                "calculation_rule_count": len(calculation_rules),
                "enabled_calculation_rule_count": sum(calculation_enabled.values()),
                "section_count": len(sections),
                "effective": plan.sync_error is None,
            },
        )
        PlanCRUD(self.db).flush()
        PlanCRUD(self.db).refresh(plan)
        return PlanConfigViewsService(self.db).plan_config_view( plan)


    def _require_list(self, payload: dict[str, Any], key: str) -> list[dict[str, Any]]:
        value = payload.get(key)
        if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
            raise BusinessError(
                "PLAN_CONFIG_INVALID",
                f"方案配置的「{key}」必须是一个列表",
                details={"column": key},
            )
        return list(value)


    def _required_key(self, item: dict[str, Any], key: str, where: str) -> str:
        value = str(item.get(key) or "").strip()
        if not value:
            raise BusinessError(
                "PLAN_CONFIG_INVALID",
                f"{where}缺少必填项「{key}」",
                details={"column": key},
            )
        return value
