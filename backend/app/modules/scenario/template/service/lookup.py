"""场景身份与发布版本的存在性查询：SQL 在 crud。"""

from sqlalchemy.orm import Session

from app.common.exceptions import BusinessError
from app.modules.scenario.template.model import ScenarioTemplate, ScenarioVersion
from app.modules.scenario.template.service.crud import ScenarioTemplateCRUD


class ScenarioTemplateLookupService:
    """本实体的服务方法；db 由构造时注入。"""

    def __init__(self, db: Session | None = None) -> None:
        self.db = db

    def template_or_404(self, scenario_code: str, *, lock: bool = False) -> ScenarioTemplate:
        """按场景编码取场景；取不到即 404，写路径可先锁行。"""
        template = ScenarioTemplateCRUD(self.db).template_by_code(scenario_code, lock=lock)
        if not template:
            raise BusinessError(
                "SCENARIO_NOT_FOUND",
                "审核场景不存在或已被删除",
                status_code=404,
                details={"scenario_code": scenario_code},
            )
        return template

    def version_or_404(self, scenario_code: str, version_no: int) -> ScenarioVersion:
        """按场景编码与版本号取已发布版本；取不到即 404。"""
        version = self.find_published_version(scenario_code, version_no)
        if not version:
            raise BusinessError(
                "SCENARIO_VERSION_NOT_FOUND",
                "场景版本不存在",
                status_code=404,
                details={"scenario_code": scenario_code, "version_no": version_no},
            )
        return version

    def find_published_version(self, scenario_code: str, version_no: int) -> ScenarioVersion | None:
        return ScenarioTemplateCRUD(self.db).version_by_no(scenario_code, version_no)

    def find_latest_published_version(self, scenario_code: str) -> ScenarioVersion | None:
        return ScenarioTemplateCRUD(self.db).latest_version(scenario_code)

    def scenario_or_404(self, scenario_code: str, version_no: int) -> ScenarioVersion:
        """按场景编码与版本号取已发布版本；不存在时 404。"""
        scenario = self.find_published_version(scenario_code, version_no)
        if not scenario:
            raise BusinessError("SCENARIO_VERSION_NOT_FOUND", "已发布场景版本不存在", status_code=404)
        return scenario
