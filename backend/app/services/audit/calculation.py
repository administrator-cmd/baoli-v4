"""计算节点按冻结业务方法调用 LLM，写统一事实与 CSV 规则结果。

流程：取当前有效的字段事实与已计算派生事实，按来源、编号、期间与报表口径建索引 →
按规则依赖做拓扑序执行（保证上游先于下游）→ 每条规则求值、判定、写 `task_fact`(DERIVED)
与 `rule_result` → 上游结果写入后立即进入索引，供同一批里的下游规则引用。

失败语义：求值失败（除零、输入缺失、数值不可计算）不是异常，而是明确的业务状态——写一条
`rule_result`，状态 `EVIDENCE_INSUFFICIENT` 并说明缺什么；它表示这次没算出结论，不等于
通过也不等于不合格。配置错误或模型响应违反契约时抛 BusinessError 让整个
节点失败，不该被当成「这条规则没结论」掩盖过去。

RAG 不参与本模块：数值一律来自字段事实与上游计算结果。
"""

from __future__ import annotations
from .. import task_csv_service

from typing import Any
from decimal import Decimal

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from ...domain import calculation as calc, fact_snapshot, workflow_dag
from ...domain.audit_vocab import SCOPE_NOT_APPLICABLE
from ...domain.identity import public_no
from ...common.exceptions import BusinessError
from ...entity import AuditTask, ManualReview, NodeExecution, RuleResult, TaskFact
from ..llm_calculation import evaluate_with_llm
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

    拓扑序用确定性的顺序（同级按 rule_code 排序），因此同一份配置每次执行
    得到同一个顺序，重跑与首次执行的结果不会因为顺序不同而不同。
    """
    rules = [
        item
        for item in manifest.get("calculations", [])
        if item.get("rule_code") in enabled_codes and item.get("status") != "DISABLED"
    ]
    by_code = {item["rule_code"]: item for item in rules}
    result_owners = {item["result_code"]: item["rule_code"] for item in rules}
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
        rule = by_code[code]
        for dependency in sorted(rule.get("depends_on_result_codes") or []):
            # 依赖保存的是结果编号，必须先定位产出它的规则。其它计算节点的
            # 上游不在本次集合中，由冻结 DAG 保证先执行，不在这里重复计算。
            owner = result_owners.get(dependency)
            if owner is not None:
                visit(owner)
        ordered.append(rule)
        state[code] = 2

    for code in sorted(by_code):
        visit(code)
    return ordered


def _field_facts(db: Session, task: AuditTask) -> dict[tuple[str, str, str, str], calc.FactValue]:
    """当前有效的字段与上游计算事实，按计算参数的定位键建索引。

    EXTRACTED 只取已形成有效值的事实；DERIVED 只取已经 COMPUTED 的事实。
    LLM 可读取数值、日期、文本、布尔和结构化值。
    这样分属不同计算节点的规则也能引用上游结果，未算出值的派生事实不会混入。
    """
    facts: dict[tuple[str, str, str, str], calc.FactValue] = {}
    all_rows = task_csv_service.records(db, task, TaskFact)
    by_id = {row.id: row for row in all_rows}
    material_keys_by_id: dict[int, tuple[str, ...]] = {}
    fields_by_code = {
        str(field.get("source_ref") or field["field_key"]): field
        for field in task.task_schema_json["fields"]
    }

    def source_material_keys(fact_id: int) -> tuple[str, ...]:
        if fact_id in material_keys_by_id:
            return material_keys_by_id[fact_id]
        row = by_id[fact_id]
        if row.fact_kind == "EXTRACTED":
            keys = {row.material_key}
        else:
            # 读取计算当时引用的具体事实版本，包括已退出当前位的历史输入。
            keys = {
                key for item in row.input_snapshot_json["inputs"]
                for key in source_material_keys(item["fact_id"])
            }
        material_keys_by_id[fact_id] = tuple(sorted(key for key in keys if key is not None))
        return material_keys_by_id[fact_id]

    rows = [row for row in all_rows
            if row.is_current
            and ((row.fact_kind == "EXTRACTED" and row.status in {"PRESENT", "REVIEWED"})
                 or (row.fact_kind == "DERIVED" and row.status == calc.CALC_STATUS_COMPUTED))]
    for row in rows:
        value = row.decimal_value if row.value_type in {"DECIMAL", "AMOUNT"} else fact_snapshot.task_fact_value(
            row, field_definition=fields_by_code.get(row.fact_code),
        )
        if value is None:
            continue
        key = calc.fact_key(
            source_type="FIELD" if row.fact_kind == "EXTRACTED" else "CALC_RESULT",
            source_code=row.fact_code,
            period_scope=row.period_scope,
            statement_scope=row.statement_scope,
        )
        if key in facts:
            raise BusinessError(
                "CALC_INPUT_IDENTITY_DUPLICATE", "计算输入存在多个相同身份的当前有效事实",
                status_code=500, details={"fact_code": row.fact_code},
            )
        facts[key] = calc.FactValue(
            fact_id=row.id,
            fact_code=row.fact_code,
            revision_no=row.revision_no,
            value=value,
            unit=row.unit,
            currency=row.currency,
            source_execution_id=row.source_execution_id,
            period_scope=row.period_scope,
            statement_scope=row.statement_scope,
            material_key=row.material_key,
            source_material_keys=source_material_keys(row.id),
            value_type=row.value_type,
        )
    return facts


def _compiled_rule(payload: dict[str, Any]) -> calc.CalculationRule:
    """把 manifest 里冻结的计算规则还原成可执行结构。

    还原失败说明这份 manifest 的计算段不完整或版本不兼容——明确报错，
    不尝试用缺省的字段补出一份「能跑」的规则。
    """
    calc.assert_llm_engine(str(payload.get("rule_code") or ""), payload.get("execution_engine"))
    try:
        definition = calc.CalculationDefinition.model_validate(
            {
                "rule_code": payload["rule_code"],
                "rule_name": payload["rule_name"],
                "calculation_type": payload["calculation_type"],
                "execution_engine": payload["execution_engine"],
                "judgement_instruction": payload.get("judgement_instruction", ""),
                "execution_mode": payload.get("execution_mode", "CHECK"),
                "formula": payload["formula"],
                "result_code": payload["result_code"],
                "result_type": payload["result_type"],
                "unit": payload.get("unit"),
                "currency": payload.get("currency"),
                # 口径仅在业务实际涉及时有具体值；历史或省略配置统一按不适用还原。
                "period_policy": payload.get("period_policy") or SCOPE_NOT_APPLICABLE,
                "statement_scope": payload.get("statement_scope") or SCOPE_NOT_APPLICABLE,
                "status": payload.get("status") or "CLEAR",
                "remark": payload.get("remark") or "",
                "description": payload.get("description") or "",
                "verification_logic": payload.get("verification_logic") or "",
                "bands_text": payload.get("bands_text") or "",
                "risk_level_text": payload.get("risk_level_text") or "",
                "review_advice": payload.get("review_advice") or "",
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
        )
    ).all()
    revision_no = max((item.revision_no for item in previous), default=0) + 1
    current = [item for item in previous if item.is_current]
    if current:
        db.execute(
            update(TaskFact)
            .where(TaskFact.id.in_([item.id for item in current]))
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
        value_type="AMOUNT" if definition.result_type == "AMOUNT" else "DECIMAL",
        decimal_value=result.value,
        unit=definition.unit,
        currency=definition.currency,
        source_execution_id=execution.id,
        input_snapshot_json=calc.input_snapshot(rule=rule, result=result),
        judgement_json=result.llm_result,
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
    manifest: dict[str, Any],
) -> dict[str, Any]:
    definition = rule.definition
    input_names = {
        ("FIELD", item.get("source_ref") or item["field_key"]): item["name"]
        for item in manifest["fields"]
    }
    input_names.update({
        ("CALC_RESULT", item["result_code"]): item["rule_name"] for item in manifest.get("calculations", [])
    })
    material_names = _material_names(manifest)
    value_only = definition.execution_mode == "VALUE_ONLY"
    payload: dict[str, Any] = {
        "rule_code": definition.rule_code,
        "result_code": definition.result_code,
        "result_type": definition.result_type,
        "unit": definition.unit,
        "currency": definition.currency,
        "calculation_type": definition.calculation_type,
        "execution_mode": definition.execution_mode,
        "execution_engine": definition.execution_engine,
        "judgement_instruction": definition.judgement_instruction,
        "formula": definition.formula,
        "calculation_status": result.status,
        "inputs": [
            {
                "fact_id": item.fact_id,
                "fact_code": item.fact_code,
                "revision_no": item.revision_no,
                "material_key": key,
                "material_name": material_names[key],
                "field_name": input_names[("FIELD" if item.material_key is not None else "CALC_RESULT", item.fact_code)],
                "source_type": "FIELD" if item.material_key is not None else "CALC_RESULT",
                "value_type": item.value_type,
                "unit": item.unit,
                "currency": item.currency,
                "normalized_value": format(item.value, "f") if isinstance(item.value, Decimal) else item.value,
            }
            for item in result.inputs
            for key in item.source_material_keys
        ],
        "standard": {
            "operator_label": "计算",
            "requirement": definition.formula,
            "comparison_label": "仅计算中间值，不判断合格或异常" if value_only else "按规则库要求由 AI 辅助判断",
            "clear": True,
        },
        "comparison": {
            "kind": "CALCULATION",
            "left_label": "计算结果",
            "left_value": None if result.value is None else format(result.value, "f"),
            "expression": definition.formula,
            "mode_label": "仅计算中间值" if value_only else "按业务标准判断",
        },
    }
    if not value_only:
        payload["comparison"].update({
            "right_label": "判断标准",
            "right_value": definition.judgement_instruction or "；".join(
                f"{'结果' if band.comparison_basis == 'RESULT' else '结果绝对值'} "
                f"{band.describe()}：{band.judgement}"
                for band in rule.bands
            ),
        })
    if result.reason:
        payload["reason"] = result.reason
    if result.missing_parameters:
        payload["missing_parameters"] = list(result.missing_parameters)
    payload.update(origin="LLM", llm_result=result.llm_result,
                   calculation_steps=result.llm_result["calculation_steps"])
    if not value_only:
        payload["judgement"] = result.llm_result
        payload["risk_level"] = result.llm_result["risk_level"]
        payload["action"] = result.llm_result["action"]
    return payload


def _calculation_review_available(db: Session, task: AuditTask, manifest: dict[str, Any], node_code: str) -> bool:
    plans = workflow_dag.plans_of(manifest)
    by_code = {plan.node_instance_code: plan for plan in plans}
    completed = set(
        db.scalars(
            select(NodeExecution.node_code).where(
                NodeExecution.audit_task_id == task.id,
                NodeExecution.status.in_(("SUCCEEDED", "SKIPPED")),
            )
        ).all()
    )

    def depends_on_calculation(code: str, seen: set[str]) -> bool:
        if code == node_code:
            return True
        if code in seen:
            return False
        return any(depends_on_calculation(dependency, seen | {code}) for dependency in by_code[code].depends_on)

    return any(
        plan.node_type == "MANUAL_REVIEW"
        and plan.enabled
        and plan.node_instance_code not in completed
        and depends_on_calculation(plan.node_instance_code, set())
        for plan in plans
    )


def _calculation(
    db: Session,
    task: AuditTask,
    manifest: dict[str, Any],
    execution: NodeExecution,
    node_code: str | None = None,
) -> dict[str, Any]:
    """计算节点的主入口。

    `node_code` 是本节点在场景里的实例码，由 `flow._dispatch` 传入。规则按它归属：
    不同场景与节点实例共用此执行入口，每次按当前节点的冻结计划选择计算规则。
    拿不到实例码即明确报错，不用能力类型顶替——用能力类型当实例码会让过滤条件
    恒为假，将已配置规则误记为 SKIPPED。
    """
    if not node_code:
        raise BusinessError(
            "AUDIT_NODE_UNKNOWN",
            "计算节点执行时拿不到节点实例码，无法确定本次要执行哪些计算规则",
            status_code=500,
            details={"node": task.current_node},
        )
    # 本次执行的计算规则范围取自节点计划里冻结的 `calculation_rule_codes`
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
            "skip_reason": "本方案未启用计算规则" if configured else "当前节点未配置计算规则",
        }
    compiled_rules = [_compiled_rule(payload) for payload in rules]
    facts = _field_facts(db, task)
    material_names = _material_names(manifest)
    counts: dict[str, int] = {}
    computed = 0
    for rule in compiled_rules:
        result = evaluate_with_llm(rule=rule, facts=facts)
        _write_fact(db, task, execution, rule=rule, result=result)
        if result.status == calc.CALC_STATUS_COMPUTED:
            computed += 1
            # 同一批下游计算也重新读取 CSV，不能直接沿用内存计算值跳过中间存取。
            facts = _field_facts(db, task)
        status = result.llm_result["status"]
        counts[status] = counts.get(status, 0) + 1
        record = RuleResult(
            audit_task_id=task.id,
            node_execution_id=execution.id,
            # rule_key 与 rule_code 取同一个业务编号：唯一键仍建在 rule_key 上，
            # 历史读取路径与新的业务编号路径看到的是同一条规则。
            rule_key=rule.definition.rule_code,
            rule_code=rule.definition.rule_code,
            legacy_rule_key=None,
            rule_name=rule.definition.rule_name,
            status=status,
            message=result.llm_result["message"],
            result_json=_result_payload(rule=rule, result=result, manifest=manifest),
        )
        db.add(record)
        if status == "PENDING_REVIEW":
            if not _calculation_review_available(db, task, manifest, node_code):
                raise BusinessError(
                    "CALC_REVIEW_NODE_UNAVAILABLE",
                    "计算结果需要人工确认，但当前流程没有依赖该计算且尚未执行的人工复核节点",
                    status_code=409,
                    details={"rule_code": rule.definition.rule_code, "calculation_node": node_code},
                )
            db.flush()
            db.add(
                ManualReview(
                    review_no=public_no("REV"),
                    audit_task_id=task.id,
                    rule_result_id=record.id,
                    review_type="RULE_RESULT",
                    status="PENDING",
                    system_judgment_json={
                        "origin": "CALCULATION",
                        "status": status,
                        "message": record.message,
                        "rule_key": record.rule_key,
                        "result_code": rule.definition.result_code,
                        "calculation_status": result.status,
                        "calculated_value": None if result.value is None else format(result.value, "f"),
                        "judgement": result.llm_result,
                        "llm_result": result.llm_result,
                    },
                    business_note=record.message,
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
