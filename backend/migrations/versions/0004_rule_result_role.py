"""给规则结果补上规则角色列。

风险结果列表原先按任务清单判定风险身份：每条规则结果都要解析一次约 690KB 的
manifest_json，分页、总数、等级取值三个查询累积到两秒以上。角色在写入结果时
本来就已经判定过（rule_role / RISK_TRIGGER），这里把它落成一列，并按同一口径回填历史行。

回填按任务逐个执行：每个任务只解析一次整份清单，不随规则结果条数放大。
降级只撤销结构，已回填的角色不还原：缺少该列时读取逻辑不依赖它。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import context, op

revision: str = "0004_rule_result_role"
down_revision: Union[str, Sequence[str], None] = "0003_report_and_attempt_backfill"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# 判定口径与 domain.rule_library.is_risk_rule 一致：清单里声明为风险角色，
# 或执行算子就是风险触发。
ROLE_OF_TASK = (
    "UPDATE rule_result rr "
    "JOIN audit_task t ON t.id = rr.audit_task_id "
    "JOIN JSON_TABLE(t.manifest_json, '$.rules[*]' COLUMNS ("
    "  rule_key VARCHAR(80) PATH '$.rule_key', "
    "  rule_role VARCHAR(20) PATH '$.rule_role', "
    "  op VARCHAR(64) PATH '$.operator'"
    ")) jt ON jt.rule_key = rr.rule_key "
    "SET rr.rule_role = CASE WHEN jt.rule_role = 'RISK' OR jt.op = 'RISK_TRIGGER' "
    "THEN 'RISK' ELSE 'CHECK' END "
    "WHERE rr.rule_role IS NULL"
)


def upgrade() -> None:
    op.add_column("rule_result", sa.Column("rule_role", sa.String(20), nullable=True))
    op.create_index("idx_rule_result_role_task", "rule_result", ["rule_role", "audit_task_id"])
    if context.is_offline_mode():
        op.execute(sa.text(ROLE_OF_TASK))
        op.alter_column("rule_result", "rule_role", existing_type=sa.String(20), nullable=False)
        return
    bind = op.get_bind()
    task_ids = [int(item) for item in bind.execute(sa.text("SELECT id FROM audit_task")).scalars().all()]
    for task_id in task_ids:
        bind.execute(sa.text(ROLE_OF_TASK + " AND rr.audit_task_id = :task_id"), {"task_id": task_id})
    # 计算类结果的 rule_key 是计算业务编号，不在任务清单的规则里；
    # 它们本来就不满足风险身份判定，按非风险补上而不是留空。
    unresolved = int(bind.execute(
        sa.text("SELECT COUNT(*) FROM rule_result WHERE rule_role IS NULL")
    ).scalar() or 0)
    if unresolved:
        print(f"[0004] {unresolved} 条结果的规则编号不在任务清单中，按非风险（CHECK）回填。")
        bind.execute(sa.text("UPDATE rule_result SET rule_role = 'CHECK' WHERE rule_role IS NULL"))
    op.alter_column("rule_result", "rule_role", existing_type=sa.String(20), nullable=False)


def downgrade() -> None:
    op.drop_index("idx_rule_result_role_task", table_name="rule_result")
    op.drop_column("rule_result", "rule_role")
