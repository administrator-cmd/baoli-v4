"""LangGraph 持久化 checkpoint 运行时。

用社区实现 langgraph-checkpoint-mysql 的 PyMySQLSaver，把 checkpoint 保存在同一个
MySQL 业务库 baoli_v4 中，不额外引入数据库。

每次图调用/状态读取都新建一个 saver 连接、用完立即关闭：等待用户操作期间既不持有业务
Session，也不长期占用请求连接；下次调用重新建连即可读取同一个 thread 的 checkpoint。

本模块不调用 saver.setup()，也不维护迁移记录表。
建表由人工从空库执行 backend/database/schema.sql，结构不匹配时明确失败。
"""

from contextlib import contextmanager
from typing import Any, Iterator

import pymysql
from pymysql.cursors import DictCursor
from sqlalchemy.exc import DBAPIError, DisconnectionError, InterfaceError, OperationalError

from ..config import get_settings, validated_database_url
from ..common.exceptions import BusinessError


def is_storage_unavailable(exc: Exception) -> bool:
    """识别连接及数据库运行故障，不按异常模块把所有数据库错误都当成不可达。"""
    return isinstance(
        exc,
        (pymysql.OperationalError, pymysql.InterfaceError, OperationalError, InterfaceError, DisconnectionError),
    ) or (isinstance(exc, DBAPIError) and exc.connection_invalidated)


def _connection_kwargs() -> dict[str, object]:
    """与 backend/app/database.py 相同的目标校验：驱动与库名都不能被改到别处。"""
    settings = get_settings()
    if not settings.database_url:
        raise BusinessError(
            "CHECKPOINT_DSN_MISSING",
            "数据库未配置，无法连接 checkpoint 存储",
            status_code=500,
        )
    try:
        url = validated_database_url(settings.database_url)
    except ValueError as exc:
        raise BusinessError(
            "CHECKPOINT_DSN_INVALID",
            "checkpoint 存储的数据库目标无效",
            status_code=500,
            details={"reason": str(exc)},
        ) from exc
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
)

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

    这是临时状态：连接或查询异常只说明这一次读不到存储，不能证明 checkpoint 真的缺失，
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


def check_checkpoint_tables() -> None:
    """只读核对 checkpoint 表是否存在；列和索引由 schema_check 统一核对。"""
    connection = _open_checkpoint_connection()
    try:
        database = _database_name(connection)
        existing = _existing_checkpoint_tables(connection)
        missing = [name for name in REQUIRED_CHECKPOINT_TABLES if name not in existing]
        if missing:
            raise BusinessError(
                "CHECKPOINT_TABLES_MISSING",
                f"数据库 {database} 缺少持久化 checkpoint 数据表：{'、'.join(missing)}。"
                "请清空开发库后执行 backend/database/schema.sql 重新建表。",
                status_code=500,
                details={
                    "database": database,
                    "missing_tables": missing,
                    "existing_table_count": len(existing),
                    "script": "backend/database/schema.sql",
                },
            )
    finally:
        connection.close()
