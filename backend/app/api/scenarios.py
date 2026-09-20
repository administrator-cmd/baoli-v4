"""审核场景管理 API：场景列表、页面配置草稿、发布、版本与对比。

- 列表使用真实后端筛选、排序与分页；导出与当前筛选使用同一作用域。
- 草稿的每次写入都会递增修订号；发布必须携带页面看到的修订号。
- 已发布版本不可修改：修改走“基于此版创建修订”，不覆盖任何历史版本。
- 页面创建的场景没有 Excel 作业与工作表行号：发布时不伪造导入来源。
- 旧入口 POST /scenarios/{code}/versions/{n}/tasks 已收回：新任务必须从方案版本创建。
"""

from datetime import datetime
from io import BytesIO
from urllib.parse import quote

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from ..database import get_db
from ..domain.manifest import build_task_schema
from ..errors import BusinessError
from ..schemas import (
    CreateTaskRequest,
    CreateTaskResponse,
    DraftFieldRequest,
    DraftFieldUpdateRequest,
    DraftMaterialRequest,
    DraftMaterialUpdateRequest,
    DraftRuleEnabledRequest,
    DraftRuleRequest,
    DraftSectionRequest,
    DraftSectionUpdateRequest,
    PagedResponse,
    ScenarioCopyRequest,
    ScenarioCreateRequest,
    ScenarioDetailResponse,
    ScenarioDraftViewResponse,
    ScenarioEnabledRequest,
    ScenarioPublishRequest,
    ScenarioPublishResponse,
    ScenarioUpdateRequest,
    ScenarioVersionConfigResponse,
    ScenarioVersionResponse,
    TaskSchemaPreviewRequest,
    TaskSchemaPreviewResponse,
)
from ..services import scenario_admin_service
from ..services.scenario_admin_service import (
    copy_scenario,
    create_draft_field,
    create_draft_material,
    create_draft_rule,
    create_draft_section,
    create_scenario,
    delete_draft_field,
    delete_draft_material,
    delete_draft_rule,
    delete_draft_section,
    delete_scenario,
    draft_config_view,
    list_scenario_options,
    list_scenarios_page,
    publish_draft,
    restore_scenario,
    scenario_detail,
    set_draft_rule_enabled,
    set_scenario_enabled,
    template_or_404,
    update_draft_field,
    update_draft_material,
    update_draft_rule,
    update_draft_section,
    update_scenario,
)
from ..services.task_lookup import scenario_or_404
from ..services.task_service import create_task
from ..services.node_template_export_service import build_node_rulebook_export
from ..services.template_export_service import build_scenario_version_export
from .export_utils import EXPORT_PAGE_SIZE, csv_response

router = APIRouter(prefix="/scenarios", tags=["审核场景"])


def _page_params(page: int, page_size: int) -> tuple[int, int]:
    if page < 1:
        raise BusinessError("PAGE_INVALID", "页码必须是大于 0 的整数", status_code=400)
    if page_size < 1 or page_size > 200:
        raise BusinessError("PAGE_SIZE_INVALID", "每页条数必须在 1 到 200 之间", status_code=400)
    return page, page_size


def _export_manifest(manifest: dict) -> bytes:
    """按 manifest 自己的格式版本选择导出器。

    节点版（contract_review_v3）的列与 v2 完全不同，必须用它自己的七张业务表导出；
    旧版本继续按各自的列导出。拿另一种格式的列硬套会静默丢失配置，这里不做这种降级。
    """
    if manifest.get("profile_version") == "contract_review_v3":
        return build_node_rulebook_export(manifest)
    return build_scenario_version_export(manifest)


def _draft_response(request: Request, payload: dict) -> ScenarioDraftViewResponse:
    return ScenarioDraftViewResponse.model_validate({**payload, "request_id": request.state.request_id})


# ---------------------------------------------------------------------------
# 场景列表与基本资料
# ---------------------------------------------------------------------------


