from collections.abc import Generator

from sqlalchemy import create_engine, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import get_settings, validated_database_url


class Base(DeclarativeBase):
    pass


settings = get_settings()


def _validated_database_url():
    try:
        return validated_database_url(settings.database_url)
    except ValueError as exc:
        raise RuntimeError(f"数据库连接配置无效：{exc}") from exc


engine = create_engine(_validated_database_url(), pool_pre_ping=True, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


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
