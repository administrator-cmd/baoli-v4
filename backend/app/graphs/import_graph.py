from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from pydantic import ValidationError

from ..domain import scenario_schema
from ..domain.proposals import ProposalBundle
from ..domain.workbook import WorkbookIR
from ..common.exceptions import BusinessError
from ..runtime.checkpoint import mysql_saver
from ..services.llm_service import convert_workbook_with_notes
from ..services.storage_service import resolve_storage_path
from ..services.workbook_service import read_workbook_ir


logger = logging.getLogger(__name__)

# 检查点存储暂时不可达（连接/读取异常）与「检查点确实不在等待」是两件事：
# 调用方据此区分“本次操作可重试”与“业务等待点已丢失”，不能一律按缺失处理。
CHECKPOINT_STORAGE_UNAVAILABLE = "CHECKPOINT_STORAGE_UNAVAILABLE"

# 导入图的业务等待点节点名：转换后停留在待裁决处。
IMPORT_DECISION_NODE = "await_import_decision"


class ImportGraphState(TypedDict, total=False):
    job_no: str
    graph_namespace: str
    start_stage: str
    source_path: str
    workbook_id: str
    source_filename: str
    source_size: int
    source_sha256: str
    scenario_code: str
    schema_json: dict[str, Any]
    workbook_ir: dict[str, Any]
    bindings: dict[str, dict[str, Any]]
    proposals: dict[str, Any]
    node_notes: list[dict[str, Any]]
    decisions: list[dict[str, Any]]
    needs_decision: bool
    decision_count: int
    decision_signature: str
    stage: str
    stage_history: list[str]


_START_STAGES = {
    "read_workbook",
    "convert_sheet_chunks",
}


def _state_error(message: str, *, details: dict[str, Any] | None = None) -> BusinessError:
    return BusinessError("IMPORT_GRAPH_STATE_INVALID", message, status_code=500, details=details)


def _required_string(state: ImportGraphState, key: str) -> str:
    value = state.get(key)
    if not isinstance(value, str) or not value:
        raise _state_error("导入流程状态缺少必要文本字段", details={"field": key})
    return value


def _required_int(state: ImportGraphState, key: str) -> int:
    value = state.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise _state_error("导入流程状态缺少必要整数字段", details={"field": key})
    return value


def _required_schema(state: ImportGraphState) -> dict[str, Any]:
    value = state.get("schema_json")
    if not isinstance(value, dict):
        raise _state_error("导入流程缺少场景 schema 快照")
    return scenario_schema.normalize_schema(value)


def _workbook_from_state(state: ImportGraphState) -> WorkbookIR:
    value = state.get("workbook_ir")
    if not isinstance(value, dict):
        raise _state_error("导入流程缺少 WorkbookIR")
    try:
        return WorkbookIR.model_validate(value)
    except ValidationError as exc:
        raise _state_error("导入流程中的 WorkbookIR 不符合契约") from exc


def _bundle_from_state(state: ImportGraphState) -> ProposalBundle:
    value = state.get("proposals")
    if not isinstance(value, dict):
        raise _state_error("导入流程缺少规则转换提案")
    try:
        return ProposalBundle.model_validate(value)
    except ValidationError as exc:
        raise _state_error("导入流程中的规则转换提案不符合契约") from exc


def _stage_update(state: ImportGraphState, stage: str, **updates: Any) -> dict[str, Any]:
    history = list(state.get("stage_history", []))
    if not history or history[-1] != stage:
        history.append(stage)
    return {"stage": stage, "stage_history": history, **updates}


def _decision_targets(bundle: ProposalBundle) -> list[dict[str, Any]]:
    return [
        {
            "target_type": question.target_type,
            "target_ref": question.target_ref,
            "property": question.property,
            "source": question.source.model_dump(mode="json"),
        }
        for sheet in bundle.sheets
        for question in sheet.questions
    ]


