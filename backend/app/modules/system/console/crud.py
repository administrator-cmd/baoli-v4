"""系统渠道状态探测的 SQL 层。"""

from sqlalchemy import text

from app.core.base_crud import SessionCRUD


class SystemConsoleCRUD(SessionCRUD):
    """状态页只执行只读探针，不承载业务查询。"""

    def ping(self) -> None:
        """执行一次最小查询；连接失败由调用方按 SQLAlchemyError 处理。"""
        self.db.execute(text("SELECT 1"))
