"""执行计划与恢复重跑集合：预览、受理和真实调度共用同一份判断。

计划只由任务冻结的配置决定（启用的规则及其依赖顺序），重跑集合只由「未完成的规则
及其传递下游」决定。业务结论好不好不参与判断：异常、证据不足、不适用都是有效结论，
不能因为不是「通过」就重新调用模型。

计划还要分成字段核对前后两段：抽取规则先执行，人工核对字段后其余规则才能读取字段值。
分段同样只按显式依赖计算，不按规则名称或执行方式猜阶段。
"""

from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from app.common.exceptions import BusinessError
from app.constants.execution import ORIGIN_HUMAN_CORRECTION
from app.constants.status import LOOP_BLOCKED
from app.domain.audit.audit_state import EvaluationPlanEntry
from app.domain.configuration.execution_mode import MODE_EXTRACT, MODE_MATERIAL_CHECK, SOURCE_ONLY_MODES, execution_mode_label
from app.domain.audit.field_review import reviewed_extraction_keys
from app.domain.materials.material_precheck import material_blocks, precheck_rule_keys
from app.domain.configuration.rule_library import active, dependency_indexes, execution_order, rule_dependencies
from app.modules.audit.rule.model import RuleResult
from app.modules.audit.execution.model import NodeExecution
from app.modules.audit.field.crud import AuditFieldCRUD
from app.modules.audit.shared import _task_manifest
from app.modules.audit.task.model import AuditTask


def enabled_rules(task: AuditTask, manifest: dict[str, Any], node_code: str) -> list[dict[str, Any]]:
    """任务冻结配置里该节点启用的规则。"""
    enabled_keys = set(task.task_schema_json["enabled_rule_keys"])
    return [
        rule
        for rule in manifest["rules"]
        if active(rule) and rule.get("node") == node_code and rule.get("rule_key") in enabled_keys
    ]


def evaluation_plan(task: AuditTask, node_code: str, *, execution: Any = None) -> list[EvaluationPlanEntry]:
    """按依赖顺序排好的执行计划；预览与真实执行使用同一排序。

    这里不做执行方式的校验：那是「能不能开始执行」的判断，由执行入口自己明确拒绝。
    预览只回答「如果继续，会保留哪些、重跑哪些」。
    """
    manifest = _task_manifest(task)
    ordered = execution_order(enabled_rules(task, manifest, node_code), all_rules=manifest["rules"])
    plan: list[EvaluationPlanEntry] = [
        {
            "cursor": index,
            "rule_key": rule.get("rule_key"),
            "rule_code": rule.get("rule_code") or rule.get("rule_key"),
            "node_code": node_code,
            "rule": rule,
        }
        for index, rule in enumerate(ordered)
    ]
    if execution is None:
        return plan
    result = execution.result_json or {}
    blocked = material_blocks(result)
    early = precheck_rule_keys(result)
    fields = {item["field_key"]: item for item in manifest["fields"]}
    dependencies = plan_dependencies(plan, all_rules=manifest["rules"])
    affected: dict[str, set[str]] = {}
    for entry in plan:
        rule, key = entry["rule"], entry["rule_key"]
        if key in early or rule.get("kind") == "REPORT":
            continue
        if rule.get("execution_mode") in SOURCE_ONLY_MODES:
            affected[key] = set()
            continue
        materials = set(rule.get("materials") or [])
        materials.update(item["material_key"] for item in rule.get("output_definitions") or [] if item.get("material_key"))
        materials.update(item.get("material_key") or fields.get(item["field_key"], {}).get("material_key")
                         for item in rule.get("inputs") or [])
        if rule.get("execution_mode") == MODE_MATERIAL_CHECK and not materials:
            materials = {item["material_key"] for item in manifest["materials"]}
        affected[key] = materials & blocked.keys()
    # 显式依赖闭包，不依赖排序位置；报告继续描述已完成范围及未执行原因。
    changed = True
    while changed:
        changed = False
        for key, materials in affected.items():
            inherited = set().union(*(affected.get(dep, set()) for dep in dependencies[key]))
            if not inherited <= materials:
                materials.update(inherited)
                changed = True
    for entry in plan:
        keys = affected.get(entry["rule_key"])
        if keys:
            entry["rule"] = {**entry["rule"], "material_quality_block": [blocked[key] for key in sorted(keys)]}
        elif entry["rule_key"] in early:
            entry["rule"] = {**entry["rule"], "material_precheck": True}
    return plan


