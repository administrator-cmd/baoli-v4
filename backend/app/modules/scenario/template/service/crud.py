"""场景查询的 SQL 层：只取实体与行，不组装视图。"""

from datetime import datetime

from sqlalchemy import and_, func, select

from app.core.base_crud import CRUDBase
from app.modules.plan.plan.model import AuditPlan
from app.modules.scenario.draft.model import ScenarioDraft
from app.modules.scenario.template.model import ScenarioTemplate, ScenarioVersion


class ScenarioTemplateCRUD(CRUDBase):
    model = ScenarioTemplate

    def templates_page(self, *,
        name: str | None,
        category: str | None,
        enabled: bool | None,
        published_from: datetime | None,
        published_to: datetime | None,
        page: int,
        page_size: int,
    ) -> tuple[list[ScenarioTemplate], int]:
        """按筛选条件分页取场景实体，total 与列表同一条件。"""
        conditions = []
        if name:
            conditions.append(ScenarioTemplate.scenario_name.like(f"%{name.strip()}%"))
        if category:
            conditions.append(ScenarioTemplate.category == category)
        if enabled is not None:
            conditions.append(ScenarioTemplate.enabled.is_(enabled))
        if published_from or published_to:
            version_conditions = [ScenarioVersion.scenario_code == ScenarioTemplate.scenario_code]
            if published_from:
                version_conditions.append(ScenarioVersion.published_at >= published_from)
            if published_to:
                version_conditions.append(ScenarioVersion.published_at <= published_to)
            conditions.append(select(ScenarioVersion.id).where(*version_conditions).exists())

        total = self.db.scalar(select(func.count()).select_from(ScenarioTemplate).where(*conditions)) or 0
        templates = self.db.scalars(
            select(ScenarioTemplate)
            .where(*conditions)
            .order_by(ScenarioTemplate.updated_at.desc(), ScenarioTemplate.id.desc())
            .limit(page_size)
            .offset((page - 1) * page_size)
        ).all()
        return list(templates), int(total)


    def latest_version_rows(self, scenario_codes: list[str]
    ) -> list[tuple[str, int, datetime | None, int]]:
        """每个场景的最新发布版本：scenario_code、version_no、published_at、id。"""
        if not scenario_codes:
            return []
        latest_version = (
            select(
                ScenarioVersion.scenario_code.label("scenario_code"),
                func.max(ScenarioVersion.version_no).label("version_no"),
            )
            .where(ScenarioVersion.scenario_code.in_(scenario_codes))
            .group_by(ScenarioVersion.scenario_code)
            .subquery()
        )
        return list(
            self.db.execute(
                select(
                    ScenarioVersion.scenario_code,
                    ScenarioVersion.version_no,
                    ScenarioVersion.published_at,
                    ScenarioVersion.id,
                ).join(latest_version, and_(
                    ScenarioVersion.scenario_code == latest_version.c.scenario_code,
                    ScenarioVersion.version_no == latest_version.c.version_no,
                ))
            ).all()
        )


    def drafts_for_codes(self, scenario_codes: list[str]) -> list[ScenarioDraft]:
        """批量取场景草稿。"""
        if not scenario_codes:
            return []
        return list(self.db.scalars(select(ScenarioDraft).where(ScenarioDraft.scenario_code.in_(scenario_codes))).all())


    def draft_for_code(self, scenario_code: str) -> ScenarioDraft | None:
        return self.db.scalar(select(ScenarioDraft).where(ScenarioDraft.scenario_code == scenario_code))


    def manifests_for_ids(self, version_ids: list[int]) -> dict[int, dict]:
        """按版本 id 取发布 manifest，避免整行载入。"""
        if not version_ids:
            return {}
        return dict(self.db.execute(
            select(ScenarioVersion.id, ScenarioVersion.manifest_json).where(ScenarioVersion.id.in_(version_ids))
        ).all())


    def all_templates(self) -> list[ScenarioTemplate]:
        return list(self.db.scalars(select(ScenarioTemplate).order_by(ScenarioTemplate.scenario_name)).all())


    def latest_version_numbers(self) -> dict[str, int]:
        """每个场景的最大版本号；没有版本的场景不在结果里。"""
        return {
            scenario_code: int(version_no)
            for scenario_code, version_no in self.db.execute(
                select(ScenarioVersion.scenario_code, func.max(ScenarioVersion.version_no))
                .group_by(ScenarioVersion.scenario_code)
            ).all()
        }


    def count_plans(self, scenario_code: str) -> int:
        return int(self.db.scalar(
            select(func.count()).select_from(AuditPlan).where(
                AuditPlan.scenario_code == scenario_code, AuditPlan.deleted_at.is_(None)
            )
        ) or 0)

    def template_by_code(self, scenario_code: str, *, lock: bool = False) -> ScenarioTemplate | None:
        statement = select(ScenarioTemplate).where(ScenarioTemplate.scenario_code == scenario_code)
        if lock:
            statement = statement.with_for_update()
        return self.db.scalar(statement)

    def version_by_no(self, scenario_code: str, version_no: int) -> ScenarioVersion | None:
        return self.db.scalar(
            select(ScenarioVersion).where(
                ScenarioVersion.scenario_code == scenario_code,
                ScenarioVersion.version_no == version_no,
            )
        )

    def version_id_by_import_job(self, import_job_id: int) -> int | None:
        return self.db.scalar(
            select(ScenarioVersion.id).where(ScenarioVersion.import_job_id == import_job_id)
        )

    def version_summary(self, version_id: int):
        return self.db.execute(
            select(
                ScenarioVersion.version_no,
                ScenarioVersion.published_at,
                ScenarioVersion.scenario_name,
            ).where(ScenarioVersion.id == version_id)
        ).first()

    def versions_by_import_jobs(self, job_ids: list[int], *, lock: bool = False) -> list[ScenarioVersion]:
        statement = select(ScenarioVersion).where(ScenarioVersion.import_job_id.in_(job_ids))
        if lock:
            statement = statement.with_for_update()
        return list(self.db.scalars(statement).all())

    def latest_version(self, scenario_code: str) -> ScenarioVersion | None:
        version_id = self.db.scalar(
            select(ScenarioVersion.id)
            .where(ScenarioVersion.scenario_code == scenario_code)
            .order_by(ScenarioVersion.version_no.desc())
            .limit(1)
        )
        return self.get_of(ScenarioVersion, version_id) if version_id is not None else None
