from __future__ import annotations

import logging
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from pydantic import ValidationError

from ..domain import scenario_schema
from ..domain.proposals import ProposalBundle
from ..domain.workbook import WorkbookIR
from ..common.exceptions import BusinessError
from ..runtime.checkpoint import is_storage_unavailable, mysql_saver
from ..services.llm_service import convert_workbook_with_notes
from ..services.storage_service import resolve_storage_path
from ..services.workbook_service import read_workbook_ir


logger = logging.getLogger(__name__)

# 检查点存储暂时不可达（连接/读取异常）与「检查点确实不在等待」是两件事：
# 调用方据此区分“本次操作可重试”与“业务等待点已丢失”，不能一律按缺失处理。
CHECKPOINT_STORAGE_UNAVAILABLE = "CHECKPOINT_STORAGE_UNAVAILABLE"



class ImportGraphState(TypedDict, total=False):
    job_no: str
    operation_no: str
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






def _read_workbook(state: ImportGraphState) -> dict[str, Any]:
    schema = _required_schema(state)
    workbook = read_workbook_ir(
        resolve_storage_path(_required_string(state, "source_path")),
        workbook_id=_required_string(state, "workbook_id"),
        source_filename=_required_string(state, "source_filename"),
        source_size=_required_int(state, "source_size"),
        source_sha256=_required_string(state, "source_sha256"),
        profile_version=schema["profile_version"],
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
    # 转换入口统一返回（候选集合，来源附加信息）。附加信息的形状必须按类型判断，
    # 不能把整份 value 当成 notes 列表：对 dict 做 list() 只会得到字符串键，
    # 后续 note.get(...) 会立刻失败。
    schema = _required_schema(state)
    if workbook.scenario_code != _required_string(state, "scenario_code"):
        raise _state_error("导入流程的场景与工作簿读取时不一致")
    if workbook.schema_key != schema["schema_key"] or workbook.schema_version != schema["schema_version"]:
        raise _state_error("导入流程的场景 schema 与工作簿读取时不一致")
    bundle, node_payload = convert_workbook_with_notes(
        workbook, bindings, operation_no=_required_string(state, "operation_no"),
    )
    notes = _node_notes_of(node_payload)
    proposal_json = bundle.model_dump(mode="json")
    return _stage_update(
        state,
        "convert_sheet_chunks",
        proposals=proposal_json,
        node_notes=notes,
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










def _validate_proposals(state: ImportGraphState) -> dict[str, Any]:
    bundle = _bundle_from_state(state)
    notes = _node_notes_of(state.get("node_notes"))
    return _stage_update(
        state,
        "validate_proposals",
        proposals=bundle.model_dump(mode="json"),
    )




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
_builder.add_edge("validate_proposals", END)


def _config(job_no: str, conversion_batch: str | None = None) -> dict[str, dict[str, str]]:
    """每次转换使用独立批次身份，旧转换不能覆盖新批次的检查点。

    业务批次（本次转换的检查点命名空间）保存在图 state 的 graph_namespace 里，
    不使用 checkpoint_ns 承载业务批次信息。
    """
    if not job_no:
        raise _state_error("导入流程检查点缺少作业标识")
    thread_id = f"{job_no}:{conversion_batch}" if conversion_batch is not None else job_no
    return {"configurable": {"thread_id": thread_id}}


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


def _graph_failure(exc: Exception) -> BusinessError:
    """图执行失败分类：存储故障与代码缺陷必须分开报。

    图入口的 `.invoke()` 会执行全部节点，因此节点里抛出的 `NameError` / `KeyError`
    一类的代码缺陷也会走到这里。把它们统一包装成「检查点存储暂时不可达，可稍后重试」
    会让真实原因完全不可见——而重试永远不会成功，问题只能靠翻日志才发现。
    因此只把连接及数据库运行故障按不可达报，约束、SQL 和序列化错误保留异常类型与消息。
    """
    if is_storage_unavailable(exc):
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
    operation_no: str,
    checkpoint_ns: str,
    workbook_ir: dict[str, Any],
    bindings: dict[str, dict[str, Any]],
    scenario_code: str,
    schema_json: dict[str, Any],
) -> dict[str, Any]:
    """导入图入口：按工作表绑定转换出候选与覆盖对账。"""
    config = _config(job_no, checkpoint_ns)
    try:
        with mysql_saver() as saver:
            result = _compiled(saver).invoke(
            {
                "job_no": job_no,
                "graph_namespace": checkpoint_ns,
                "operation_no": operation_no,
                "start_stage": "convert_sheet_chunks",
                "workbook_ir": workbook_ir,
                "bindings": bindings,
                "scenario_code": scenario_code,
                "schema_json": schema_json,
                "stage_history": [],
            },
            config=config,
            )
            return _result_values(config, result, saver)
    except BusinessError:
        raise
    except Exception as exc:
        raise _graph_failure(exc) from exc