def failed_extraction_keys(db: Session, task: AuditTask, execution: NodeExecution,
                           plan: list[EvaluationPlanEntry]) -> set[str]:
    """抽取技术失败且仍需机器补跑的规则；人工已处理的字段不重新抽取。"""
    result = execution.result_json or {}
    failed = {item["rule_key"] for item in result.get("rule_outcomes", [])
              if item.get("loop_status") == LOOP_BLOCKED or item.get("status") == "NOT_EXECUTED"}
    failed.update(result.get("missing_rule_keys", []))
    outputs = {entry["rule_key"]: {field["field_key"] for field in entry["rule"]["output_definitions"]}
               for entry in plan if entry["rule_key"] in failed and entry["rule"].get("execution_mode") == MODE_EXTRACT
               and not entry["rule"].get("material_quality_block")}
    if not outputs:
        return set()
    manual = AuditFieldCRUD(db).human_keys_for(task.id, set().union(*outputs.values()), ORIGIN_HUMAN_CORRECTION)
    return {key for key, fields in outputs.items() if not fields or fields - manual}


def rule_finished(result: RuleResult) -> bool:
    """这条规则是否已经形成可复用结果。

    依据执行状态与结果契约：阻断或未执行的规则要重跑，已经保存的结论原样保留。
    """
    return (result.loop_status or "DONE") != LOOP_BLOCKED and result.status != "NOT_EXECUTED"


def plan_dependencies(
    plan: list[EvaluationPlanEntry], *, all_rules: list[dict[str, Any]],
) -> dict[str, set[str]]:
    """计划内每条规则直接依赖的计划内规则键；报告依赖全部非报告项。

    依赖只看显式引用（字段产出规则、来源规则、结果编号），与执行顺序、重跑范围、
    字段核对边界共用这一份计算。
    """
    plan_keys = {entry["rule_key"] for entry in plan}
    indexes = dependency_indexes(all_rules)
    dependencies: dict[str, set[str]] = {}
    for entry in plan:
        rule = entry["rule"]
        deps = rule_dependencies(rule, all_rules=all_rules, indexes=indexes)
        if rule.get("kind") == "REPORT":
            deps |= {other["rule_key"] for other in plan if other["rule"].get("kind") != "REPORT"}
        dependencies[entry["rule_key"]] = deps & plan_keys
    return dependencies


def rerun_split(
    plan: list[EvaluationPlanEntry], saved: dict[str, RuleResult], *, all_rules: list[dict[str, Any]],
) -> tuple[set[str], set[str]]:
    """(未完成项, 因依赖受影响需要重跑的已完成项)。

    上游重新执行后，之前用旧上游结果得到的下游结论不能继续作为当前结论；
    报告依赖全部非报告项，也会被自动加入重跑集合。两类互斥，合计就是本次重跑范围。
    """
    plan_keys = {entry["rule_key"] for entry in plan}
    unfinished = {
        entry["rule_key"]
        for entry in plan
        if entry["rule_key"] not in saved or not rule_finished(saved[entry["rule_key"]])
    }
    dependents: dict[str, set[str]] = {}
    for key, deps in plan_dependencies(plan, all_rules=all_rules).items():
        for dependency in deps & plan_keys:
            dependents.setdefault(dependency, set()).add(key)
    affected = set(unfinished)
    pending = list(unfinished)
    while pending:
        key = pending.pop()
        for dependent in dependents.get(key, set()):
            if dependent not in affected:
                affected.add(dependent)
                pending.append(dependent)
    return unfinished, affected - unfinished


@dataclass(frozen=True)
class ReviewBoundary:
    """字段核对边界：核对前只跑抽取，核对后跑其余全部规则。

    `extraction` 为空表示这次计划没有可核对的字段抽取，直接按完整计划执行，
    不制造空的核对步骤。
    """

    extraction: tuple[str, ...]
    after: tuple[str, ...]
    early: tuple[str, ...] = ()

    @property
    def required(self) -> bool:
        """本次计划是否存在字段核对等待点。"""
        return bool(self.extraction)

    def phase_keys(self, *, confirmed: bool, extraction_closed: bool = False) -> set[str]:
        """当前允许执行的规则键。

        抽取阶段一旦结束就不再重跑：人工核对补填的值不能被模型重新抽取覆盖，
        等待核对期间也不该重复计费。已确认时执行核对后的其余规则；
        已结束但尚未确认时本轮不执行任何规则，批次继续停在等待核对。
        """
        if not self.required:
            return {*self.extraction, *self.after, *self.early}
        if confirmed:
            return {*self.after, *self.early}
        if extraction_closed:
            return set()
        return {*self.extraction, *self.early}

    def summary(self) -> dict[str, int]:
        return {"extraction": len(self.extraction), "after_review": len(self.after)}


