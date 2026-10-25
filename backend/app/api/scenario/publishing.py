"""场景发布与任务范围预览接口。"""

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from ...core.database import get_db
from ...domain.manifest import build_task_schema
from ...schema.dto.scenarios import ScenarioPublishRequest
from ...schema.dto.tasks import TaskSchemaPreviewRequest
from ...schema.vo.scenarios import ScenarioPublishResponse
from ...schema.vo.tasks import TaskSchemaPreviewResponse
from ...services.scenario.lookup import scenario_or_404
from ...services.scenario.publishing import publish_draft

router = APIRouter(prefix="/scenarios", tags=["审核场景"])


@router.post("/{scenario_code}/draft/publish", response_model=ScenarioPublishResponse, status_code=201)
def publish_scenario_draft(
    scenario_code: str, payload: ScenarioPublishRequest, request: Request, db: Session = Depends(get_db)
) -> ScenarioPublishResponse:
    """发布场景：把当前草稿编译成一份不可变的已发布版本。

    这是管理员确认配置的动作，与「保存草稿」严格分开：保存只写草稿，
    只有本接口会产生业务用户可用的 `scenario_version`。

    发布时编译当前草稿，结构错误明确返回；发布快照与草稿删除同一次提交。

    修订号必须与草稿当前一致；内容与最新已发布版本完全一致时返回原版本，
    不制造重复版本。已发布版本不可修改——发布只新增版本。
    """
    version, reused = publish_draft(db, scenario_code, payload.model_dump())
    manifest = version.manifest_json
    return ScenarioPublishResponse(
        request_id=request.state.request_id,
        scenario_code=version.scenario_code,
        scenario_name=version.scenario_name,
        version_no=version.version_no,
        status=version.status,
        source=version.source,
        change_summary=version.change_summary,
        material_count=len(manifest.get("materials", [])),
        field_count=len(manifest.get("fields", [])),
        rule_count=len(manifest.get("rules", [])),
        published_at=version.published_at,
        reused=reused,
    )


@router.post("/{scenario_code}/versions/{version_no}/task-schema", response_model=TaskSchemaPreviewResponse)
def preview_task_schema(
    scenario_code: str,
    version_no: int,
    payload: TaskSchemaPreviewRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> TaskSchemaPreviewResponse:
    """在方案编辑/发布前展示实际执行范围（不写库）。

    范围完全等于用户勾选的规则——系统不补入任何规则，因此这里展示的
    「已启用 / 未启用」就是方案最终会跑与不会跑的全部。
    """
    scenario = scenario_or_404(db, scenario_code, version_no)
    manifest = scenario.manifest_json
    schema = build_task_schema(manifest, payload.enabled_rule_keys)
    scope = _task_scope_view(manifest, schema)
    return TaskSchemaPreviewResponse(
        request_id=request.state.request_id,
        enabled_rule_keys=schema["enabled_rule_keys"],
        unenabled_rule_keys=schema["unenabled_rule_keys"],
        scope=scope,
        field_count=len(schema["fields"]),
    )


def _task_scope_view(manifest: dict, schema: dict) -> dict:
    """任务实际执行范围：区分模板未配置 / 任务未启用 / 已启用。

    没有「自动补入」这一类：勾了什么就跑什么，范围完全等于这份方案的规则选择。
    """
    rule_names = {item["rule_key"]: item["name"] for item in manifest.get("rules", [])}
    return {
        "enabled_rules": [
            {"rule_key": key, "name": rule_names.get(key, key)} for key in schema["enabled_rule_keys"]
        ],
        "unenabled_rules": [
            {"rule_key": key, "name": rule_names.get(key, key)} for key in schema["unenabled_rule_keys"]
        ],
        "enabled_rule_keys": list(schema["enabled_rule_keys"]),
        "unenabled_rule_keys": list(schema["unenabled_rule_keys"]),
    }