@router.get("", response_model=PagedResponse)
def list_scenarios(
    request: Request,
    name: str | None = None,
    category: str | None = None,
    enabled: bool | None = None,
    published_from: datetime | None = None,
    published_to: datetime | None = None,
    deleted: bool = False,
    page: int = Query(default=1),
    page_size: int = Query(default=20),
    db: Session = Depends(get_db),
) -> PagedResponse:
    page, page_size = _page_params(page, page_size)
    result = list_scenarios_page(
        db,
        name=name,
        category=category,
        enabled=enabled,
        published_from=published_from,
        published_to=published_to,
        deleted=deleted,
        page=page,
        page_size=page_size,
    )
    return PagedResponse.model_validate({**result, "request_id": request.state.request_id})


@router.get("/options")
def scenario_options(request: Request, db: Session = Depends(get_db)) -> dict:
    return {"request_id": request.state.request_id, "items": list_scenario_options(db)}


@router.get("/export")
def export_scenarios(
    name: str | None = None,
    category: str | None = None,
    enabled: bool | None = None,
    published_from: datetime | None = None,
    published_to: datetime | None = None,
    deleted: bool = False,
    db: Session = Depends(get_db),
):
    result = list_scenarios_page(
        db,
        name=name,
        category=category,
        enabled=enabled,
        published_from=published_from,
        published_to=published_to,
        deleted=deleted,
        page=1,
        page_size=EXPORT_PAGE_SIZE,
    )
    rows = [
        [
            item["scenario_name"],
            item["category"],
            item["published_version_no"],
            item["material_count"],
            item["field_count"],
            item["rule_count"],
            item["enabled"],
            item["updated_at"],
        ]
        for item in result["items"]
    ]
    return csv_response(
        "审核场景列表.csv",
        ["场景名称", "业务类型", "当前发布版本", "材料数", "字段数", "业务规则数", "启用状态", "更新时间"],
        rows,
    )


@router.post("", response_model=ScenarioDetailResponse, status_code=201)
def create_scenario_endpoint(payload: ScenarioCreateRequest, request: Request, db: Session = Depends(get_db)) -> ScenarioDetailResponse:
    template = create_scenario(db, payload.model_dump())
    return ScenarioDetailResponse.model_validate({**scenario_detail(db, template.scenario_code), "request_id": request.state.request_id})


@router.get("/{scenario_code}", response_model=ScenarioDetailResponse)
def get_scenario(scenario_code: str, request: Request, db: Session = Depends(get_db)) -> ScenarioDetailResponse:
    return ScenarioDetailResponse.model_validate({**scenario_detail(db, scenario_code), "request_id": request.state.request_id})


@router.put("/{scenario_code}", response_model=ScenarioDetailResponse)
def update_scenario_endpoint(
    scenario_code: str, payload: ScenarioUpdateRequest, request: Request, db: Session = Depends(get_db)
) -> ScenarioDetailResponse:
    update_scenario(db, scenario_code, payload.model_dump(exclude_unset=True))
    return ScenarioDetailResponse.model_validate({**scenario_detail(db, scenario_code), "request_id": request.state.request_id})


@router.put("/{scenario_code}/enabled", response_model=ScenarioDetailResponse)
def set_scenario_enabled_endpoint(
    scenario_code: str, payload: ScenarioEnabledRequest, request: Request, db: Session = Depends(get_db)
) -> ScenarioDetailResponse:
    set_scenario_enabled(db, scenario_code, payload.enabled)
    return ScenarioDetailResponse.model_validate({**scenario_detail(db, scenario_code), "request_id": request.state.request_id})


@router.delete("/{scenario_code}", response_model=ScenarioDetailResponse)
def delete_scenario_endpoint(scenario_code: str, request: Request, db: Session = Depends(get_db)) -> ScenarioDetailResponse:
    delete_scenario(db, scenario_code)
    return ScenarioDetailResponse.model_validate({**scenario_detail(db, scenario_code), "request_id": request.state.request_id})


