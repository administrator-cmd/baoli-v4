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
    DraftDisabledSourceRowUpdateRequest,
    DraftDisabledSourceRowActivateRequest,
    DraftDisabledSourceRowActivationPreviewResponse,
    DraftFieldRequest,
    DraftFieldUpdateRequest,
    DraftMaterialRequest,
    DraftNodeRequest,
    DraftNodeUpdateRequest,
    DraftMaterialUpdateRequest,
    DraftRuleEnabledRequest,
    DraftRuleRequest,
    DraftSectionRequest,
    DraftSectionUpdateRequest,
    DraftSheetSelectionRequest,
    PagedResponse,
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
    create_draft_field,
    create_draft_material,
    create_draft_node,
    create_draft_rule,
    create_draft_section,
    create_scenario,
    list_admin_scenario_options,
    select_draft_sheets,
    delete_draft_field,
    delete_draft_material,
    delete_draft_node,
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
    update_draft_node,
    update_draft_rule,
    update_draft_section,
    update_scenario,
)
from ..services.task_lookup import scenario_or_404
from ..services.task_service import create_task
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
    """按统一模板协议导出。

    系统只有一份协议，因此不再按 manifest 的「格式版本」在多个导出器之间分派——
    那种分派本身就是已删除的双轨 Profile 的产物。导出的 Sheet 集合由**配置实际用到的
    内容**决定（见 `template_export_service.build_scenario_version_export`）。
    """
    return build_scenario_version_export(manifest)


def _draft_response(request: Request, payload: dict) -> ScenarioDraftViewResponse:
    return ScenarioDraftViewResponse.model_validate({**payload, "request_id": request.state.request_id})


# 场景列表与基本资料


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
    """场景分页列表：按名称、业务类型、启用状态与发布时间筛选。"""
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
    """**业务用户可用**的场景下拉：只含已启用且已有已发布版本的场景。

    方案与任务的场景选择器用它。草稿、未发布、停用或已删除的场景不出现——
    业务用户只能从已发布且启用的场景创建方案（需求 41）。
    """
    return {"request_id": request.state.request_id, "items": list_scenario_options(db)}


@router.get("/admin-options")
def admin_scenario_options(request: Request, db: Session = Depends(get_db)) -> dict:
    """**管理员用**的场景下拉：未删除的全部场景及其实时状态。

    导入页与场景管理页在场景**发布之前**就要选中它（先建场景、再上传规则库），
    因此不能按「已发布」过滤。`available` 如实标出该场景当前能否被业务用户使用。
    """
    return {"request_id": request.state.request_id, "items": list_admin_scenario_options(db)}


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
    """按当前筛选导出场景列表 CSV。"""
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
    """新建场景（页面配置）：没有 Excel 作业与工作表行号，不伪造导入来源。"""
    template = create_scenario(db, payload.model_dump(by_alias=True))
    return ScenarioDetailResponse.model_validate({**scenario_detail(db, template.scenario_code), "request_id": request.state.request_id})


@router.get("/{scenario_code}", response_model=ScenarioDetailResponse)
def get_scenario(scenario_code: str, request: Request, db: Session = Depends(get_db)) -> ScenarioDetailResponse:
    """场景详情：当前发布版本与配置概览。"""
    return ScenarioDetailResponse.model_validate({**scenario_detail(db, scenario_code), "request_id": request.state.request_id})


@router.put("/{scenario_code}", response_model=ScenarioDetailResponse)
def update_scenario_endpoint(
    scenario_code: str, payload: ScenarioUpdateRequest, request: Request, db: Session = Depends(get_db)
) -> ScenarioDetailResponse:
    """编辑场景基本资料（名称、业务类型、说明）。"""
    update_scenario(db, scenario_code, payload.model_dump(exclude_unset=True, by_alias=True))
    return ScenarioDetailResponse.model_validate({**scenario_detail(db, scenario_code), "request_id": request.state.request_id})


@router.put("/{scenario_code}/enabled", response_model=ScenarioDetailResponse)
def set_scenario_enabled_endpoint(
    scenario_code: str, payload: ScenarioEnabledRequest, request: Request, db: Session = Depends(get_db)
) -> ScenarioDetailResponse:
    """启用 / 停用场景；已停用的场景不能用于创建任务。"""
    set_scenario_enabled(db, scenario_code, payload.enabled)
    return ScenarioDetailResponse.model_validate({**scenario_detail(db, scenario_code), "request_id": request.state.request_id})


@router.delete("/{scenario_code}", response_model=ScenarioDetailResponse)
def delete_scenario_endpoint(scenario_code: str, request: Request, db: Session = Depends(get_db)) -> ScenarioDetailResponse:
    """逻辑删除场景；历史版本与任务保留。"""
    delete_scenario(db, scenario_code)
    return ScenarioDetailResponse.model_validate({**scenario_detail(db, scenario_code), "request_id": request.state.request_id})


