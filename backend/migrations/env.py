"""Alembic 迁移环境：连接与元数据复用应用自身配置。"""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

from app.core.config import get_settings, validated_database_url
from app.core.database import Base
from app.modules.audit.artifact import model as audit_artifacts  # noqa: F401
from app.modules.audit.execution import model as audit_executions  # noqa: F401
from app.modules.audit.field import model as audit_fields  # noqa: F401
from app.modules.audit.report import model as audit_reports  # noqa: F401
from app.modules.audit.report_template import model as report_templates  # noqa: F401
from app.modules.audit.rule import model as audit_rules  # noqa: F401
from app.modules.audit.task import model as audit_tasks  # noqa: F401
from app.modules.imports.generation import model as generations  # noqa: F401
from app.modules.imports.job import model as imports  # noqa: F401
from app.modules.material.document import model as documents  # noqa: F401
from app.modules.plan.config import model as plan_config  # noqa: F401
from app.modules.plan.plan import model as plans  # noqa: F401
from app.modules.scenario.draft import model as scenario_drafts  # noqa: F401
from app.modules.scenario.template import model as scenarios  # noqa: F401
from app.modules.system.operation import model as operations  # noqa: F401

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _database_url():
    """迁移只允许连接配置里声明的业务库，和应用使用同一道校验。"""
    try:
        return validated_database_url(get_settings().database_url)
    except ValueError as exc:
        raise RuntimeError(f"数据库连接配置无效：{exc}") from exc


def run_migrations_offline() -> None:
    """只生成 SQL，不连接数据库。"""
    context.configure(
        url=_database_url().render_as_string(hide_password=False),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """直接连接数据库执行迁移；使用独立连接，不复用应用的会话池。"""
    connectable = create_engine(_database_url(), poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