@router.post("/{scenario_code}/restore", response_model=ScenarioDetailResponse)
def restore_scenario_endpoint(scenario_code: str, request: Request, db: Session = Depends(get_db)) -> ScenarioDetailResponse:
    restore_scenario(db, scenario_code)
    return ScenarioDetailResponse.model_validate({**scenario_detail(db, scenario_code), "request_id": request.state.request_id})


@router.post("/{scenario_code}/copy", response_model=ScenarioDetailResponse, status_code=201)
def copy_scenario_endpoint(
    scenario_code: str, payload: ScenarioCopyRequest, request: Request, db: Session = Depends(get_db)
) -> ScenarioDetailResponse:
    template = copy_scenario(db, scenario_code, payload.model_dump())
    return ScenarioDetailResponse.model_validate({**scenario_detail(db, template.scenario_code), "request_id": request.state.request_id})


# ---------------------------------------------------------------------------
# 配置草稿：材料 / 字段 / 规则 / 报告章节
# ---------------------------------------------------------------------------


@router.get("/{scenario_code}/draft", response_model=ScenarioDraftViewResponse)
def get_scenario_draft(scenario_code: str, request: Request, db: Session = Depends(get_db)) -> ScenarioDraftViewResponse:
    template = template_or_404(db, scenario_code)
    return _draft_response(request, draft_config_view(db, template))


@router.post("/{scenario_code}/draft/materials", response_model=ScenarioDraftViewResponse, status_code=201)
def create_material(scenario_code: str, payload: DraftMaterialRequest, request: Request, db: Session = Depends(get_db)) -> ScenarioDraftViewResponse:
    draft = create_draft_material(db, scenario_code, payload.model_dump())
    return _draft_response(request, scenario_admin_service._draft_config_payload(db, draft))


@router.put("/{scenario_code}/draft/materials/{material_key}", response_model=ScenarioDraftViewResponse)
def update_material(
    scenario_code: str, material_key: str, payload: DraftMaterialUpdateRequest, request: Request, db: Session = Depends(get_db)
) -> ScenarioDraftViewResponse:
    draft = update_draft_material(db, scenario_code, material_key, payload.model_dump(exclude_unset=True))
    return _draft_response(request, scenario_admin_service._draft_config_payload(db, draft))


@router.delete("/{scenario_code}/draft/materials/{material_key}", response_model=ScenarioDraftViewResponse)
def delete_material(scenario_code: str, material_key: str, request: Request, db: Session = Depends(get_db)) -> ScenarioDraftViewResponse:
    draft = delete_draft_material(db, scenario_code, material_key)
    return _draft_response(request, scenario_admin_service._draft_config_payload(db, draft))


@router.post("/{scenario_code}/draft/fields", response_model=ScenarioDraftViewResponse, status_code=201)
def create_field(scenario_code: str, payload: DraftFieldRequest, request: Request, db: Session = Depends(get_db)) -> ScenarioDraftViewResponse:
    draft = create_draft_field(db, scenario_code, payload.model_dump())
    return _draft_response(request, scenario_admin_service._draft_config_payload(db, draft))


@router.put("/{scenario_code}/draft/fields/{field_key}", response_model=ScenarioDraftViewResponse)
def update_field(
    scenario_code: str, field_key: str, payload: DraftFieldUpdateRequest, request: Request, db: Session = Depends(get_db)
) -> ScenarioDraftViewResponse:
    draft = update_draft_field(db, scenario_code, field_key, payload.model_dump(exclude_unset=True))
    return _draft_response(request, scenario_admin_service._draft_config_payload(db, draft))


@router.delete("/{scenario_code}/draft/fields/{field_key}", response_model=ScenarioDraftViewResponse)
def delete_field(scenario_code: str, field_key: str, request: Request, db: Session = Depends(get_db)) -> ScenarioDraftViewResponse:
    draft = delete_draft_field(db, scenario_code, field_key)
    return _draft_response(request, scenario_admin_service._draft_config_payload(db, draft))