def _rule_evidence(entry: dict[str, Any]) -> dict[str, Any]:
    """冲突说明用业务编号与名称，不把内部规则键当成给用户看的身份。"""
    rule = entry["rule"]
    return {
        "rule_code": str(rule.get("rule_code") or rule.get("rule_key") or ""),
        "rule_name": str(rule.get("name") or ""),
        "execution_mode": str(rule.get("execution_mode") or ""),
    }


def field_review_boundary(
    plan: list[EvaluationPlanEntry], *, all_rules: list[dict[str, Any]],
) -> ReviewBoundary:
    """按显式依赖拆出「字段核对前」与「核对后」两段。

    核对前只放抽取规则，并且必须依赖闭合：抽取依赖另一条抽取时可以一起前置；
    抽取依赖计算、检查等业务判断时，那条依赖会用尚未核对的字段值算出结果，
    一次核对无法覆盖这种情况。此时明确失败并列出具体规则，不由程序改阶段或跳过核对。
    """
    plan_keys = [entry["rule_key"] for entry in plan]
    by_key = {entry["rule_key"]: entry for entry in plan}
    early = {entry["rule_key"] for entry in plan
             if entry["rule"].get("material_precheck") or entry["rule"].get("material_quality_block")}
    extraction = {
        entry["rule_key"] for entry in plan
        if str(entry["rule"].get("execution_mode") or "") == MODE_EXTRACT
    } - early
    if not extraction:
        return ReviewBoundary((), tuple(plan_keys))
    dependencies = plan_dependencies(plan, all_rules=all_rules)
    before: set[str] = set()
    conflicts: list[dict[str, Any]] = []
    pending = sorted(extraction)
    while pending:
        key = pending.pop()
        if key in before:
            continue
        before.add(key)
        for dependency in sorted(dependencies.get(key, set())):
            if dependency in early:
                continue
            if dependency in extraction:
                pending.append(dependency)
                continue
            conflicts.append({
                **_rule_evidence(by_key[key]),
                "dependency": _rule_evidence(by_key[dependency]),
                "dependency_mode_label": execution_mode_label(
                    by_key[dependency]["rule"].get("execution_mode"),
                ),
            })
    if conflicts:
        described = "；".join(
            f"抽取规则「{item['rule_name'] or item['rule_code']}」依赖"
            f"「{item['dependency']['rule_name'] or item['dependency']['rule_code']}」"
            f"（{item['dependency_mode_label']}）"
            for item in conflicts[:5]
        )
        raise BusinessError(
            "AUDIT_FIELD_REVIEW_DEPENDENCY_CONFLICT",
            f"字段抽取依赖了抽取以外的业务规则，无法在一次「字段核对」里完成：{described}。"
            "请先把该依赖调整为抽取字段，或确认是否需要在核对后再设第二个核对点；"
            "当前不会自动跳过字段核对，也不会把依赖改到核对之前执行。",
            status_code=409,
            details={"conflicts": conflicts},
        )
    after = [key for key in plan_keys if key not in before and key not in early]
    return ReviewBoundary(extraction=tuple(sorted(before)), after=tuple(after), early=tuple(sorted(early)))


def reviewed_extraction_rule_keys(execution: Any) -> set[str]:
    """只读取本批次已保存的核对清单，不按当前配置推算。"""
    if execution is None:
        return set()
    return reviewed_extraction_keys(
        execution.result_json or {}, waiting=execution.status == "WAITING_FIELD_REVIEW",
    )


def assert_results_match_plan(plan: list[EvaluationPlanEntry], saved: dict[str, RuleResult]) -> None:
    """已提交结果必须能由当前执行计划解释。

    出现计划外的规则结果说明任务快照与批次对不上，此时任何重跑范围都是猜的，
    明确失败而不是挑一部分继续。
    """
    unknown = set(saved) - {entry["rule_key"] for entry in plan}
    if unknown:
        raise BusinessError(
            "AUDIT_RESUME_RESULTS_MISMATCH",
            "执行批次里有不属于当前执行计划的规则结果，无法确定重跑范围",
            status_code=500,
            details={"rule_keys": sorted(unknown)},
        )