@router.post("/{scenario_code}/restore", response_model=ScenarioDetailResponse)
def restore_scenario_endpoint(scenario_code: str, request: Request, db: Session = Depends(get_db)) -> ScenarioDetailResponse:
    """恢复已逻辑删除的场景。"""
    restore_scenario(db, scenario_code)
    return ScenarioDetailResponse.model_validate({**scenario_detail(db, scenario_code), "request_id": request.state.request_id})


# 配置草稿：材料 / 字段 / 规则 / 报告章节
#
# 写入统一收口：记录操作、递增草稿修订号，并刷新发布就绪状态。
# 编译不通过时草稿照常保存、已发布版本保持不变，原因在 draft_config_view.sync_error 里如实返回。


@router.get("/{scenario_code}/draft", response_model=ScenarioDraftViewResponse)
def get_scenario_draft(scenario_code: str, request: Request, db: Session = Depends(get_db)) -> ScenarioDraftViewResponse:
    """场景配置草稿：材料、字段、规则、报告章节与草稿修订号。"""
    template = template_or_404(db, scenario_code)
    return _draft_response(request, draft_config_view(db, template))


@router.put("/{scenario_code}/draft/disabled-source-rows/{row_key}", response_model=ScenarioDraftViewResponse)
def update_disabled_source_row(
    scenario_code: str,
    row_key: str,
    payload: DraftDisabledSourceRowUpdateRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> ScenarioDraftViewResponse:
    draft = scenario_admin_service.update_draft_disabled_source_row(
        db, scenario_code, row_key, payload.model_dump()
    )
    return _draft_response(request, scenario_admin_service._draft_config_payload(db, draft))


@router.post("/{scenario_code}/draft/disabled-source-rows/{row_key}/activate", response_model=ScenarioDraftViewResponse)
def activate_disabled_source_row(
    scenario_code: str,
    row_key: str,
    payload: DraftDisabledSourceRowActivateRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> ScenarioDraftViewResponse:
    draft = scenario_admin_service.activate_disabled_source_row(
        db, scenario_code, row_key, expected_revision=payload.expected_revision
    )
    return _draft_response(request, scenario_admin_service._draft_config_payload(db, draft))


@router.get("/{scenario_code}/draft/disabled-source-rows/{row_key}/activation-preview", response_model=DraftDisabledSourceRowActivationPreviewResponse)
def preview_disabled_source_row_activation(
    scenario_code: str,
    row_key: str,
    request: Request,
    db: Session = Depends(get_db),
) -> DraftDisabledSourceRowActivationPreviewResponse:
    preview = scenario_admin_service.preview_disabled_rule_group(db, scenario_code, row_key)
    return DraftDisabledSourceRowActivationPreviewResponse.model_validate({
        **preview, "request_id": request.state.request_id,
    })


@router.put("/{scenario_code}/draft/sheets", response_model=ScenarioDraftViewResponse)
def select_sheets(
    scenario_code: str, payload: DraftSheetSelectionRequest, request: Request, db: Session = Depends(get_db)
) -> ScenarioDraftViewResponse:
    """整体替换本场景**选用**的 Sheet 清单（需求 23/24）。

    场景负责配置「使用哪些 Sheet、Sheet 对应哪些节点及其流程关系」；统一模板协议
    负责定义「系统支持哪些 Sheet 以及每张 Sheet 的结构」。两者职责分离，
    但只有一份 Profile 文件。

    一次提交完整清单：Sheet 选择是一份集合，逐条增删会让结果取决于历史操作顺序。
    清空清单是合法配置（还没选表），不是错误。
    """
    draft = select_draft_sheets(db, scenario_code, [item.model_dump() for item in payload.sheets])
    return _draft_response(request, scenario_admin_service._draft_config_payload(db, draft))


@router.post("/{scenario_code}/draft/nodes", response_model=ScenarioDraftViewResponse, status_code=201)
def create_node(scenario_code: str, payload: DraftNodeRequest, request: Request, db: Session = Depends(get_db)) -> ScenarioDraftViewResponse:
    """在草稿中新增**节点实例**。

    节点由用户配置：实例码、能力类型、顺序与依赖都由请求给出，系统不按规则内容
    自动生成节点，也不按表头推断流程关系。
    """
    draft = create_draft_node(db, scenario_code, payload.model_dump())
    return _draft_response(request, scenario_admin_service._draft_config_payload(db, draft))


@router.put("/{scenario_code}/draft/nodes/{node_code}", response_model=ScenarioDraftViewResponse)
def update_node(
    scenario_code: str, node_code: str, payload: DraftNodeUpdateRequest, request: Request, db: Session = Depends(get_db)
) -> ScenarioDraftViewResponse:
    """修改草稿中的节点实例（只覆盖请求里出现的字段）。"""
    draft = update_draft_node(db, scenario_code, node_code, payload.model_dump(exclude_unset=True))
    return _draft_response(request, scenario_admin_service._draft_config_payload(db, draft))


@router.delete("/{scenario_code}/draft/nodes/{node_code}", response_model=ScenarioDraftViewResponse)
def delete_node(scenario_code: str, node_code: str, request: Request, db: Session = Depends(get_db)) -> ScenarioDraftViewResponse:
    """删除草稿中的节点实例；仍被规则或下游节点引用时明确 409，不级联删除。"""
    draft = delete_draft_node(db, scenario_code, node_code)
    return _draft_response(request, scenario_admin_service._draft_config_payload(db, draft))


@router.post("/{scenario_code}/draft/materials", response_model=ScenarioDraftViewResponse, status_code=201)
def create_material(scenario_code: str, payload: DraftMaterialRequest, request: Request, db: Session = Depends(get_db)) -> ScenarioDraftViewResponse:
    """在草稿中新增材料位，返回最新草稿视图。"""
    draft = create_draft_material(db, scenario_code, payload.model_dump())
    return _draft_response(request, scenario_admin_service._draft_config_payload(db, draft))


@router.put("/{scenario_code}/draft/materials/{material_key}", response_model=ScenarioDraftViewResponse)
def update_material(
    scenario_code: str, material_key: str, payload: DraftMaterialUpdateRequest, request: Request, db: Session = Depends(get_db)
) -> ScenarioDraftViewResponse:
    """修改草稿中的材料位（只覆盖请求里出现的字段）。"""
    draft = update_draft_material(db, scenario_code, material_key, payload.model_dump(exclude_unset=True))
    return _draft_response(request, scenario_admin_service._draft_config_payload(db, draft))


@router.delete("/{scenario_code}/draft/materials/{material_key}", response_model=ScenarioDraftViewResponse)
def delete_material(scenario_code: str, material_key: str, request: Request, db: Session = Depends(get_db)) -> ScenarioDraftViewResponse:
    """删除草稿中的材料位；仍被字段或规则引用时明确 409，不级联删除。"""
    draft = delete_draft_material(db, scenario_code, material_key)
    return _draft_response(request, scenario_admin_service._draft_config_payload(db, draft))


@router.post("/{scenario_code}/draft/fields", response_model=ScenarioDraftViewResponse, status_code=201)
def create_field(scenario_code: str, payload: DraftFieldRequest, request: Request, db: Session = Depends(get_db)) -> ScenarioDraftViewResponse:
    """在草稿中新增字段，返回最新草稿视图。"""
    draft = create_draft_field(db, scenario_code, payload.model_dump())
    return _draft_response(request, scenario_admin_service._draft_config_payload(db, draft))


@router.put("/{scenario_code}/draft/fields/{field_key}", response_model=ScenarioDraftViewResponse)
def update_field(
    scenario_code: str, field_key: str, payload: DraftFieldUpdateRequest, request: Request, db: Session = Depends(get_db)
) -> ScenarioDraftViewResponse:
    """修改草稿中的字段（只覆盖请求里出现的字段）。"""
    draft = update_draft_field(db, scenario_code, field_key, payload.model_dump(exclude_unset=True))
    return _draft_response(request, scenario_admin_service._draft_config_payload(db, draft))


@router.delete("/{scenario_code}/draft/fields/{field_key}", response_model=ScenarioDraftViewResponse)
def delete_field(scenario_code: str, field_key: str, request: Request, db: Session = Depends(get_db)) -> ScenarioDraftViewResponse:
    """删除草稿中的字段；仍被规则或报告章节引用时明确 409，不级联删除。"""
    draft = delete_draft_field(db, scenario_code, field_key)
    return _draft_response(request, scenario_admin_service._draft_config_payload(db, draft))


@router.post("/{scenario_code}/draft/rules", response_model=ScenarioDraftViewResponse, status_code=201)
def create_rule(scenario_code: str, payload: DraftRuleRequest, request: Request, db: Session = Depends(get_db)) -> ScenarioDraftViewResponse:
    """在草稿中新增规则，返回最新草稿视图。"""
    draft = create_draft_rule(db, scenario_code, payload.model_dump())
    return _draft_response(request, scenario_admin_service._draft_config_payload(db, draft))


@router.put("/{scenario_code}/draft/rules/{rule_key}", response_model=ScenarioDraftViewResponse)
def update_rule(
    scenario_code: str, rule_key: str, payload: DraftRuleRequest, request: Request, db: Session = Depends(get_db)
) -> ScenarioDraftViewResponse:
    """整体替换草稿中这条规则的定义；引用了它的下游规则名记录在操作明细里。"""
    draft = update_draft_rule(db, scenario_code, rule_key, payload.model_dump(exclude_unset=True))
    return _draft_response(request, scenario_admin_service._draft_config_payload(db, draft))


@router.put("/{scenario_code}/draft/rules/{rule_key}/enabled", response_model=ScenarioDraftViewResponse)
def set_rule_enabled(
    scenario_code: str, rule_key: str, payload: DraftRuleEnabledRequest, request: Request, db: Session = Depends(get_db)
) -> ScenarioDraftViewResponse:
    """启用 / 停用草稿中的规则；停用规则不进入发布后的执行范围。"""
    draft = set_draft_rule_enabled(db, scenario_code, rule_key, payload.enabled)
    return _draft_response(request, scenario_admin_service._draft_config_payload(db, draft))


@router.delete("/{scenario_code}/draft/rules/{rule_key}", response_model=ScenarioDraftViewResponse)
def delete_rule(scenario_code: str, rule_key: str, request: Request, db: Session = Depends(get_db)) -> ScenarioDraftViewResponse:
    """删除草稿中的规则。"""
    draft = delete_draft_rule(db, scenario_code, rule_key)
    return _draft_response(request, scenario_admin_service._draft_config_payload(db, draft))


@router.post("/{scenario_code}/draft/sections", response_model=ScenarioDraftViewResponse, status_code=201)
def create_section(scenario_code: str, payload: DraftSectionRequest, request: Request, db: Session = Depends(get_db)) -> ScenarioDraftViewResponse:
    """在草稿中新增报告章节。"""
    draft = create_draft_section(db, scenario_code, payload.model_dump())
    return _draft_response(request, scenario_admin_service._draft_config_payload(db, draft))


@router.put("/{scenario_code}/draft/sections/{section_key}", response_model=ScenarioDraftViewResponse)
def update_section(
    scenario_code: str, section_key: str, payload: DraftSectionUpdateRequest, request: Request, db: Session = Depends(get_db)
) -> ScenarioDraftViewResponse:
    """修改草稿中的报告章节。"""
    draft = update_draft_section(db, scenario_code, section_key, payload.model_dump(exclude_unset=True))
    return _draft_response(request, scenario_admin_service._draft_config_payload(db, draft))


@router.delete("/{scenario_code}/draft/sections/{section_key}", response_model=ScenarioDraftViewResponse)
def delete_section(scenario_code: str, section_key: str, request: Request, db: Session = Depends(get_db)) -> ScenarioDraftViewResponse:
    """删除草稿中的报告章节。"""
    draft = delete_draft_section(db, scenario_code, section_key)
    return _draft_response(request, scenario_admin_service._draft_config_payload(db, draft))


@router.post("/{scenario_code}/draft/publish", response_model=ScenarioPublishResponse, status_code=201)
def publish_scenario_draft(
    scenario_code: str, payload: ScenarioPublishRequest, request: Request, db: Session = Depends(get_db)
) -> ScenarioPublishResponse:
    """**发布场景**：把当前草稿编译成一份不可变的已发布版本。

    这是管理员确认配置的动作，与「保存草稿」严格分开：保存只写草稿，
    只有本接口会产生业务用户可用的 `scenario_version`。

    发布前的检查全部在编译期完成，任一项不通过即 409/400 并附具体原因，
    **不产生可用版本**：节点依赖与顺序、Sheet 绑定、材料/字段/规则引用、
    计算配置、能力缺口、报告章节，以及「至少选用一张 Sheet」。

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

    导出格式由 manifest 自己的格式版本决定，规则见 _export_manifest。
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


# 版本


@router.post("/{scenario_code}/versions/{version_no}/task-schema", response_model=TaskSchemaPreviewResponse)
def preview_task_schema(
    scenario_code: str,
    version_no: int,
    payload: TaskSchemaPreviewRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> TaskSchemaPreviewResponse:
    """在方案编辑/发布前展示实际执行范围（不写库）。

    **范围完全等于用户勾选的规则**——系统不补入任何规则，因此这里展示的
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
        unconfigured_check_node_types=schema["unconfigured_check_node_types"],
        unconfigured_check_node_codes=schema["unconfigured_check_node_codes"],
        scope=scope,
        field_count=len(schema["fields"]),
    )


def _task_scope_view(manifest: dict, schema: dict) -> dict:
    """任务实际执行范围：区分模板未配置 / 任务未启用 / 已启用。

    **没有「自动补入」这一类**：勾了什么就跑什么，范围完全等于这份方案的规则选择。
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
        # 未配置的检查类别按**能力类型**汇总；页面若要判断某个具体节点，
        # 用实例码口径的 unconfigured_check_node_codes，不交叉比较。
        "unconfigured_node_types": list(manifest.get("unconfigured_check_node_types", [])),
        "unconfigured_node_codes": list(manifest.get("unconfigured_check_node_codes", [])),
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
