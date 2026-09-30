from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from ..database import engine


def database_status() -> str:
    """执行一次真实连接探测，不修改任何数据库状态。"""
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        return "CONNECTED"
    except SQLAlchemyError:
        return "FAILED"

