"""启动时只读检查必需业务表；实际列与约束错误在访问时明确暴露。"""

import logging

from sqlalchemy import inspect
from sqlalchemy.engine import Engine

from ..database import engine as default_engine

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
    "evidence",
    "manual_review",
)


def verify_schema(engine: Engine | None = None) -> dict[str, list[str]]:
    """不执行 DDL，不维护第二套列、索引、外键和触发器清单。"""
    present = set(inspect(engine or default_engine).get_table_names())
    missing = [name for name in REQUIRED_TABLES if name not in present]
    if missing:
        raise RuntimeError(
            "数据库缺少必需业务表：" + "、".join(missing)
            + "。应用不会自动修表；请执行 backend/database/schema.sql 重建空库。"
        )
    logger.info("必需业务表齐全")
    return {"missing_tables": []}
