"""方案 JSON 配置读取；不把配置条目当作数据库实体。"""

from sqlalchemy.orm import Session

from app.modules.plan.config.model import PlanField, PlanMaterial, PlanRule, PlanSection
from app.services.shared import configuration as config_store
from app.services.shared.configuration import ConfigItem


class PlanConfigCRUD:
    def __init__(self, db: Session) -> None:
        self.db = db

    def plan_config_rows(self, plan_id: int
    ) -> tuple[list[ConfigItem], list[ConfigItem], list[ConfigItem], list[ConfigItem]]:
        """方案自己的配置行，按用户可见的顺序。"""
        return (
            config_store.rows(self.db, PlanMaterial, plan_id),
            config_store.rows(self.db, PlanField, plan_id),
            config_store.rows(self.db, PlanRule, plan_id),
            config_store.rows(self.db, PlanSection, plan_id),
        )
