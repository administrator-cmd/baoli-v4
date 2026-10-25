"""组合汇总规则：只根据来源规则结果生成确定性结论，不调用模型。"""

from typing import Any

from sqlalchemy.orm import Session

from ....constants.status import LOOP_DONE, RULE_STATUS_LABELS, UNRESOLVED_RESULT_STATUSES
from ..tasks.lookup import task_or_404
from .errors import agent_step


def build_aggregate_verdict(
    rule: dict[str, Any],
    upstream_results: list[dict[str, Any]],
    *,
    scope_keys: set[str] | None = None,
    rule_names: dict[str, str] | None = None,
    code_to_key: dict[str, str] | None = None,
) -> dict[str, Any]:
    """按来源规则的最终状态汇总；不重新检查材料，也不新增业务结论。

    `scope_keys` 是本次任务实际启用的规则集合。来源规则引用里包含被停用的规则时，
    它们不属于本次审核范围，不参与汇总，也不计为缺失。

    `code_to_key` 把 `source_rule_codes` 解析成规则身份。汇总按规则键判断缺失，
    只声明业务编号的来源必须先解析；解析不掉的编号本身就是缺失来源。
    """
    declared = list(dict.fromkeys([
        *(rule.get("source_rule_keys") or []),
        *([rule["source_rule_key"]] if rule.get("source_rule_key") else []),
    ]))
    unresolved_codes: list[str] = []
    for code in dict.fromkeys(str(item) for item in rule.get("source_rule_codes") or [] if item):
        key = (code_to_key or {}).get(code)
        if key is None:
            unresolved_codes.append(code)
        elif key not in declared:
            declared.append(key)
    expected = [key for key in declared if scope_keys is None or key in scope_keys]
    labels = rule_names or {}

    def label(key: str) -> str:
        return labels.get(key) or key

    by_key = {item.get("rule_key"): item for item in upstream_results}
    if declared or unresolved_codes:
        sources = [by_key.get(key) for key in expected]
        missing = [key for key in expected if key not in by_key] + unresolved_codes
    else:
        sources = list(upstream_results)
        missing = []

    present = [item for item in sources if item]
    abnormal = [item for item in present if item.get("status") == "ABNORMAL"]
    unresolved = [item for item in present if item.get("status") in UNRESOLVED_RESULT_STATUSES]
    not_applicable = [item for item in present if item.get("status") == "NOT_APPLICABLE"]

    if declared and not expected and not unresolved_codes:
        # 来源规则全部被停用：没有可汇总的结果，不能当成「均未发现异常」。
        status = "EVIDENCE_INSUFFICIENT"
        message = "组合汇总无法完成：配置的来源规则均未启用。"
        missing_items = ["配置的来源规则均未启用，本次没有可汇总的结果"]
    elif missing:
        status = "EVIDENCE_INSUFFICIENT"
        message = f"组合汇总无法完成：缺少 {len(missing)} 条来源规则结果。"
        missing_items = [f"缺少来源规则结果：{label(key)}" for key in missing]
    else:
        missing_items = []
        if abnormal:
            status = "ABNORMAL"
            message = f"组合汇总：{len(abnormal)} 条来源规则异常。"
        elif unresolved:
            status = "EVIDENCE_INSUFFICIENT"
            message = f"组合汇总：{len(unresolved)} 条来源规则无法判断。"
        elif present and len(not_applicable) == len(present):
            status = "NOT_APPLICABLE"
            message = "组合汇总：来源规则本次均不适用。"
        else:
            status = "PASSED"
            message = f"组合汇总：{len(present)} 条来源规则均未发现异常。"

    basis = [
        {
            "label": str(item.get("rule_name") or item.get("rule_key") or ""),
            "value": RULE_STATUS_LABELS.get(str(item.get("status") or ""), str(item.get("status") or "")),
            "explanation": str(item.get("message") or ""),
        }
        for item in present
    ]
    if missing:
        basis.extend({"label": label(key), "value": "未找到", "explanation": "没有来源规则结果"} for key in missing)
    return {
        "status": status,
        "message": message,
        "basis": basis,
        "missing_items": missing_items,
        "next_action": "",
        "outputs": [],
        "evidence": [],
        "presentation_version": 2,
    }


def graph_build_aggregate_verdict(
    db: Session,
    task_no: str,
    node_code: str,
    execution_id: int | None,
    rule_instance: dict[str, Any] | None,
    planned_rule: dict[str, Any],
) -> dict[str, Any]:
    """读取来源规则结果并形成确定性汇总结论。"""
    with agent_step(db, task_no):
        task = task_or_404(db, task_no, lock=True)
        from ..rule_execution import evaluation_context

        context = evaluation_context(db, task, planned_rule)
        scope_keys = {str(key) for key in (task.task_schema_json or {}).get("enabled_rule_keys") or []}
        rule_names = {
            str(item.get("rule_key")): str(item.get("name") or item.get("rule_code") or item.get("rule_key"))
            for item in (task.manifest_json or {}).get("rules") or []
        }
        # 业务编号按冻结配置解析成规则身份；同号命中多条时不猜，留给缺失项说明。
        by_code: dict[str, list[str]] = {}
        for item in (task.manifest_json or {}).get("rules") or []:
            for code in {item.get("rule_code"), item.get("result_code")} - {None, ""}:
                by_code.setdefault(str(code), []).append(str(item.get("rule_key")))
        code_to_key = {code: keys[0] for code, keys in by_code.items() if len(keys) == 1}
        return {
            "candidate_verdict": build_aggregate_verdict(
                planned_rule,
                context["upstream_results"],
                scope_keys=scope_keys or None,
                rule_names=rule_names,
                code_to_key=code_to_key,
            ),
            "loop_status": LOOP_DONE,
            "unresolved_items": [],
            "last_error": None,
        }