@router.post("/{scenario_code}/draft/rules", response_model=ScenarioDraftViewResponse, status_code=201)
def create_rule(scenario_code: str, payload: DraftRuleRequest, request: Request, db: Session = Depends(get_db)) -> ScenarioDraftViewResponse:
    draft = create_draft_rule(db, scenario_code, payload.model_dump())
    return _draft_response(request, scenario_admin_service._draft_config_payload(db, draft))


@router.put("/{scenario_code}/draft/rules/{rule_key}", response_model=ScenarioDraftViewResponse)
def update_rule(
    scenario_code: str, rule_key: str, payload: DraftRuleRequest, request: Request, db: Session = Depends(get_db)
) -> ScenarioDraftViewResponse:
    draft = update_draft_rule(db, scenario_code, rule_key, payload.model_dump())
    return _draft_response(request, scenario_admin_service._draft_config_payload(db, draft))


@router.put("/{scenario_code}/draft/rules/{rule_key}/enabled", response_model=ScenarioDraftViewResponse)
def set_rule_enabled(
    scenario_code: str, rule_key: str, payload: DraftRuleEnabledRequest, request: Request, db: Session = Depends(get_db)
) -> ScenarioDraftViewResponse:
    draft = set_draft_rule_enabled(db, scenario_code, rule_key, payload.enabled)
    return _draft_response(request, scenario_admin_service._draft_config_payload(db, draft))


@router.delete("/{scenario_code}/draft/rules/{rule_key}", response_model=ScenarioDraftViewResponse)
def delete_rule(scenario_code: str, rule_key: str, request: Request, db: Session = Depends(get_db)) -> ScenarioDraftViewResponse:
    draft = delete_draft_rule(db, scenario_code, rule_key)
    return _draft_response(request, scenario_admin_service._draft_config_payload(db, draft))


@router.post("/{scenario_code}/draft/sections", response_model=ScenarioDraftViewResponse, status_code=201)
def create_section(scenario_code: str, payload: DraftSectionRequest, request: Request, db: Session = Depends(get_db)) -> ScenarioDraftViewResponse:
    draft = create_draft_section(db, scenario_code, payload.model_dump())
    return _draft_response(request, scenario_admin_service._draft_config_payload(db, draft))


@router.put("/{scenario_code}/draft/sections/{section_key}", response_model=ScenarioDraftViewResponse)
def update_section(
    scenario_code: str, section_key: str, payload: DraftSectionUpdateRequest, request: Request, db: Session = Depends(get_db)
) -> ScenarioDraftViewResponse:
    draft = update_draft_section(db, scenario_code, section_key, payload.model_dump(exclude_unset=True))
    return _draft_response(request, scenario_admin_service._draft_config_payload(db, draft))


@router.delete("/{scenario_code}/draft/sections/{section_key}", response_model=ScenarioDraftViewResponse)
def delete_section(scenario_code: str, section_key: str, request: Request, db: Session = Depends(get_db)) -> ScenarioDraftViewResponse:
    draft = delete_draft_section(db, scenario_code, section_key)
    return _draft_response(request, scenario_admin_service._draft_config_payload(db, draft))


@router.post("/{scenario_code}/draft/publish", response_model=ScenarioPublishResponse, status_code=201)
def publish_scenario_draft(
    scenario_code: str, payload: ScenarioPublishRequest, request: Request, db: Session = Depends(get_db)
) -> ScenarioPublishResponse:
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
        manifest_hash=version.manifest_hash,
        material_count=len(manifest.get("materials", [])),
        field_count=len(manifest.get("fields", [])),
        rule_count=len(manifest.get("rules", [])),
        published_at=version.published_at,
        reused=reused,
    )


@router.get("/{scenario_code}/draft/export")
def export_scenario_draft(scenario_code: str, db: Session = Depends(get_db)) -> StreamingResponse:
    """导出当前草稿的有效配置（与发布使用同一编译结果）。

    导出使用 manifest 自己的格式版本：节点版写出节点版的七张业务表，
    v1/v2 沿用各自的列。不能用另一种格式的列硬套，也不能静默降级。
    """
    from ..services.scenario_admin_service import compile_draft_manifest, ensure_draft

    template = template_or_404(db, scenario_code)
    draft = ensure_draft(db, template)
    manifest = compile_draft_manifest(db, template, draft)
    content = _export_manifest(manifest)
    filename = f"{template.scenario_name}-配置草稿-有效配置.xlsx"
    return StreamingResponse(
        BytesIO(content),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"},
    )


