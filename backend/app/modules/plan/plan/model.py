"""方案与方案配置的模型定义。"""

from datetime import datetime
from typing import Any

from sqlalchemy import (
    CHAR,
    JSON,
    BigInteger,
    Boolean,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.base_model import TimestampMixin, UTCDateTime
from app.core.database import Base
from app.domain.configuration import config_property, empty_config


class AuditPlan(Base, TimestampMixin):
    """审核方案：已发布场景版本的一份可编辑副本。

    创建方案时把那一版配置整份复制到 `config_json`，之后由用户增删改。节点与 Sheet 是场景级执行结构，只读地
    存在 `context_json` 里随方案一起冻结，用户在方案页只能看不能改。

    保存方案 = 用自己的配置重新编译并写 `manifest_json`，再按编译产物算任务范围指纹
    写 `scope_hash`；算不出来时保存照常成功，原因记在 `sync_error`，页面显示「待核对」。

    已建任务不受方案后续改动影响：任务创建时把方案的编译产物整份抓进
    `audit_task.manifest_json`，执行期只读它，不再读场景版本。
    """

    __tablename__ = "audit_plan"
    __table_args__ = (
        UniqueConstraint("plan_code"),
        Index("idx_audit_plan_scenario", "scenario_code", "deleted_at"),
        Index("idx_audit_plan_deleted", "deleted_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    plan_code: Mapped[str] = mapped_column(String(80), nullable=False)
    plan_name: Mapped[str] = mapped_column(String(255), nullable=False)
    # 历史快照：场景删除后方案仍保留，因此不建指向 scenario_template 的外键。
    scenario_code: Mapped[str] = mapped_column(String(80), nullable=False)
    # 这份方案从哪一版场景复制而来。只是溯源：执行与范围都不再读它，
    # 场景后来发布新版本也不会改变本方案（要换基准请另建方案）。
    scenario_version_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("scenario_version.id", name="fk_audit_plan_scenario_version", ondelete="RESTRICT", onupdate="RESTRICT"))
    description: Mapped[str | None] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=text("1"))
    deleted_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    # 场景级、方案里只读的那部分上下文：节点实例、选用 Sheet 与协议版本。
    # 它们不参与方案编辑，但编译（compile_form_manifest）必须有，因此随方案冻结一份。
    context_json = config_property("context")
    # 方案自己的动态材料、字段、规则与报告配置，不再写 plan_* 明细表。
    config_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=empty_config)
    # 方案的编译产物：报告渲染、任务冻结都读它。未编译成功时为空，原因见 sync_error。
    manifest_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    # 最近一次保存时范围未能生效的原因（编译未通过：没选规则、字段引用缺失等）。
    # NULL 表示当前范围已生效。这是可展示状态，不是静默降级。
    sync_error: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    # 范围指纹：内容没变时不必重新核对；任务创建时用它校验页面预览的范围。
    scope_hash: Mapped[str | None] = mapped_column(CHAR(64))
