"""方案配置的 SQL 层。"""


from app.core.base_crud import CRUDBase
from app.modules.plan.config.model import PlanField, PlanMaterial, PlanRule, PlanSection
from app.services.shared import configuration as config_store


class PlanConfigCRUD(CRUDBase):
    model = PlanMaterial

    def plan_config_rows(self, plan_id: int
    ) -> tuple[list[PlanMaterial], list[PlanField], list[PlanRule], list[PlanSection]]:
        """方案自己的配置行，按用户可见的顺序。"""
        return (
            list(sorted(config_store.rows(self.db, PlanMaterial, plan_id), key=lambda item: (item.sort_no, item.id,))),
            list(sorted(config_store.rows(self.db, PlanField, plan_id), key=lambda item: (item.sort_no, item.id,))),
            list(sorted(config_store.rows(self.db, PlanRule, plan_id), key=lambda item: (item.sort_no, item.id,))),
            list(sorted(config_store.rows(self.db, PlanSection, plan_id), key=lambda item: (item.sort_no, item.id,))),
        )
