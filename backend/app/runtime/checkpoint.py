"""LangGraph 持久化 checkpoint 运行时。

使用社区实现 langgraph-checkpoint-mysql 的 PyMySQLSaver，把 checkpoint 保存在同一个
MySQL 业务库 baoli_v4 中，不额外引入数据库。

连接生命周期：每次图调用/状态读取都新建一个 saver 连接，用完立即关闭。
等待用户操作期间既不持有业务 Session，也不长期占用请求连接；下次调用重新建连即可
读取同一个 thread 的 checkpoint。

注意：本模块不会调用 saver.setup()。建表由用户手工执行
backend/database/schema.sql。表不存在时明确失败。
"""

from contextlib import contextmanager
from typing import Any, Iterator

import pymysql
from pymysql.cursors import DictCursor

from ..config import get_settings
from ..errors import BusinessError


def _connection_kwargs() -> dict[str, object]:
    """与 backend/app/database.py 相同的目标校验：驱动与库名都不能被改到别处。"""
    settings = get_settings()
    if not settings.database_url:
        raise BusinessError(
            "CHECKPOINT_DSN_MISSING",
            "数据库未配置，无法连接 checkpoint 存储",
            status_code=500,
        )
    from sqlalchemy.engine import make_url

    try:
        url = make_url(settings.database_url)
    except Exception as exc:  # noqa: BLE001 - 配置错误必须显式暴露
        raise BusinessError("CHECKPOINT_DSN_INVALID", "数据库连接配置无效", status_code=500) from exc
    if url.drivername != "mysql+pymysql" or url.database != "baoli_v4":
        raise BusinessError(
            "CHECKPOINT_DSN_INVALID",
            "checkpoint 存储只允许使用 mysql+pymysql 的 baoli_v4 数据库",
            status_code=500,
            details={"drivername": url.drivername, "database": url.database},
        )
    url = url.set(database="baoli_v4")
    return {
        "host": url.host or "127.0.0.1",
        "port": url.port or 3306,
        "user": url.username,
        "password": url.password or "",
        "database": url.database,
        "charset": "utf8mb4",
        "cursorclass": DictCursor,
        "autocommit": True,
    }


@contextmanager
def mysql_saver() -> Iterator[object]:
    """创建一个只在本调用内有效的 PyMySQLSaver。"""
    try:
        from langgraph.checkpoint.mysql.pymysql import PyMySQLSaver
    except ImportError as exc:
        raise BusinessError(
            "CHECKPOINT_SAVER_MISSING",
            "未安装持久化 checkpoint 依赖 langgraph-checkpoint-mysql，审核流程无法恢复",
            status_code=500,
        ) from exc
    connection = _open_checkpoint_connection()
    try:
        yield PyMySQLSaver(connection)
    finally:
        connection.close()


REQUIRED_CHECKPOINT_TABLES = (
    "checkpoints",
    "checkpoint_blobs",
    "checkpoint_writes",
    "checkpoint_migrations",
)

# 依赖包 langgraph-checkpoint-mysql 3.0.0 的 MIGRATIONS 最大版本。
# 来源：langgraph/checkpoint/mysql/base.py 的 MIGRATIONS 列表共 22 条（索引 0..21，
# 第 0 条创建 checkpoint_migrations 表），最后一条的版本号即 21。
# 核对方式（依赖升级后这个数字会变，必须重新核对）：
#   python -c "from langgraph.checkpoint.mysql.base import MIGRATIONS; print(len(MIGRATIONS) - 1)"
# 输出的数字与常量不一致时说明依赖已升级：先把数据库结构按该包迁移脚本升到最新，
# 再同步更新本常量与 backend/database/schema.sql 中写入的版本行。
EXPECTED_CHECKPOINT_MIGRATION_VERSION = 21

# classify_checkpoint_storage() 在“存储可达且结构存在”时返回的状态值。
CHECKPOINT_STORAGE_READY = "READY"


def _open_checkpoint_connection() -> "pymysql.connections.Connection":
    """打开 checkpoint 存储连接；连接失败统一抛 CHECKPOINT_CONNECT_FAILED（临时不可达，可重试）。"""
    try:
        return pymysql.connect(**_connection_kwargs())
    except pymysql.MySQLError as exc:
        raise _checkpoint_unreachable() from exc


def _single_value(row: Any) -> Any:
    """pymysql 的 DictCursor 返回 dict，普通游标返回 tuple。"""
    if row is None:
        return None
    return list(row.values())[0] if isinstance(row, dict) else row[0]


