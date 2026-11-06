"""启动时只读检查必需业务表与列；不执行 DDL，也不维护第二套列清单。

列清单直接取自 SQLAlchemy 实体元数据，因此“实体改了、库没重建”会在启动时
明确报出缺列，而不是等到第一次访问才报 Unknown column。唯一键、生成列表达式
仍以 backend/resource/schema.sql 为准，启动校验不比较它们。
"""

import logging

from sqlalchemy import inspect
from sqlalchemy.engine import Engine

from app.core.database import Base
from app.core.database import engine as default_engine
from app.modules.audit.artifact import model as artifacts  # noqa: F401
from app.modules.audit.execution import model as executions  # noqa: F401
from app.modules.audit.field import model as fields  # noqa: F401
from app.modules.audit.report import model as reports  # noqa: F401
from app.modules.audit.report_template import model as report_templates  # noqa: F401
from app.modules.audit.rule import model as rules  # noqa: F401
from app.modules.audit.task import model as tasks  # noqa: F401
from app.modules.imports.generation import model as generations  # noqa: F401
from app.modules.imports.job import model as imports  # noqa: F401
from app.modules.material.document import model as documents  # noqa: F401
from app.modules.plan.config import model as plan_config  # noqa: F401
from app.modules.plan.plan import model as plans  # noqa: F401
from app.modules.scenario.draft import model as scenario_drafts  # noqa: F401
from app.modules.scenario.template import model as scenarios  # noqa: F401
from app.modules.system.operation import model as operations  # noqa: F401

logger = logging.getLogger(__name__)


REQUIRED_TABLES: tuple[str, ...] = (
    "scenario_template",
    "background_operation",
    "import_job",
    # AI 辅助生成规则库：作业、冻结导出与导入关联。
    "rulebook_generation_job",
    "rulebook_generation_export",
    "rulebook_generation_import",
    "rulebook_generation_revision",
    "rulebook_generation_message",
    "scenario_version",
    "scenario_node",
    "scenario_draft",
    # 执行容器通过版本与任务关联真实执行记录。
    "audit_plan",
    # 方案与草稿均将业务配置保存在 config_json 中。
    "audit_task",
    "audit_report",
    "report_template",
    "operation_log",
    "task_document",
    "document_upload",
    "stored_artifact",
    "node_execution",
    # 字段事实的版本与当前位索引；取值在任务 CSV，不在表里。
    "task_field_value",
    # 统一事实的身份与口径索引（期间 / 报表口径 / 主体范围）；取值同样在任务 CSV。
    "task_fact",
    "rule_result",
    "manual_review",
    # 字段核对确认：绑定执行批次、输入修订与字段版本摘要。
    "field_review_confirmation",
)


def verify_schema(engine: Engine | None = None) -> dict[str, list[str]]:
    """按实体元数据核对必需业务表及列；结构不一致时明确拒绝启动。"""
    inspector = inspect(engine or default_engine)
    present = set(inspector.get_table_names())
    missing = [name for name in REQUIRED_TABLES if name not in present]
    if missing:
        raise RuntimeError(
            "数据库缺少必需业务表：" + "、".join(missing)
            + "。应用不会自动建表；新库请执行 backend/resource/schema.sql 后 alembic stamp head，已有库按需 upgrade head。"
        )
    problems: list[str] = []
    missing_all: list[str] = []
    unexpected_all: list[str] = []
    for name in REQUIRED_TABLES:
        table = Base.metadata.tables.get(name)
        if table is None:
            continue
        actual_columns = inspector.get_columns(name)
        actual = {column["name"] for column in actual_columns}
        expected = {column.name for column in table.columns}
        missing_columns = sorted(column for column in expected if column not in actual)
        if missing_columns:
            problems.append(f"{name} 缺少列（{', '.join(missing_columns)}）")
            missing_all.extend(f"{name}.{column}" for column in missing_columns)
        # 迁移前启动时，代码已不再写入的 NOT NULL 无默认列会让插入直接失败；
        # 启动校验必须把它当作结构不一致，而不是只看“实体列是否都在”。
        unexpected_required = sorted(
            column["name"] for column in actual_columns
            if column["name"] not in expected
            and not column["nullable"]
            and column.get("default") is None
            and not column.get("computed")
            and not column.get("autoincrement")
        )
        if unexpected_required:
            problems.append(f"{name} 存在代码未写入的必填列（{', '.join(unexpected_required)}）")
            unexpected_all.extend(f"{name}.{column}" for column in unexpected_required)
    if problems:
        raise RuntimeError(
            "数据库结构与当前代码不一致：" + "；".join(problems)
            + "。应用不会自动迁移；已有库请执行 alembic upgrade head，新库用当前 schema.sql 建库。"
        )
    logger.info("必需业务表与列齐全")
    return {"missing_tables": [], "missing_columns": missing_all, "unexpected_required_columns": unexpected_all}