def import_decision_signature(items: list[dict[str, Any]]) -> str:
    """为「当前这批待确认事项」的精确集合生成稳定身份。"""
    normalized: list[dict[str, Any]] = []
    for item in items:
        source = item.get("source")
        if not isinstance(source, dict):
            raise _state_error("导入问题缺少来源信息")
        target_type = item.get("target_type")
        target_ref = item.get("target_ref")
        property_name = item.get("property")
        sheet_key = source.get("sheet_key")
        row_no = source.get("row_no")
        cells = source.get("cells")
        if (
            not isinstance(target_type, str)
            or not isinstance(target_ref, str)
            or not isinstance(property_name, str)
            or not isinstance(sheet_key, str)
            or not isinstance(row_no, int)
            or isinstance(row_no, bool)
            or not isinstance(cells, list)
            or any(not isinstance(cell, str) for cell in cells)
        ):
            raise _state_error("导入问题来源信息不完整")
        normalized.append(
            {
                "target_type": target_type,
                "target_ref": target_ref,
                "property": property_name,
                "source": {"sheet_key": sheet_key, "row_no": row_no, "cells": sorted(cells)},
            }
        )
    canonical = json.dumps(
        sorted(normalized, key=lambda item: json.dumps(item, ensure_ascii=False, sort_keys=True)),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _read_workbook(state: ImportGraphState) -> dict[str, Any]:
    schema = _required_schema(state)
    workbook = read_workbook_ir(
        resolve_storage_path(_required_string(state, "source_path")),
        workbook_id=_required_string(state, "workbook_id"),
        source_filename=_required_string(state, "source_filename"),
        source_size=_required_int(state, "source_size"),
        source_sha256=_required_string(state, "source_sha256"),
        profile=scenario_schema.effective_profile(schema),
        scenario_code=_required_string(state, "scenario_code"),
        schema_key=schema["schema_key"],
        schema_version=schema["schema_version"],
    )
    return _stage_update(state, "read_workbook", workbook_ir=workbook.model_dump(mode="json"))


def _build_workbook_ir(state: ImportGraphState) -> dict[str, Any]:
    workbook = _workbook_from_state(state)
    return _stage_update(state, "build_workbook_ir", workbook_ir=workbook.model_dump(mode="json"))


def _convert_sheet_chunks(state: ImportGraphState) -> dict[str, Any]:
    workbook = _workbook_from_state(state)
    bindings = state.get("bindings")
    if not isinstance(bindings, dict):
        raise _state_error("导入流程缺少工作表绑定")
    # 转换入口统一返回（候选集合，节点版附加信息）。附加信息的形状必须按类型判断，
    # 不能把整份 value 当成 notes 列表：对 dict 做 list() 只会得到字符串键，
    # 后续 note.get(...) 会立刻失败。
    schema = _required_schema(state)
    if workbook.scenario_code != _required_string(state, "scenario_code"):
        raise _state_error("导入流程的场景与工作簿读取时不一致")
    if workbook.schema_key != schema["schema_key"] or workbook.schema_version != schema["schema_version"]:
        raise _state_error("导入流程的场景 schema 与工作簿读取时不一致")
    bundle, node_payload = convert_workbook_with_notes(
        workbook, bindings, profile=scenario_schema.effective_profile(schema)
    )
    notes = _node_notes_of(node_payload)
    proposal_json = bundle.model_dump(mode="json")
    questions = [*_decision_targets(bundle), *_node_note_targets(notes)]
    return _stage_update(
        state,
        "convert_sheet_chunks",
        proposals=proposal_json,
        node_notes=notes,
        needs_decision=bool(questions),
        decision_count=len(questions),
        decision_signature=import_decision_signature(questions),
    )


def _node_notes_of(payload: Any) -> list[dict[str, Any]]:
    """从转换附加信息里取出待确认事项列表。

    转换附加信息是包含 `node_notes` / `coverage` / `bundle_hash` 的字典；
    图状态持久化后直接保存 `node_notes` 列表。这里统一两种当前输入形状，
    非空的非法形状明确报错。
    """
    if payload is None:
        return []
    if isinstance(payload, list):
        if any(not isinstance(item, dict) for item in payload):
            raise _state_error("导入流程的待确认事项不是对象列表")
        return list(payload)
    if isinstance(payload, dict):
        raw = payload.get("node_notes")
        if raw is None:
            return []
        if not isinstance(raw, list) or any(not isinstance(item, dict) for item in raw):
            raise _state_error("导入流程的待确认事项不是对象列表")
        return list(raw)
    raise _state_error("导入流程的转换附加信息形状不受支持", details={"type": type(payload).__name__})


def _questions_of_bundle_and_notes(
    bundle: ProposalBundle,
    node_notes: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    return [*_decision_targets(bundle), *_node_note_targets(node_notes)]


def _resolve_field(resolve: Any, key: str, note: dict[str, Any]) -> str:
    """待确认事项的裁决身份字段必须是明确的非空文本，缺失当即报错，不做兜底取值。"""
    value = resolve.get(key) if isinstance(resolve, dict) else None
    if not isinstance(value, str) or not value:
        raise _state_error(
            "待确认事项缺少可裁决的业务对象",
            details={"field": key, "code": note.get("code"), "sheet_key": note.get("sheet_key")},
        )
    return value


def _node_note_targets(node_notes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把节点版的待确认事项投影成与模型 questions 同构的裁决目标。

    只有带 `resolve` 的事项才需要业务作答（确认口径、指定单位币种、明确停用）；
    纯说明性事项（能力缺口、等级差异提醒）不进入裁决集合，它们随覆盖对账与
    能力缺口一起展示，不会因为无法作答而卡住流程。

    目标身份取自 `resolve` 里的真实业务对象（MATERIAL/FIELD/RULE/REPORT_SECTION + 候选引用）与被裁决属性：
    落库的 `ImportIssue.source_json` 与恢复期的 `import_decision_signature` 用的是同一套口径，
    在这里另造一套合成身份会让转换期与恢复期算出两个不同的 decision_signature。
    """
    targets: list[dict[str, Any]] = []
    for note in node_notes:
        resolve = note.get("resolve")
        if not resolve:
            continue
        targets.append(
            {
                "target_type": _resolve_field(resolve, "target_type", note),
                "target_ref": _resolve_field(resolve, "target_ref", note),
                "property": _resolve_field(resolve, "property", note),
                "source": {
                    "sheet_key": note["sheet_key"],
                    "row_no": note["row_no"],
                    "cells": [note["cell"]] if note.get("cell") else [],
                },
            }
        )
    return targets


def node_note_target_ref(note: dict[str, Any]) -> str:
    """待确认事项的裁决目标身份：与 `import_decision_signature` 用同一套口径。

    身份由真实业务对象类型、候选引用与被裁决属性组成，同一事项重算时保持相同；
    落库的 `node_note_target` 因此不会与签名口径脱节。
    """
    resolve = note.get("resolve")
    return "{}:{}#{}".format(
        _resolve_field(resolve, "target_type", note),
        _resolve_field(resolve, "target_ref", note),
        _resolve_field(resolve, "property", note),
    )


def _validate_proposals(state: ImportGraphState) -> dict[str, Any]:
    bundle = _bundle_from_state(state)
    notes = _node_notes_of(state.get("node_notes"))
    questions = _questions_of_bundle_and_notes(bundle, notes)
    expected_count = len(questions)
    expected_signature = import_decision_signature(questions)
    if state.get("needs_decision") != bool(questions):
        raise _state_error("规则转换的待裁决状态与提案内容不一致")
    if state.get("decision_count") != expected_count or state.get("decision_signature") != expected_signature:
        raise _state_error("规则转换的待裁决检查点与提案内容不一致")
    return _stage_update(
        state,
        "validate_proposals",
        proposals=bundle.model_dump(mode="json"),
        needs_decision=bool(questions),
        decision_count=expected_count,
        decision_signature=expected_signature,
    )


def _await_import_decision(state: ImportGraphState) -> dict[str, Any]:
    if not state.get("needs_decision", False):
        return _stage_update(state, "await_import_decision", decisions=state.get("decisions", []))
    decision_count = _required_int(state, "decision_count")
    decision_signature = _required_string(state, "decision_signature")
    decisions = interrupt(
        {
            "job_no": _required_string(state, "job_no"),
            "stage": "集中业务裁决",
            "decision_count": decision_count,
            "decision_signature": decision_signature,
        }
    )
    if not isinstance(decisions, list) or any(not isinstance(item, dict) for item in decisions):
        raise BusinessError("IMPORT_DECISION_RESUME_INVALID", "导入裁决恢复数据不符合契约", status_code=409)
    return _stage_update(state, "await_import_decision", decisions=decisions)


def _route_start(state: ImportGraphState) -> str:
    stage = state.get("start_stage")
    if stage not in _START_STAGES:
        raise _state_error("导入流程起始节点无效", details={"start_stage": stage})
    return stage


def _after_build_workbook_ir(_: ImportGraphState) -> str:
    return "end"


_builder = StateGraph(ImportGraphState)
_builder.add_node("read_workbook", _read_workbook)
_builder.add_node("build_workbook_ir", _build_workbook_ir)
_builder.add_node("convert_sheet_chunks", _convert_sheet_chunks)
_builder.add_node("validate_proposals", _validate_proposals)
_builder.add_node("await_import_decision", _await_import_decision)
_builder.add_conditional_edges(
    START,
    _route_start,
    {
        "read_workbook": "read_workbook",
        "convert_sheet_chunks": "convert_sheet_chunks",
    },
)
_builder.add_edge("read_workbook", "build_workbook_ir")
_builder.add_conditional_edges("build_workbook_ir", _after_build_workbook_ir, {"end": END})
_builder.add_edge("convert_sheet_chunks", "validate_proposals")
_builder.add_edge("validate_proposals", "await_import_decision")
_builder.add_edge("await_import_decision", END)


def _config(job_no: str) -> dict[str, dict[str, str]]:
    """恢复身份只由 thread_id（作业号）决定。

    业务批次（本次转换的检查点命名空间）保存在图 state 的 graph_namespace 里，
    不使用 checkpoint_ns 承载业务批次信息。
    """
    if not job_no:
        raise _state_error("导入流程检查点缺少作业标识")
    return {"configurable": {"thread_id": job_no}}


def _compiled(saver: Any) -> Any:
    # 每次调用都用当次的有效 saver 重新构图；saver 关闭后下次调用可重新读取同一作业。
    return _builder.compile(checkpointer=saver)


def _storage_unavailable(exc: Exception) -> BusinessError:
    """把检查点读取异常转成可识别的错误码，供调用方与「检查点缺失」分开处理。"""
    return BusinessError(
        CHECKPOINT_STORAGE_UNAVAILABLE,
        "导入流程检查点存储暂时不可达，本次操作未完成，可稍后重试",
        status_code=500,
        details={"reason": str(exc) or exc.__class__.__name__},
    )


# 数据库驱动/连接层的异常模块前缀。只有来自它们的失败才是「存储暂时不可达」。
_STORAGE_MODULES = ("sqlalchemy", "pymysql", "MySQLdb", "MySQLdb.connections")


def _graph_failure(exc: Exception) -> BusinessError:
    """图执行失败分类：**存储故障**与**代码缺陷**必须分开报。

    图入口的 `.invoke()` 会执行全部节点，因此节点里抛出的 `NameError` / `KeyError`
    一类的代码缺陷也会走到这里。把它们统一包装成「检查点存储暂时不可达，可稍后重试」
    会让真实原因完全不可见——而重试永远不会成功，问题只能靠翻日志才发现。
    因此只有来自数据库驱动/连接层的异常才按存储故障报，其余原样报出异常类型与消息。
    """
    module = type(exc).__module__ or ""
    if module.startswith(_STORAGE_MODULES):
        return _storage_unavailable(exc)
    return BusinessError(
        "IMPORT_GRAPH_EXECUTION_FAILED",
        f"导入流程执行失败（{type(exc).__name__}）：{exc}",
        status_code=500,
        details={"exception": type(exc).__name__, "reason": str(exc)},
    )




def _result_values(config: dict[str, dict[str, str]], result: dict[str, Any], saver: Any) -> dict[str, Any]:
    try:
        snapshot = _compiled(saver).get_state(config)
    except BusinessError:
        raise
    except Exception as exc:
        raise _graph_failure(exc) from exc
    if not snapshot or not snapshot.values:
        raise BusinessError("IMPORT_CHECKPOINT_MISSING", "导入流程检查点不存在或未记录执行结果", status_code=409)
    values = dict(snapshot.values)
    values["interrupted"] = bool(result.get("__interrupt__"))
    return values


def run_workbook_read(
    *,
    job_no: str,
    source_path: str,
    workbook_id: str,
    source_filename: str,
    source_size: int,
    source_sha256: str,
    scenario_code: str,
    schema_json: dict[str, Any],
) -> dict[str, Any]:
    """导入图入口：读取工作簿并产出 IR。"""
    config = _config(job_no)
    try:
        with mysql_saver() as saver:
            result = _compiled(saver).invoke(
            {
                "job_no": job_no,
                "graph_namespace": "import:read",
                "start_stage": "read_workbook",
                "source_path": source_path,
                "workbook_id": workbook_id,
                "source_filename": source_filename,
                "source_size": source_size,
                "source_sha256": source_sha256,
                "scenario_code": scenario_code,
                "schema_json": schema_json,
            },
            config=config,
            )
            return _result_values(config, result, saver)
    except BusinessError:
        raise
    except Exception as exc:
        raise _graph_failure(exc) from exc


def run_sheet_conversion(
    *,
    job_no: str,
    checkpoint_ns: str,
    workbook_ir: dict[str, Any],
    bindings: dict[str, dict[str, Any]],
    scenario_code: str,
    schema_json: dict[str, Any],
) -> dict[str, Any]:
    """导入图入口：按工作表绑定转换出候选、问题与覆盖对账。"""
    config = _config(job_no)
    try:
        with mysql_saver() as saver:
            result = _compiled(saver).invoke(
            {
                "job_no": job_no,
                "graph_namespace": checkpoint_ns,
                "start_stage": "convert_sheet_chunks",
                "workbook_ir": workbook_ir,
                "bindings": bindings,
                "scenario_code": scenario_code,
                "schema_json": schema_json,
                "decisions": [],
                "stage_history": [],
            },
            config=config,
            )
            return _result_values(config, result, saver)
    except BusinessError:
        raise
    except Exception as exc:
        raise _graph_failure(exc) from exc


def resume_import_decision(
    *,
    job_no: str,
    checkpoint_ns: str,
    decision_signature: str,
    decision_count: int,
    decisions: list[dict[str, Any]],
) -> dict[str, Any]:
    """导入图入口：从裁决等待点继续执行，裁决集合身份不符时明确拒绝。"""
    config = _config(job_no)
    try:
        with mysql_saver() as saver:
            graph = _compiled(saver)
            snapshot = graph.get_state(config)
            if not snapshot:
                raise BusinessError("IMPORT_CHECKPOINT_MISSING", "导入裁决流程检查点不存在或已失效", status_code=409)
            values = snapshot.values
            # 检查点存在不代表处于正确等待点：还要核对业务批次、问题集合内容版本与数量。
            if (
                values.get("graph_namespace") != checkpoint_ns
                or values.get("start_stage") != "convert_sheet_chunks"
                or values.get("needs_decision") is not True
                or values.get("decision_signature") != decision_signature
                or values.get("decision_count") != decision_count
            ):
                raise BusinessError("IMPORT_DECISION_CHECKPOINT_MISMATCH", "导入裁决流程检查点与当前问题集合不一致", status_code=409)
            if not snapshot.next:
                # 图与业务库分别提交：图已记录同一批裁决、业务库却未提交时，原请求可重试。
                if (
                    not snapshot.interrupts
                    and values.get("stage") == IMPORT_DECISION_NODE
                    and values.get("decisions") == decisions
                ):
                    return {**values, "interrupted": False}
                raise BusinessError("IMPORT_CHECKPOINT_MISSING", "导入裁决流程检查点不存在或已失效", status_code=409)
            if IMPORT_DECISION_NODE not in snapshot.next or not snapshot.interrupts:
                raise BusinessError("IMPORT_CHECKPOINT_MISSING", "导入裁决流程检查点不在裁决等待点", status_code=409)
            result = graph.invoke(Command(resume=decisions), config=config)
            if result.get("__interrupt__"):
                raise BusinessError("IMPORT_DECISION_RESUME_INCOMPLETE", "导入裁决流程恢复后仍处于等待状态", status_code=409)
            return _result_values(config, result, saver)
    except BusinessError:
        raise
    except Exception as exc:
        raise _graph_failure(exc) from exc
