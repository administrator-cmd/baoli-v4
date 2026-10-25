"""启动时只读检查必需业务表与列；不执行 DDL，也不维护第二套列清单。

列清单直接取自 SQLAlchemy 实体元数据，因此“实体改了、库没重建”会在启动时
明确报出缺列，而不是等到第一次访问才报 Unknown column。唯一键、生成列表达式
仍以 backend/resource/schema.sql 为准，启动校验不比较它们。
"""

import logging

from sqlalchemy import inspect
from sqlalchemy.engine import Engine

from ..core.database import Base
from ..core.database import engine as default_engine
from ..entity import (  # noqa: F401 - 导入实体模块以填充 Base.metadata
    imports,
    operations,
    plans,
    reports,
    scenario_drafts,
    scenarios,
    tasks,
)

logger = logging.getLogger(__name__)


REQUIRED_TABLES: tuple[str, ...] = (
    "scenario_template",
    "background_operation",
    "import_job",
    "scenario_version",
    "scenario_node",
    "scenario_draft",
    # 执行容器通过版本与任务关联真实执行记录。
    "audit_plan",
    # 方案与草稿均将业务配置保存在 config_json 中。
    "audit_task",
    "audit_report",
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
)


def verify_schema(engine: Engine | None = None) -> dict[str, list[str]]:
    """按实体元数据核对必需业务表及列；结构不一致时明确拒绝启动。"""
    inspector = inspect(engine or default_engine)
    present = set(inspector.get_table_names())
    missing = [name for name in REQUIRED_TABLES if name not in present]
    if missing:
        raise RuntimeError(
            "数据库缺少必需业务表：" + "、".join(missing)
            + "。应用不会自动修表；请执行 backend/resource/schema.sql 重建空库。"
        )
    problems: list[str] = []
    missing_all: list[str] = []
    for name in REQUIRED_TABLES:
        table = Base.metadata.tables.get(name)
        if table is None:
            continue
        actual = {column["name"] for column in inspector.get_columns(name)}
        missing_columns = sorted(column.name for column in table.columns if column.name not in actual)
        if missing_columns:
            problems.append(f"{name} 缺少列（{', '.join(missing_columns)}）")
            missing_all.extend(f"{name}.{column}" for column in missing_columns)
    if problems:
        raise RuntimeError(
            "数据库结构与当前代码不一致：" + "；".join(problems)
            + "。应用不会自动迁移；请执行 backend/resource/schema.sql 重建空库。"
        )
    logger.info("必需业务表与列齐全")
    return {"missing_tables": [], "missing_columns": missing_all}