def _checkpoint_unreachable() -> BusinessError:
    """存储暂不可达的显式错误。

    这是**临时**状态：连接或查询异常只说明这一次读不到存储，不能证明 checkpoint 真的缺失，
    调用方不得据此把任务标记为永久失败或丢弃等待中的流程，应提示稍后重试。
    """
    return BusinessError(
        "CHECKPOINT_CONNECT_FAILED",
        "checkpoint 存储暂时不可达（连接或查询失败），请稍后重试；不要据此把任务判为永久失败",
        status_code=500,
    )


def _database_name(connection: "pymysql.connections.Connection") -> Any:
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT DATABASE()")
            return _single_value(cursor.fetchone())
    except pymysql.MySQLError as exc:
        raise _checkpoint_unreachable() from exc


def _existing_checkpoint_tables(connection: "pymysql.connections.Connection") -> set[str]:
    """只读查询库里已存在的表名（小写）。查询失败视为存储暂不可达。"""
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = DATABASE()"
            )
            return {str(_single_value(row)).lower() for row in cursor.fetchall()}
    except pymysql.MySQLError as exc:
        raise _checkpoint_unreachable() from exc


def _applied_checkpoint_migration_version(connection: "pymysql.connections.Connection") -> int | None:
    """已应用的最大迁移版本；表为空时返回 None。"""
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT MAX(v) FROM checkpoint_migrations")
            value = _single_value(cursor.fetchone())
    except pymysql.MySQLError as exc:
        raise _checkpoint_unreachable() from exc
    if value is None:
        return None
    return int(value)


def classify_checkpoint_storage() -> str:
    """读取探测：区分 checkpoint 存储的三种状态，供审核图与审核命令层判断后续动作。

    三种结果：
      1. 存储暂不可达（连接或查询异常）→ 抛 BusinessError(CHECKPOINT_CONNECT_FAILED)。
         **这是临时状态，调用方不得据此把任务标记为永久失败**，应提示稍后重试。
      2. 存储可达但 checkpoint 表缺失 → 抛 BusinessError(CHECKPOINT_TABLES_MISSING)，属建表/配置问题。
      3. 存储可达且结构存在 → 返回 CHECKPOINT_STORAGE_READY。

    本函数只读：不执行任何 DDL，不改动 saver 配置（autocommit 契约保持 _connection_kwargs 不变）。
    """
    connection = _open_checkpoint_connection()
    try:
        existing = _existing_checkpoint_tables(connection)
    finally:
        connection.close()
    missing = [name for name in REQUIRED_CHECKPOINT_TABLES if name not in existing]
    if missing:
        raise BusinessError(
            "CHECKPOINT_TABLES_MISSING",
            f"checkpoint 存储缺少数据表：{'、'.join(missing)}。请执行 backend/database/schema.sql。",
            status_code=500,
            details={"missing_tables": missing, "script": "backend/database/schema.sql"},
        )
    return CHECKPOINT_STORAGE_READY


def check_checkpoint_tables() -> None:
    """启动时校验 checkpoint 表结构与迁移版本。只读检查，绝不建表、不执行任何 DDL。

    必须说清「连的是哪个库、库里有多少张表、缺哪几张、迁移到哪个版本」：
    否则无法区分「没执行建表脚本」「脚本执行到了另一个库」和「依赖升级但库结构没跟上」。
    """
    connection = _open_checkpoint_connection()
    try:
        database = _database_name(connection)
        existing = _existing_checkpoint_tables(connection)
        missing = [name for name in REQUIRED_CHECKPOINT_TABLES if name not in existing]
        if missing:
            raise BusinessError(
                "CHECKPOINT_TABLES_MISSING",
                f"数据库 {database} 缺少持久化 checkpoint 数据表：{'、'.join(missing)}。"
                "请执行 backend/database/schema.sql（可重复执行，只补缺表，不会改动已有数据）。",
                status_code=500,
                details={
                    "database": database,
                    "missing_tables": missing,
                    "existing_table_count": len(existing),
                    "script": "backend/database/schema.sql",
                },
            )
        applied = _applied_checkpoint_migration_version(connection)
    finally:
        connection.close()
    if applied is None or applied < EXPECTED_CHECKPOINT_MIGRATION_VERSION:
        raise BusinessError(
            "CHECKPOINT_MIGRATIONS_OUTDATED",
            f"数据库 {database} 的 checkpoint 迁移版本不足：已应用 "
            f"{applied if applied is not None else '无'}，期望至少 {EXPECTED_CHECKPOINT_MIGRATION_VERSION}。"
            "说明依赖包结构已升级但库结构没跟上，请执行 backend/database/schema.sql 补齐（含版本记录）。",
            status_code=500,
            details={
                "database": database,
                "applied_version": applied,
                "expected_version": EXPECTED_CHECKPOINT_MIGRATION_VERSION,
                "script": "backend/database/schema.sql",
            },
        )
