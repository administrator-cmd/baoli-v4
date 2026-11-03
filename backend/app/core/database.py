from collections.abc import Generator

import pymysql
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError, DisconnectionError, InterfaceError, OperationalError
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.core.config import get_settings, validated_database_url


class Base(DeclarativeBase):
    pass


settings = get_settings()


def _validated_database_url():
    try:
        return validated_database_url(settings.database_url)
    except ValueError as exc:
        raise RuntimeError(f"数据库连接配置无效：{exc}") from exc


engine = create_engine(
    _validated_database_url(),
    pool_pre_ping=True,
    future=True,
    # 会话时区固定 UTC：DATETIME 列与 CURRENT_TIMESTAMP 默认值都按 UTC 存储，
    # 与代码里一律使用 datetime.now(UTC) 的写入口径一致，不受本机/服务器时区影响。
    connect_args={"init_command": "SET time_zone = '+00:00'"},
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def is_storage_unavailable(exc: Exception) -> bool:
    """识别连接及数据库运行故障，不按异常模块把所有数据库错误都当成不可达。

    调用方据此区分「稍后重试可能成功」和「这次执行本身有缺陷」，两者的处理方式不同。
    """
    return isinstance(
        exc,
        (pymysql.OperationalError, pymysql.InterfaceError, OperationalError, InterfaceError, DisconnectionError),
    ) or (isinstance(exc, DBAPIError) and exc.connection_invalidated)


def get_db() -> Generator[Session, None, None]:
    session = SessionLocal()
    try:
        yield session
    except Exception:
        # 未提交的变更必须显式回滚，例如发布场景中途失败时不能残留半成品版本。
        session.rollback()
        raise
    finally:
        session.close()


def check_database_connection() -> None:
    with engine.connect() as connection:
        connection.execute(text("SELECT 1"))
