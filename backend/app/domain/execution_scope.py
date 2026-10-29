"""执行批次与结果有效性的纯判定。

“当前批次”只有一个口径：每个（任务, 节点）最新一条未失效执行。失效批次保留历史但
退出当前位，所以判断当前结果不能只看 is_current 或 enabled。批次选取在
`services.audit.execution_scope` 的 SQL 里实现，这里只判单条结果是否仍代表本次执行。
"""

import json
from dataclasses import dataclass
from typing import Any

from ..common.exceptions import BusinessError

# 结果失效的机器原因；说明文案由各页面按自己的语境给出。
RESULT_STALE_EXECUTION = "EXECUTION_NOT_CURRENT"
RESULT_STALE_PENDING_RERUN = "PENDING_RERUN"


@dataclass(frozen=True)
class ResultState:
    """规则结果是否仍代表本次执行；只判批次与重跑，不判业务结论。"""

    current: bool
    reason: str | None


def result_state(
    *,
    execution_id: int | None,
    rule_key: str | None,
    current_ids: set[int],
    pending_rerun_keys: set[str],
) -> ResultState:
    """规则结果是否属于当前执行范围；通过、异常、证据不足等业务结论不在这里判断。"""
    if execution_id not in current_ids:
        return ResultState(False, RESULT_STALE_EXECUTION)
    if rule_key is not None and rule_key in pending_rerun_keys:
        return ResultState(False, RESULT_STALE_PENDING_RERUN)
    return ResultState(True, None)


def pending_rerun_keys_of(raw: Any, *, execution_id: int | None = None) -> set[str]:
    """把执行批次保存的续跑清单还原成规则键集合。

    清单必须是数组。字符串按字符拆分会被当成多个规则键，形状不对就是真实的数据问题，
    这里直接报错，不当成空集合或单条规则。
    """
    if raw is None:
        return set()
    if isinstance(raw, (str, bytes, bytearray)):
        try:
            raw = json.loads(raw)
        except ValueError as exc:
            raise BusinessError(
                "AUDIT_PENDING_RERUN_INVALID", "执行批次的续跑清单不是合法 JSON",
                status_code=500, details={"execution_id": execution_id},
            ) from exc
    if not isinstance(raw, list):
        raise BusinessError(
            "AUDIT_PENDING_RERUN_INVALID", "执行批次的续跑清单不是列表",
            status_code=500, details={"execution_id": execution_id},
        )
    return {str(item) for item in raw}
