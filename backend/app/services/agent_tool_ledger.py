"""Agent 工具调用账本：请求与结果的持久化、幂等恢复与协议冲突判定。

checkpoint 不构成审计账本：恢复时无法判断某个 `tool_call_id` 是否已执行过（只能重跑，
而工具可能已产生真实外部效果）、工具结果与当时的事实快照失去绑定、模型看到的是哪一版
工具定义无从查证。因此请求与结果落在 `agent_tool_call`，与 checkpoint 各自提交、
通过 `tool_call_id` 对账。

「同一次调用」的判据是完整调用身份：同一个 `tool_call_id` 必须逐列对得上（任务、评估批次、
规则实例、工具与版本、工具目录版本与内容指纹、工具策略版本、参数指纹、事实快照引用与
内容指纹、调用方身份）。任一项不同就是两次不同的调用，抛 `TOOL_CALL_PROTOCOL_CONFLICT`，
不按新参数重算、不静默覆盖。「一个为空、一个非空」同样属于冲突——用缺失绕过比对等于把
身份校验变成「两边都写了才比」。

恢复按原记录状态分四种：SUCCEEDED 读原结果不重跑；RUNNING 进待核查不重复执行；
FAILED 返回原错误不自动重试（明确重试必须换新调用标识并关联原调用）；身份任一项不同
抛协议冲突。终态一经形成不允许改为其他状态，只有 `mark_*` 能写终态且每次先断言当前状态；
数据库触发器是同一约束的第二道保证。
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime
from typing import Any

from ..domain import tool_catalog
from ..domain.tool_context import ToolExecutionContext
from ..common.exceptions import BusinessError
from ..entity import AgentToolCall
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session


logger = logging.getLogger(__name__)

# 工具调用状态。与数据库列的取值一一对应。
TOOL_CALL_PENDING = "PENDING"
TOOL_CALL_RUNNING = "RUNNING"
TOOL_CALL_SUCCEEDED = "SUCCEEDED"
TOOL_CALL_FAILED = "FAILED"

# 恢复判定的结果。调用方按它决定是复用、核查还是拒绝。
RECOVERY_REUSE = "REUSE"                 # 已有成功结果，直接复用
RECOVERY_RECONCILE = "NEEDS_RECONCILE"   # 上次停在执行中，状态未知，必须核查
RECOVERY_RETRY = "RETRY"                 # 上次失败，保留原错误，需新的调用标识才能重试
RECOVERY_EXECUTE = "EXECUTE"             # 无记录，正常执行


def arguments_fingerprint(arguments: Any) -> str:
    """调用参数的内容指纹。

    用规范化 JSON（键排序、无多余空白）算：同一份参数在不同序列化下必须得到同一个指纹，
    否则恢复时会把「同一次调用」误判成协议冲突。
    """
    canonical = json.dumps(arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def snapshot_hash_of(context: ToolExecutionContext | None) -> str | None:
    """事实快照的内容指纹。没有快照身份时返回 None（由调用方按缺失处理）。"""
    if context is None:
        return None
    if context.fact_snapshot_hash:
        return context.fact_snapshot_hash
    if context.fact_snapshot_ref:
        return hashlib.sha256(context.fact_snapshot_ref.encode("utf-8")).hexdigest()
    return None


def _conflict(message: str, **details: Any) -> BusinessError:
    return BusinessError(
        "TOOL_CALL_PROTOCOL_CONFLICT",
        message,
        details=details,
    )


def _record_of(db: Session, tool_call_id: str) -> AgentToolCall | None:
    return db.scalar(select(AgentToolCall).where(AgentToolCall.tool_call_id == tool_call_id))


def _same(left: Any, right: Any) -> bool:
    """「同一次调用」的逐列比较口径。

    两边都为空算相同；一边为空、一边非空算不同——缺失不是通配符。
    """
    return left == right and (left is None) == (right is None)


def _assert_same_call(
    record: AgentToolCall,
    *,
    task_id: int | None,
    evaluation_id: str | None,
    rule_instance_id: str | None,
    tool_name: str,
    tool_version: str | None,
    catalog_version: str | None,
    catalog_hash: str | None,
    policy_version: str | None,
    arguments: Any,
    snapshot_ref: str | None,
    snapshot_hash: str | None,
    caller_identity: str | None,
) -> None:
    """同一个 tool_call_id 必须对应同一次调用，逐列核对完整调用身份。

    参数或快照身份变了就不是同一次调用：按新参数执行会让账本与真实执行内容不一致。
    这里明确报冲突，不静默覆盖原记录、也不按新参数重算。
    """
    recorded = [
        ("audit_task_id", record.audit_task_id, task_id),
        ("evaluation_id", record.evaluation_id, evaluation_id),
        ("rule_instance_id", record.rule_instance_id, rule_instance_id),
        ("tool_name", record.tool_name, tool_name),
        ("tool_version", record.tool_version, tool_version),
        ("tool_catalog_version", record.tool_catalog_version, catalog_version),
        ("tool_catalog_hash", record.tool_catalog_hash, catalog_hash),
        ("tool_policy_version", record.tool_policy_version, policy_version),
        ("fact_snapshot_ref", record.fact_snapshot_ref, snapshot_ref),
        ("input_snapshot_hash", record.input_snapshot_hash, snapshot_hash),
        ("caller_identity", record.caller_identity, caller_identity),
    ]
    differing = {
        name: {"recorded": old, "requested": new}
        for name, old, new in recorded
        if not _same(old, new)
    }
    recorded_args = arguments_fingerprint(record.arguments_json)
    requested_args = arguments_fingerprint(arguments)
    if recorded_args != requested_args:
        differing["arguments"] = {
            "recorded": recorded_args,
            "requested": requested_args,
        }
    if differing:
        raise _conflict(
            "同一个工具调用标识对应的调用身份与记录不一致，本次调用未执行；"
            "换参数或换上下文的调用必须使用新的调用标识",
            tool_call_id=record.tool_call_id,
            differing=differing,
        )


def _assert_scope(
    *,
    task_id: int | None,
    evaluation_id: str | None,
    rule_instance_id: str | None,
    snapshot_hash: str | None,
    catalog_version: str | None,
    catalog_hash: str | None,
) -> None:
    """评估身份、快照身份与工具目录身份必须齐备。

    Agent 路径缺失时应显式失败：写一条没有评估批次、没有规则实例、没有快照
    或没有目录身份的记录，等于让这次调用无法被任何一次评估对上账，
    也无法回答「模型当时看到的是哪一版工具定义」。
    """
    missing = [
        name
        for name, value in (
            ("audit_task_id", task_id),
            ("evaluation_id", evaluation_id),
            ("rule_instance_id", rule_instance_id),
            ("fact_snapshot_hash", snapshot_hash),
            ("tool_catalog_version", catalog_version),
            ("tool_catalog_hash", catalog_hash),
        )
        if value in (None, "")
    ]
    if missing:
        raise BusinessError(
            "TOOL_CALL_SCOPE_MISSING",
            "工具调用缺少评估身份、事实快照身份或工具目录身份，本次调用未执行",
            status_code=500,
            details={"missing": missing},
        )


def _retry_origin(db: Session, record: AgentToolCall) -> AgentToolCall | None:
    """本调用是否是对上一次失败调用的明确重试。

    只认「同一次评估、同一条规则实例、同一工具、同一参数」的最近一条失败记录：
    模型换了一个新的调用标识再来一次，就是重试；把原标识写进
    `retry_of_tool_call_id`，两次调用因此各自可追溯。
    找不到对应失败记录时返回 None——这是一次全新调用，不是重试。
    """
    candidate = db.scalar(
        select(AgentToolCall)
        .where(
            AgentToolCall.tool_call_id != record.tool_call_id,
            AgentToolCall.audit_task_id == record.audit_task_id,
            AgentToolCall.evaluation_id == record.evaluation_id,
            AgentToolCall.rule_instance_id == record.rule_instance_id,
            AgentToolCall.tool_name == record.tool_name,
            AgentToolCall.status == TOOL_CALL_FAILED,
        )
        .order_by(AgentToolCall.id.desc())
    )
    if candidate is None:
        return None
    if arguments_fingerprint(candidate.arguments_json) != arguments_fingerprint(record.arguments_json):
        return None
    return candidate


def begin_call(
    db: Session,
    *,
    tool_call_id: str,
    tool_name: str,
    arguments: Any,
    context: ToolExecutionContext,
) -> tuple[AgentToolCall, str]:
    """登记一次工具调用，返回（记录, 恢复判定）。

    判定告诉调用方该做什么：`REUSE` 已有成功结果，直接取 `record.result_json` 不执行；
    `NEEDS_RECONCILE` 上次停在执行中，状态未知必须核查；`RETRY` 上次失败，调用方把原错误
    交回模型，重试必须由模型发出新的调用标识并自动关联到这条失败记录；`EXECUTE` 无记录可以执行。

    工具目录身份只从 `context` 取，没有「取不到就用当前目录常量」的回落——用当前目录身份
    覆盖旧任务的冻结身份，会让同一个 `tool_call_id` 在恢复时被判成协议冲突，本可复用的成功
    结果反而失败。

    本函数不执行工具：执行由调用方在拿到判定后决定，避免「登记即执行」让恢复路径无法插入。
    """
    if context is None:
        raise BusinessError(
            "TOOL_CALL_SCOPE_MISSING",
            "工具调用缺少运行期上下文（评估身份、快照与策略版本），本次调用未执行",
            status_code=500,
        )
    spec = tool_catalog.tool_spec(tool_name)
    snapshot_hash = snapshot_hash_of(context)
    _assert_scope(
        task_id=context.task_id,
        evaluation_id=context.evaluation_id,
        rule_instance_id=context.rule_instance_id,
        snapshot_hash=snapshot_hash,
        catalog_version=context.tool_catalog_version,
        catalog_hash=context.tool_catalog_hash,
    )
    identity = {
        "task_id": context.task_id,
        "evaluation_id": context.evaluation_id,
        "rule_instance_id": context.rule_instance_id,
        "tool_name": tool_name,
        "tool_version": spec.version if spec else None,
        # 冻结的目录身份，不是当前目录的。
        "catalog_version": context.tool_catalog_version,
        "catalog_hash": context.tool_catalog_hash,
        "policy_version": context.tool_policy_version,
        "arguments": arguments,
        "snapshot_ref": context.fact_snapshot_ref,
        "snapshot_hash": snapshot_hash,
        "caller_identity": context.caller_identity,
    }
    existing = _record_of(db, tool_call_id)
    if existing is not None:
        _assert_same_call(existing, **identity)
        if existing.status == TOOL_CALL_SUCCEEDED:
            return existing, RECOVERY_REUSE
        if existing.status == TOOL_CALL_RUNNING:
            # 可能已经发出但结果未知：直接重跑会让有副作用的工具执行两次。
            return existing, RECOVERY_RECONCILE
        if existing.status == TOOL_CALL_FAILED:
            # 不自动重试：把原错误交回调用方。重试必须换新的调用标识。
            return existing, RECOVERY_RETRY
        return existing, RECOVERY_EXECUTE
    record = AgentToolCall(
        audit_task_id=context.task_id,
        evaluation_id=context.evaluation_id,
        rule_instance_id=context.rule_instance_id,
        tool_call_id=tool_call_id,
        tool_name=tool_name,
        tool_version=identity["tool_version"],
        tool_catalog_version=identity["catalog_version"],
        tool_catalog_hash=identity["catalog_hash"],
        tool_policy_version=context.tool_policy_version,
        arguments_json=arguments if isinstance(arguments, dict) else {"value": arguments},
        input_snapshot_hash=snapshot_hash,
        fact_snapshot_ref=context.fact_snapshot_ref,
        caller_identity=context.caller_identity,
        status=TOOL_CALL_PENDING,
        started_at=datetime.now(UTC),
    )
    try:
        # 自己的 SAVEPOINT：并发登记同一个 tool_call_id 时只回滚这一次插入，
        # 外层事务仍可继续，回查原记录后才可能按恢复情形处理。
        with db.begin_nested():
            db.add(record)
            db.flush()
    except IntegrityError as exc:
        if not _is_tool_call_id_conflict(exc):
            raise
        concurrent = _record_of(db, tool_call_id)
        if concurrent is None:
            raise BusinessError(
                "TOOL_CALL_CONCURRENT_REGISTRATION",
                "同一个工具调用标识正在被另一次执行登记，本次调用未执行；请稍后重试",
                status_code=409,
                details={"tool_call_id": tool_call_id},
            ) from exc
        _assert_same_call(concurrent, **identity)
        if concurrent.status == TOOL_CALL_SUCCEEDED:
            return concurrent, RECOVERY_REUSE
        if concurrent.status == TOOL_CALL_RUNNING:
            return concurrent, RECOVERY_RECONCILE
        if concurrent.status == TOOL_CALL_FAILED:
            return concurrent, RECOVERY_RETRY
        return concurrent, RECOVERY_EXECUTE
    origin = _retry_origin(db, record)
    if origin is not None:
        # 同一个工具调用标识不能被重放，因此「重试」只能是一条新记录；
        # 这里把新记录关联回原调用，两次执行各自留痕、也能看出这是同一次重试。
        record.retry_of_tool_call_id = origin.tool_call_id
        db.flush()
    return record, RECOVERY_EXECUTE


def _is_tool_call_id_conflict(exc: IntegrityError) -> bool:
    """只认「tool_call_id 唯一约束」这一种冲突。

    其余 `IntegrityError`（外键、长度、其它唯一键）说明数据或代码有问题，
    必须原样上抛——把它们当成「并发登记」会让真实缺陷静默消失。
    """
    orig = getattr(exc, "orig", None)
    args = getattr(orig, "args", None) or ()
    code = args[0] if args else None
    # MySQL 1062 = ER_DUP_ENTRY。
    if code != 1062:
        return False
    return "uq_agent_tool_call_id" in str(orig)


def _assert_status(record: AgentToolCall, *, allowed: frozenset[str], action: str) -> None:
    if record.status not in allowed:
        raise BusinessError(
            "TOOL_CALL_STATUS_CONFLICT",
            f"工具调用“{record.tool_name}”当前状态为 {record.status}，不能{action}",
            status_code=409,
            details={"tool_call_id": record.tool_call_id, "status": record.status, "action": action},
        )


def mark_running(db: Session, record: AgentToolCall) -> None:
    """标记为执行中。

    这一步必须先提交：工具可能真实调用外部服务，结果未知时进程被杀，
    留下的 `RUNNING` 正是「状态未知、必须核查」的真实表达。

    终态记录不允许再进入执行中（`SUCCEEDED` / `FAILED` 都是终态）。
    """
    _assert_status(record, allowed=frozenset({TOOL_CALL_PENDING}), action="重新进入执行中")
    record.status = TOOL_CALL_RUNNING
    record.started_at = datetime.now(UTC)
    db.flush()


def mark_succeeded(db: Session, record: AgentToolCall, result: dict[str, Any]) -> None:
    """写终态成功。只允许从 PENDING / RUNNING 进入。"""
    _assert_status(record, allowed=frozenset({TOOL_CALL_PENDING, TOOL_CALL_RUNNING}), action="标记为成功")
    record.status = TOOL_CALL_SUCCEEDED
    record.result_json = result
    record.error_json = None
    record.completed_at = datetime.now(UTC)
    db.flush()


def mark_failed(db: Session, record: AgentToolCall, error: BusinessError | Exception) -> None:
    """写终态失败。错误原样保留：是否重试由明确动作决定，不由系统自动重试。"""
    _assert_status(record, allowed=frozenset({TOOL_CALL_PENDING, TOOL_CALL_RUNNING}), action="标记为失败")
    record.status = TOOL_CALL_FAILED
    record.result_json = None
    if isinstance(error, BusinessError):
        record.error_json = {
            "code": error.code,
            "message": error.message,
            "status_code": error.status_code,
            "details": error.details,
        }
    else:
        record.error_json = {"code": "TOOL_CALL_UNEXPECTED_ERROR", "message": str(error)}
    record.completed_at = datetime.now(UTC)
    db.flush()


def reuse_result(record: AgentToolCall) -> dict[str, Any]:
    """复用已成功的结果。

    结果缺失说明记录与结果不一致（不该发生）：明确报错，不用空结果冒充成功。
    """
    if record.status != TOOL_CALL_SUCCEEDED or not isinstance(record.result_json, dict):
        raise BusinessError(
            "TOOL_CALL_RESULT_MISSING",
            "工具调用记录标记为成功但没有可复用的结果，本次调用未执行",
            status_code=500,
            details={"tool_call_id": record.tool_call_id, "status": record.status},
        )
    return record.result_json


def reconcile_message(record: AgentToolCall) -> dict[str, Any]:
    """执行中记录的核查说明。

    不返回结果、也不重新执行：返回「上次可能已经发出、状态未知」这一事实，
    由调用方（Agent 循环或人工）决定后续动作。
    """
    return {
        "status": "NEEDS_RECONCILE",
        "message": (
            f"工具“{record.tool_name}”的上一次调用停在执行中，结果未知；"
            "系统不会直接重跑，需要先核查该次调用是否已经产生效果"
        ),
        "tool_call_id": record.tool_call_id,
        "started_at": record.started_at.isoformat() if record.started_at else None,
    }


def failed_message(record: AgentToolCall) -> dict[str, Any]:
    """失败记录的原始错误。

    是否重试由新的调用标识触发，不在这里自动重试：同一个标识重跑会让一条
    记录先后对应两次真实执行。返回值里明确说明这一点，模型据此换标识再来一次。
    """
    error = record.error_json if isinstance(record.error_json, dict) else {}
    return {
        "status": "FAILED",
        "message": error.get("message") or "上一次工具调用失败",
        "code": error.get("code"),
        "tool_call_id": record.tool_call_id,
        "retry_requires_new_call_id": True,
    }


def calls_of_evaluation(db: Session, *, task_id: int, evaluation_id: str) -> list[AgentToolCall]:
    """一次评估的全部工具调用，按发生顺序。

    报告与页面据此列出取证过程；排序用 (started_at, id) 保证同一毫秒内的顺序也稳定。
    """
    return list(
        db.scalars(
            select(AgentToolCall)
            .where(
                AgentToolCall.audit_task_id == task_id,
                AgentToolCall.evaluation_id == evaluation_id,
            )
            .order_by(AgentToolCall.started_at, AgentToolCall.id)
        ).all()
    )


def ledger_view(db: Session, *, task_id: int, evaluation_id: str) -> list[dict[str, Any]]:
    """工具调用账本的只读视图。不含密钥与外部载荷，只给业务定位与结论。"""
    return [call_view(item) for item in calls_of_evaluation(db, task_id=task_id, evaluation_id=evaluation_id)]


def call_view(item: AgentToolCall) -> dict[str, Any]:
    """一条工具调用记录的可读视图。

    字段覆盖调用标识、工具名与状态、冻结时的目录与策略身份、
    快照身份、参数、结果、错误与起止时间。密钥与外部原始载荷不在其中。
    """
    return {
        "tool_call_id": item.tool_call_id,
        "retry_of_tool_call_id": item.retry_of_tool_call_id,
        "tool_name": item.tool_name,
        "tool_version": item.tool_version,
        "status": item.status,
        "evaluation_id": item.evaluation_id,
        "rule_instance_id": item.rule_instance_id,
        # 冻结身份：回答「模型当时看到的是哪一版工具定义、按哪一版策略选工具」。
        # 这两列与策略版本是不同维度，读取方不得互相顶替。
        "tool_catalog_version": item.tool_catalog_version,
        "tool_catalog_hash": item.tool_catalog_hash,
        "tool_policy_version": item.tool_policy_version,
        "fact_snapshot_ref": item.fact_snapshot_ref,
        "input_snapshot_hash": item.input_snapshot_hash,
        "caller_identity": item.caller_identity,
        "arguments": item.arguments_json,
        "result": item.result_json,
        "error": item.error_json,
        "started_at": item.started_at.isoformat() if item.started_at else None,
        "completed_at": item.completed_at.isoformat() if item.completed_at else None,
    }


def evaluations_of_task(db: Session, *, task_id: int) -> list[str]:
    """本任务出现过的评估批次，按首次出现顺序。

    只读接口据此按评估批次列出取证过程；批次为空表示本任务没有 Agent 工具调用，
    这是事实而不是错误。
    """
    return list(
        db.scalars(
            select(AgentToolCall.evaluation_id)
            .where(AgentToolCall.audit_task_id == task_id)
            .group_by(AgentToolCall.evaluation_id)
            .order_by(func.min(AgentToolCall.id))
        ).all()
    )
