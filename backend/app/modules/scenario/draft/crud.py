"""场景草稿与关联导入作业的 SQL 层。"""

from sqlalchemy import select

from app.core.base_crud import CRUDBase
from app.modules.imports.job.model import ImportJob
from app.modules.scenario.draft.model import ScenarioDraft


class ScenarioDraftCRUD(CRUDBase):
    model = ScenarioDraft

    def by_code(self, scenario_code: str, *, lock: bool = False) -> ScenarioDraft | None:
        statement = select(ScenarioDraft).where(ScenarioDraft.scenario_code == scenario_code)
        if lock:
            statement = statement.with_for_update()
        return self.db.scalar(statement)

    def import_jobs_by_scenario(self, scenario_code: str, *, lock: bool = False) -> list[ImportJob]:
        statement = select(ImportJob).where(ImportJob.scenario_code == scenario_code)
        if lock:
            statement = statement.with_for_update()
        return list(self.db.scalars(statement).all())
