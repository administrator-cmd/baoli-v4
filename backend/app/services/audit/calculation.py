"""计算节点的执行：跑 manifest 里冻结的计算规则，写统一事实与规则结果。

执行流程（每一步都是显式的，没有跳过分支）：

1. 取本任务当前有效的字段抽取事实，按 (字段编号, 期间, 报表口径, 主体范围) 建索引；
2. 按计算规则之间的依赖做**拓扑序**执行，保证上游结果先于下游产出；
3. 每条规则求值 → 判定 → 写 `task_fact`（DERIVED）→ 写 `rule_result`；
4. 上游结果写入后立即进入索引，供同一批里的下游规则引用。

失败语义
--------
- **求值失败**（除零、输入缺失、数值不可计算）不是异常，而是一个明确的业务状态：
  写一条 `rule_result`，状态为 `EVIDENCE_INSUFFICIENT`，消息说明具体缺什么。
  它不等于通过，也不等于不合格——它表示这次没算出结论。
- **配置错误**（manifest 里的规则引用不存在、算式节点类型未知）抛 BusinessError，
  让整个节点失败。配置错误不该被当成「这条规则没结论」掩盖过去。

RAG 不参与本模块：数值一律来自字段事实与上游计算结果。
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from ...domain import calculation as calc
from ...domain.calculation import schema as calc_schema
from ...errors import BusinessError
from ...models import AuditTask, NodeExecution, RuleResult, TaskFact
from .shared import _material_names


def _node_calculation_codes(manifest: dict[str, Any], node_code: str) -> set[str]:
    """本节点冻结的计算规则编号集合（节点计划的 `config_json.calculation_rule_codes`）。

    范围随节点计划在发布期固化，运行期只读它：重新按规则内容推断归属会让
    「用户在页面上配的计算节点」与「实际执行的计算」不一致。
    """
    for item in manifest.get("nodes") or []:
        if str(item.get("node_code")) != node_code:
            continue
        config = item.get("config_json")
        if not isinstance(config, dict):
            return set()
        return {str(code) for code in (config.get("calculation_rule_codes") or []) if str(code)}
    raise BusinessError(
        "AUDIT_NODE_NOT_IN_SCENARIO",
        f"计算节点“{node_code}”不在本任务冻结的节点计划里",
        status_code=500,
        details={"node": node_code},
    )


def _calculation_rules(manifest: dict[str, Any], enabled_codes: set[str]) -> list[dict[str, Any]]:
    """本任务范围内的计算规则，按依赖拓扑序排列。

    拓扑序用**确定性的**顺序（同级按 rule_code 排序），因此同一份配置每次执行
    得到同一个顺序，重跑与首次执行的结果不会因为顺序不同而不同。
    """
    rules = [
        item
        for item in manifest.get("calculations", [])
        if item.get("rule_code") in enabled_codes and item.get("status") != "DISABLED"
    ]
    by_code = {item["rule_code"]: item for item in rules}
    ordered: list[dict[str, Any]] = []
    state: dict[str, int] = {}

    def visit(code: str) -> None:
        marker = state.get(code, 0)
        if marker == 2:
            return
        if marker == 1:
            raise BusinessError(
                "CALC_DEPENDENCY_CYCLE",
                f"计算规则“{code}”的依赖形成环，无法确定执行顺序",
                status_code=500,
                details={"rule_code": code},
            )
        state[code] = 1
        rule = by_code.get(code)
        if rule is not None:
            for dependency in sorted(rule.get("depends_on_result_codes") or []):
                visit(dependency)
            ordered.append(rule)
        state[code] = 2

    for code in sorted(by_code):
        visit(code)
    return ordered


def _field_facts(db: Session, task: AuditTask, execution: NodeExecution) -> dict[tuple[str, str, str, str], calc.FactValue]:
    """当前有效的字段抽取事实，按计算参数的定位键建索引。

    只取 `fact_kind = EXTRACTED` 且 `is_current` 的事实：历史版本与人工修正版本
    不参与计算——重跑会新增版本，把历史版本也算进来会让同一个数被算两遍。
    """
    facts: dict[tuple[str, str, str, str], calc.FactValue] = {}
    rows = db.scalars(
        select(TaskFact).where(
            TaskFact.audit_task_id == task.id,
            TaskFact.fact_kind == "EXTRACTED",
            TaskFact.is_current.is_(True),
            TaskFact.decimal_value.is_not(None),
        )
    ).all()
    for row in rows:
        facts[
            calc.fact_key(
                source_type="FIELD",
                source_code=row.fact_code,
                period_scope=row.period_scope,
                statement_scope=row.statement_scope,
            )
        ] = calc.FactValue(
            fact_id=row.id,
            fact_code=row.fact_code,
            revision_no=row.revision_no,
            value=row.decimal_value,
            unit=row.unit,
            currency=row.currency,
            source_execution_id=row.source_execution_id,
            period_scope=row.period_scope,
            statement_scope=row.statement_scope,
            material_key=row.material_key,
        )
    return facts


def _compiled_rule(payload: dict[str, Any]) -> calc.CalculationRule:
    """把 manifest 里冻结的计算规则还原成可执行结构。

    还原失败说明这份 manifest 的计算段不完整或版本不兼容——明确报错，
    不尝试用缺省的字段补出一份「能跑」的规则。
    """
    try:
        definition = calc.CalculationDefinition.model_validate(
            {
                "rule_code": payload["rule_code"],
                "rule_name": payload["rule_name"],
                "calculation_type": payload["calculation_type"],
                "formula": payload["formula"],
                "result_code": payload["result_code"],
                "result_type": payload["result_type"],
                "unit": payload.get("unit"),
                "currency": payload.get("currency"),
                "period_policy": payload["period_policy"],
                # 报表口径与「结果期间」一样是执行契约的必填项：派生事实按它落库，
                # 下游按它定位。漏传会让任何已发布配置在这里直接还原失败。
                "statement_scope": payload["statement_scope"],
                "status": payload.get("status") or "CLEAR",
                "remark": payload.get("remark") or "",
            }
        )
        parameters = [
            calc.CalculationParameter.model_validate(item) for item in payload.get("parameters") or []
        ]
        bands = [calc.JudgementBand.model_validate(item) for item in payload.get("bands") or []]
    except Exception as exc:  # noqa: BLE001 - 契约错误要转成可识别的业务错误
        raise BusinessError(
            "CALC_MANIFEST_RULE_INVALID",
            f"已发布配置里的计算规则“{payload.get('rule_code')}”不符合执行契约，无法执行",
            status_code=500,
            details={"rule_code": payload.get("rule_code"), "reason": str(exc)},
        ) from exc
    return calc.CalculationRule(
        definition=definition,
        parameters=parameters,
        bands=bands,
        ast=payload["ast"],
        expression_hash=payload.get("expression_hash") or "",
        # 参数定位口径随编译产物冻结，运行期直接读它建立字段事实的定位键。
        # 漏还原会让每个参数都取不到事实（报「输入缺失」而看不出原因）。
        parameter_scopes={
            str(key): {str(k): str(v) for k, v in (value or {}).items()}
            for key, value in (payload.get("parameter_scopes") or {}).items()
        },
        depends_on_result_codes=list(payload.get("depends_on_result_codes") or []),
        depends_on_field_codes=list(payload.get("depends_on_field_codes") or []),
        node_instance_code=str(payload.get("node_instance_code") or ""),
    )


def _write_fact(
    db: Session,
    task: AuditTask,
    execution: NodeExecution,
    *,
    rule: calc.CalculationRule,
    result: calc.EvaluationResult,
    judgement: calc.JudgementResult | None,
) -> TaskFact:
    """写入一条计算派生事实。

    事实不可变：同一个 (事实编号, 期间, 报表口径, 主体范围) 已有当前事实时，
    先把它置为非当前再插入新版本，绝不原地改写（改写会被数据库触发器拒绝）。
    """
    definition = rule.definition
    previous = db.scalars(
        select(TaskFact).where(
            TaskFact.audit_task_id == task.id,
            TaskFact.fact_code == definition.result_code,
            TaskFact.material_key.is_(None),
            TaskFact.period_scope == definition.period_policy,
            TaskFact.statement_scope == definition.statement_scope,
            TaskFact.entity_scope == "SELF",
            TaskFact.is_current.is_(True),
        )
    ).all()
    revision_no = 1
    if previous:
        revision_no = max(item.revision_no for item in previous) + 1
        db.execute(
            update(TaskFact)
            .where(TaskFact.id.in_([item.id for item in previous]))
            .values(is_current=False)
        )
        db.flush()
    fact = TaskFact(
        audit_task_id=task.id,
        scenario_version_id=task.scenario_version_id,
        fact_code=definition.result_code,
        fact_kind="DERIVED",
        material_key=None,
        entity_scope="SELF",
        # 派生事实的期间与报表口径来自计算定义自己声明的「结果期间 / 报表口径」：
        # 硬编码成「合并报表」会让声明了母公司口径的计算结果被下游按合并口径取走。
        period_scope=definition.period_policy,
        statement_scope=definition.statement_scope,
        value_type="DECIMAL",
        decimal_value=result.value,
        unit=definition.unit,
        currency=definition.currency,
        source_execution_id=execution.id,
        input_snapshot_json=calc.input_snapshot(rule=rule, result=result),
        judgement_json=judgement.to_payload() if judgement is not None else None,
        calculation_status=result.status,
        calculation_rule_code=definition.rule_code,
        revision_no=revision_no,
        is_current=True,
        status=result.status,
    )
    db.add(fact)
    db.flush()
    return fact


def _result_payload(
    *,
    rule: calc.CalculationRule,
    result: calc.EvaluationResult,
    judgement: calc.JudgementResult | None,
) -> dict[str, Any]:
    definition = rule.definition
    payload: dict[str, Any] = {
        "rule_code": definition.rule_code,
        "result_code": definition.result_code,
        "calculation_type": definition.calculation_type,
        "formula": definition.formula,
        "calculation_status": result.status,
        "standard": {
            "operator_label": "计算",
            "requirement": definition.formula,
            "comparison_label": "计算结果与判定区间比较" if rule.bands else "本条计算没有配置判定区间",
            "clear": bool(rule.bands),
            **({} if rule.bands else {"missing": "该计算规则没有配置判定区间，无法形成业务结论"}),
        },
        "comparison": {
            "kind": "CALCULATION",
            "left_label": "计算结果",
            "left_value": None if result.value is None else format(result.value, "f"),
            "right_label": "判定区间",
            "right_value": "；".join(band.describe() for band in rule.bands) or "未配置",
            "expression": definition.formula,
            "mode_label": "落在哪个判定区间内",
        },
    }
    if judgement is not None:
        payload["judgement"] = judgement.to_payload()
    if result.reason:
        payload["reason"] = result.reason
    if result.missing_parameters:
        payload["missing_parameters"] = list(result.missing_parameters)
    return payload


def _calculation(
    db: Session,
    task: AuditTask,
    manifest: dict[str, Any],
    execution: NodeExecution,
    node_code: str | None = None,
) -> dict[str, Any]:
    """计算节点的主入口。

    `node_code` 是本节点在场景里的**实例码**，由 `flow._dispatch` 传入。规则按它归属：
    同一份计算规则可以被多个计算节点引用，各自按自己的上游事实执行。
    拿不到实例码即明确报错，**不用能力类型顶替**——用能力类型当实例码会让过滤条件
    恒为假，节点返回「本模板未配置计算规则」并记为 SKIPPED，而模板其实配了计算。
    """
    if not node_code:
        raise BusinessError(
            "AUDIT_NODE_UNKNOWN",
            "计算节点执行时拿不到节点实例码，无法确定本次要执行哪些计算规则",
            status_code=500,
            details={"node": task.current_node},
        )
    # 本次执行的计算规则范围取自**节点计划里冻结的** `calculation_rule_codes`
    # （发布期由 build_node_plan 按该节点实例固化）。不读 task_schema_json 里的
    # `enabled_rule_codes`：那个键没有任何写入方，回退到 `enabled_rule_keys`
    # 又拿稳定规则键去比计算规则的业务编号，两者永不相交。
    enabled_codes = _node_calculation_codes(manifest, node_code)
    rules = [
        item
        for item in _calculation_rules(manifest, enabled_codes)
        if str(item.get("node_instance_code") or "") == node_code
    ]
    if not rules:
        configured = any(
            str(item.get("node_instance_code") or "") == node_code
            for item in manifest.get("calculations", [])
        )
        return {
            "rule_count": 0,
            "status_counts": {},
            "check_configured": False,
            "skipped": True,
            "skip_reason": "本方案未启用计算规则" if configured else "本模板未配置计算规则",
        }
    facts = _field_facts(db, task, execution)
    material_names = _material_names(manifest)
    counts: dict[str, int] = {}
    computed = 0
    for payload in rules:
        rule = _compiled_rule(payload)
        result = calc.evaluate_rule(rule=rule, facts=facts)
        judgement: calc.JudgementResult | None = None
        if result.status == calc.CALC_STATUS_COMPUTED and result.value is not None:
            judgement = calc.judge(result.value, rule.bands)
        fact = _write_fact(db, task, execution, rule=rule, result=result, judgement=judgement)
        if result.status == calc.CALC_STATUS_COMPUTED:
            computed += 1
            # 上游结果立即进入索引：同一批里的下游规则要按它取值，
            # 用 result_code 定位，与计算参数的 CALC_RESULT 来源一一对应。
            facts[
                calc.fact_key(
                    source_type="CALC_RESULT",
                    source_code=rule.definition.result_code,
                    period_scope=rule.definition.period_policy,
                    statement_scope=rule.definition.statement_scope,
                )
            ] = calc.FactValue(
                fact_id=fact.id,
                fact_code=rule.definition.result_code,
                revision_no=fact.revision_no,
                value=result.value,
                unit=rule.definition.unit,
                currency=rule.definition.currency,
                source_execution_id=execution.id,
                period_scope=rule.definition.period_policy,
                statement_scope=rule.definition.statement_scope,
                material_key=None,
            )
        status = _rule_status_of(result, judgement)
        counts[status] = counts.get(status, 0) + 1
        db.add(
            RuleResult(
                audit_task_id=task.id,
                node_execution_id=execution.id,
                # rule_key 与 rule_code 取同一个业务编号：唯一键仍建在 rule_key 上，
                # 历史读取路径与新的业务编号路径看到的是同一条规则。
                rule_key=rule.definition.rule_code,
                rule_code=rule.definition.rule_code,
                legacy_rule_key=None,
                rule_name=rule.definition.rule_name,
                status=status,
                message=_message_of(result, judgement),
                result_json=_result_payload(rule=rule, result=result, judgement=judgement),
            )
        )
    return {
        "rule_count": len(rules),
        "computed_count": computed,
        "status_counts": counts,
        "check_configured": True,
        "skipped": False,
        "skip_reason": None,
        "material_names": material_names,
    }


def _rule_status_of(result: calc.EvaluationResult, judgement: calc.JudgementResult | None) -> str:
    """计算结果 → 规则结果状态。

    映射是显式的，不做任何「好/坏」推断：

    - 没算出来（除零 / 输入缺失 / 数值不可计算）→ `EVIDENCE_INSUFFICIENT`，
      具体原因在消息里。它不是合格，也不是不合格。
    - 算出结果但没有命中任何判定区间 → `ABNORMAL`。配置留下的空隙必须让人看到，
      不能当成通过。
    - 命中判定区间 → `PENDING_REVIEW`。判定区间的结论是**业务文本**
      （「预警」「良好」「优秀」…），系统不解释它是好是坏：把「预警」判成不合格
      等于系统替业务定义了等级语义，而样本里这套语义没有权威口径。
      判定结论与命中的区间随结果一起落库，由人工确认最终业务结论。
    """
    if result.status != calc.CALC_STATUS_COMPUTED or judgement is None:
        return "EVIDENCE_INSUFFICIENT"
    if judgement.status == calc.JUDGEMENT_UNMATCHED:
        return "ABNORMAL"
    return "PENDING_REVIEW"


def _message_of(result: calc.EvaluationResult, judgement: calc.JudgementResult | None) -> str:
    if result.status == calc.CALC_STATUS_DIVISION_BY_ZERO:
        return result.reason or "计算出现除数为 0，该指标本次没有定义"
    if result.status == calc.CALC_STATUS_INPUT_MISSING:
        return result.reason or "计算缺少必要的输入事实，本次无法计算"
    if result.status == calc.CALC_STATUS_UNDECIDABLE:
        return result.reason or "计算无法完成，本次没有结论"
    if judgement is None:
        return "计算已完成，但没有配置判定区间，未形成业务结论"
    if judgement.status == calc.JUDGEMENT_UNMATCHED:
        return f"计算结果 {judgement.compared_value} 没有命中任何判定区间"
    return f"计算结果 {judgement.compared_value} 命中判定“{judgement.band.judgement}”"
