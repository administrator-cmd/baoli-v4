"""执行计划与恢复重跑集合：预览、受理和真实调度共用同一份判断。

计划只由任务冻结的配置决定（启用的规则及其依赖顺序），重跑集合只由「未完成的规则
及其传递下游」决定。业务结论好不好不参与判断：异常、证据不足、不适用都是有效结论，
不能因为不是「通过」就重新调用模型。
"""

from typing import Any

from ....common.exceptions import BusinessError
from ....constants.status import LOOP_BLOCKED
from ....domain.rule_library import active, dependency_indexes, execution_order, rule_dependencies
from ....entity.tasks import AuditTask, RuleResult
from ..shared import _task_manifest


def enabled_rules(task: AuditTask, manifest: dict[str, Any], node_code: str) -> list[dict[str, Any]]:
    """任务冻结配置里该节点启用的规则。"""
    enabled_keys = set(task.task_schema_json["enabled_rule_keys"])
    return [
        rule
        for rule in manifest["rules"]
        if active(rule) and rule.get("node") == node_code and rule.get("rule_key") in enabled_keys
    ]


def evaluation_plan(task: AuditTask, node_code: str) -> list[dict[str, Any]]:
    """按依赖顺序排好的执行计划；预览与真实执行使用同一排序。

    这里不做执行方式的校验：那是「能不能开始执行」的判断，由执行入口自己明确拒绝。
    预览只回答「如果继续，会保留哪些、重跑哪些」。
    """
    manifest = _task_manifest(task)
    ordered = execution_order(enabled_rules(task, manifest, node_code), all_rules=manifest["rules"])
    return [
        {
            "cursor": index,
            "rule_key": rule.get("rule_key"),
            "rule_code": rule.get("rule_code") or rule.get("rule_key"),
            "node_code": node_code,
            "rule": rule,
        }
        for index, rule in enumerate(ordered)
    ]


def rule_finished(result: RuleResult) -> bool:
    """这条规则是否已经形成可复用结果。

    依据执行状态与结果契约：阻断或未执行的规则要重跑，已经保存的结论原样保留。
    """
    return (result.loop_status or "DONE") != LOOP_BLOCKED and result.status != "NOT_EXECUTED"


def rerun_split(
    plan: list[dict[str, Any]], saved: dict[str, RuleResult], *, all_rules: list[dict[str, Any]],
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
    indexes = dependency_indexes(all_rules)
    for entry in plan:
        rule = entry["rule"]
        deps = rule_dependencies(rule, all_rules=all_rules, indexes=indexes)
        if rule.get("kind") == "REPORT":
            deps |= {other["rule_key"] for other in plan if other["rule"].get("kind") != "REPORT"}
        for dependency in deps & plan_keys:
            dependents.setdefault(dependency, set()).add(entry["rule_key"])
    affected = set(unfinished)
    pending = list(unfinished)
    while pending:
        key = pending.pop()
        for dependent in dependents.get(key, set()):
            if dependent not in affected:
                affected.add(dependent)
                pending.append(dependent)
    return unfinished, affected - unfinished


def assert_results_match_plan(plan: list[dict[str, Any]], saved: dict[str, RuleResult]) -> None:
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