# ---------------------------------------------------------------------------
# 版本
# ---------------------------------------------------------------------------


@router.post("/{scenario_code}/versions/{version_no}/task-schema", response_model=TaskSchemaPreviewResponse)
def preview_task_schema(
    scenario_code: str,
    version_no: int,
    payload: TaskSchemaPreviewRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> TaskSchemaPreviewResponse:
    """在方案编辑/发布前展示实际执行范围（不写库）。

    勾选规则时后端会自动补入来源规则与字段抽取依赖、并始终保留系统报告规则；
    这些补入项必须在这里就能看到，否则用户会以为自己取消掉的规则真的不会执行。
    """
    scenario = scenario_or_404(db, scenario_code, version_no)
    manifest = scenario.manifest_json
    schema = build_task_schema(manifest, payload.enabled_rule_keys)
    scope = _task_scope_view(manifest, schema)
    rule_names = {item["rule_key"]: item["name"] for item in manifest.get("rules", [])}
    return TaskSchemaPreviewResponse(
        request_id=request.state.request_id,
        enabled_rule_keys=schema["enabled_rule_keys"],
        auto_included_rule_keys=schema["auto_included_rule_keys"],
        unenabled_rule_keys=schema["unenabled_rule_keys"],
        auto_included_rule_names=[rule_names.get(key, key) for key in schema["auto_included_rule_keys"]],
        unconfigured_check_nodes=schema["unconfigured_check_nodes"],
        scope=scope,
        field_count=len(schema["fields"]),
    )


def _task_scope_view(manifest: dict, schema: dict) -> dict:
    """任务实际执行范围：区分模板未配置 / 任务未启用 / 已启用。"""
    rule_names = {item["rule_key"]: item["name"] for item in manifest.get("rules", [])}
    auto_reasons = schema.get("auto_included_reasons", {})
    return {
        "enabled_rules": [
            {"rule_key": key, "name": rule_names.get(key, key)} for key in schema["enabled_rule_keys"]
        ],
        "unenabled_rules": [
            {"rule_key": key, "name": rule_names.get(key, key)} for key in schema["unenabled_rule_keys"]
        ],
        "auto_included_rules": [
            {"rule_key": key, "name": rule_names.get(key, key), "reason": auto_reasons.get(key)}
            for key in schema["auto_included_rule_keys"]
        ],
        "auto_included_reasons": dict(auto_reasons),
        "enabled_rule_keys": list(schema["enabled_rule_keys"]),
        "unenabled_rule_keys": list(schema["unenabled_rule_keys"]),
        "auto_included_rule_keys": list(schema["auto_included_rule_keys"]),
        "unconfigured_nodes": list(manifest.get("unconfigured_check_nodes", [])),
        "capability_gaps": list(manifest.get("capability_gaps", [])),
        # 能力覆盖随版本冻结（含能力版本与每条规则的实现状态）：任务范围据此说明
        # 本次实际执行的是哪些能力、哪一版，以及哪些引用当前没有判定实现。
        "capability_coverage": dict(manifest.get("capability_coverage") or {}),
        "capability_version": manifest.get("capability_version"),
    }


@router.post("/{scenario_code}/versions/{version_no}/tasks", response_model=CreateTaskResponse, status_code=201)
def create_audit_task(
    scenario_code: str,
    version_no: int,
    payload: CreateTaskRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> CreateTaskResponse:
    """旧入口已收回：新审核任务必须从已发布的方案版本创建。

    按场景版本直接建任务会绕过方案的范围冻结（显式规则、自动依赖与快照），
    因此这里返回明确的业务错误并给出修正入口，不静默降级。
    """
    create_task(db, scenario_code, version_no, payload)
