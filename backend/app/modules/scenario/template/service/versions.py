"""场景身份与版本号分配。

模板身份是 scenario_code（稳定标识），不是中文名称；中文名称只是显示与检索用的属性。
版本号由 scenario_template.latest_version_no 在同一事务内原子递增分配，
不依赖无保护的 max(version_no)+1；scenario_version 上的唯一约束仍是最终兜底。"""

from sqlalchemy.orm import Session

from app.common.exceptions import BusinessError
from app.modules.scenario.template.model import ScenarioTemplate
from app.modules.scenario.template.service.crud import ScenarioTemplateCRUD


class ScenarioTemplateVersionsService:
    """本实体的服务方法；db 由构造时注入。"""

    def __init__(self, db: Session | None = None) -> None:
        self.db = db

    def _lock_template(self, scenario_code: str) -> ScenarioTemplate | None:
        return ScenarioTemplateCRUD(self.db).template_by_code(scenario_code, lock=True)


    def allocate_version(self, scenario_code: str, scenario_name: str) -> int:
        """为新发布分配版本号。调用方必须在同一事务内完成发布提交。"""
        template = self._lock_template( scenario_code)
        if template is None:
            raise BusinessError(
                "SCENARIO_NOT_FOUND", "审核场景不存在或已被删除，不能发布版本", status_code=404,
                details={"scenario_code": scenario_code},
            )
        template.latest_version_no = template.latest_version_no + 1
        template.scenario_name = scenario_name
        ScenarioTemplateCRUD(self.db).flush()
        return template.latest_version_no
