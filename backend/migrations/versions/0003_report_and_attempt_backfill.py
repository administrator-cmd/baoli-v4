"""回填旧执行批次的轮次标识与旧报告快照的评估标识。

`node_execution.result_json.attempt_no` 与 `audit_report.snapshot_json.evaluation_id`
都是本次改动之后才开始写入的字段。升级前留下的行没有它们，而读取侧随后按严格口径
读取（缺轮次直接报 `AUDIT_EXECUTION_ATTEMPT_MISSING`，缺评估标识判报告失效），
所以必须在这里补齐，而不是在读取处加兼容分支。

降级只撤销结构相关操作：已回填的数据不还原，缺少这些字段时读取逻辑按缺失处理。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import context, op

revision: str = "0003_report_and_attempt_backfill"
down_revision: Union[str, Sequence[str], None] = "0002_schema_cleanup"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _scalar(statement: str) -> int:
    return int(op.get_bind().execute(sa.text(statement)).scalar() or 0)


def upgrade() -> None:
    if context.is_offline_mode():
        return
    # 1. 升级前的执行批次都只有第一轮；续跑会在该值上加一。
    #    批次也可能在写入任何结果前就被中断，result_json 为 NULL，同样补第一轮；
    #    否则续跑读取轮次会直接报 AUDIT_EXECUTION_ATTEMPT_MISSING。
    op.execute(sa.text(
        "UPDATE node_execution SET result_json = JSON_OBJECT('attempt_no', 1) "
        "WHERE result_json IS NULL"
    ))
    op.execute(sa.text(
        "UPDATE node_execution SET result_json = JSON_SET(result_json, '$.attempt_no', 1) "
        "WHERE result_json IS NOT NULL AND JSON_EXTRACT(result_json, '$.attempt_no') IS NULL"
    ))
    # 2. 报告快照的评估标识取自同一批次同一规则的执行结果；两者一起写入才代表正文有效。
    op.execute(sa.text(
        "UPDATE audit_report r "
        "JOIN rule_result rr ON rr.node_execution_id = r.node_execution_id "
        "AND rr.rule_key = JSON_UNQUOTE(JSON_EXTRACT(r.snapshot_json, '$.rule_key')) "
        "SET r.snapshot_json = JSON_SET(r.snapshot_json, '$.evaluation_id', "
        "JSON_UNQUOTE(JSON_EXTRACT(rr.result_json, '$.evaluation_id'))) "
        "WHERE r.snapshot_json IS NOT NULL "
        "AND JSON_EXTRACT(r.snapshot_json, '$.evaluation_id') IS NULL "
        "AND rr.result_json IS NOT NULL "
        "AND JSON_EXTRACT(rr.result_json, '$.evaluation_id') IS NOT NULL"
    ))
    # 3. 回填不了的报告必须如实报出：它们没有执行批次或没有评估标识，
    #    升级后会被判为失效，这是真实状态，但不能悄悄发生。
    without_execution = _scalar(
        "SELECT COUNT(*) FROM audit_report "
        "WHERE snapshot_json IS NOT NULL AND node_execution_id IS NULL"
    )
    unresolved = _scalar(
        "SELECT COUNT(*) FROM audit_report WHERE snapshot_json IS NOT NULL "
        "AND JSON_EXTRACT(snapshot_json, '$.evaluation_id') IS NULL"
    )
    if without_execution or unresolved:
        print(
            "[0003] 以下报告无法回填评估标识，升级后会被判为失效："
            f"没有执行批次 {without_execution} 份，对应结果缺少评估标识 {unresolved} 份。"
            "重新执行报告规则即可生成有效快照。"
        )


def downgrade() -> None:
    """回填是数据修正，不还原：缺少这些字段时读取逻辑按缺失处理。"""
    return
