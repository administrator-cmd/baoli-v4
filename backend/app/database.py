from collections.abc import Generator

from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import get_settings


class Base(DeclarativeBase):
    pass


settings = get_settings()


def _validated_database_url() -> URL:
    if not settings.database_url:
        raise RuntimeError("数据库未配置：请在项目根目录 .env 设置 BAOLI_DATABASE_URL，并指向 baoli_v4")
    try:
        url = make_url(settings.database_url)
    except Exception as exc:
        raise RuntimeError("数据库连接配置无效：请检查 BAOLI_DATABASE_URL") from exc
    if url.drivername != "mysql+pymysql" or url.database != "baoli_v4":
        raise RuntimeError("数据库目标必须是 baoli_v4")
    # 只替换库名，保留 SQLAlchemy 已解析出的主机、账号与查询参数。
    return url.set(database="baoli_v4")


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
