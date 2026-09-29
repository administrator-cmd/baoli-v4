from io import BytesIO
from urllib.parse import quote

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from ..database import get_db
from ..domain import scenario_schema, unified_profile
from ..errors import BusinessError
from ..models import ScenarioDraft, ScenarioDraftSheet
from ..services.scenario_admin_service import template_or_404
from ..services.scene_template_service import build_scene_template


router = APIRouter(prefix="/rulebook-templates", tags=["规则库模板"])


@router.get("")
def template_catalog(request: Request) -> dict:
    """**唯一一份**规则库模板的目录：系统支持的全部 Sheet、节点类型与列协议。

    系统只有一份统一模板协议（`backend/config/unified_profile.json`），
    因此也只有一个下载入口。不再有「合同审核模板 / 财报模板」两个下载地址，
    也不再有 v1-v4 的格式版本选择——那些都来自已删除的双轨 Profile。

    `sheets` 是协议声明的全部 Sheet，也是 base Excel 的完整表清单。
    场景已有确认绑定时，下载只含这些角色；还没有绑定时，必须由调用方传入本次要下载的
    `sheet_roles`，该选择不写入场景。
    """
    profile = unified_profile.load_unified_profile()
    return {
        "request_id": request.state.request_id,
        "profile_version": profile.get("profile_version"),
        "display_name": profile.get("display_name"),
        # 这里没有场景上下文；不要把统一协议版本冒充成场景 Schema 身份。
        "scenario_schema_key": None,
        "scenario_schema_version": None,
        "download_path": "/rulebook-templates/download",
        "download_requires_scenario": True,
        **unified_profile.profile_summary(profile),
    }


@router.get("/download")
def download_template(
    db: Session = Depends(get_db),
    scenario_code: str = Query(
        ...,
        min_length=1,
        description="场景编码。已有确认绑定时只导出这些 Sheet；没有绑定时必须另传 sheet_roles",
    ),
    sheet_roles: list[str] = Query(
        default_factory=list,
        description="仅本次下载的 Sheet 角色。场景已有确认绑定时忽略本参数，且不会写入场景",
    ),
) -> StreamingResponse:
    """下载规则库模板。

    已确认绑定（scenario_draft_sheet）是下载范围的真源，查询参数里的勾选一律忽略。
    还没有确认绑定时，必须由调用方逐项传入 `sheet_roles`。该列表只生成这一份文件，
    不写库、不要求节点、也不默认包含协议全集。

    一次下载就是**一份 base Excel**：同一份模板里的表可以一起下载，导入时按 Sheet
    独立转换，不存在「只能属于一个业务族」的限制。
    """
    template = template_or_404(db, scenario_code)
    schema = scenario_schema.normalize_schema(template.schema_json)
    profile = scenario_schema.effective_profile(schema)
    bound = _confirmed_sheet_roles(db, scenario_code, profile)
    if bound:
        selected = bound
        download_only = False
    else:
        selected = _download_only_sheet_roles(profile, sheet_roles)
        download_only = True
    scene_name = template.scenario_name
    content = build_scene_template(
        profile,
        scene_name=scene_name,
        sheet_roles=selected,
        download_only=download_only,
        scenario_schema_identity=scenario_schema.identity(schema),
    )
    filename = f"{scene_name}-规则库模板.xlsx"
    headers = {"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"}
    return StreamingResponse(
        BytesIO(content),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers=headers,
    )


@router.get("/sheets")
def list_sheets(
    request: Request,
    scenario_code: str | None = Query(default=None, min_length=2, max_length=80),
    db: Session = Depends(get_db),
) -> dict:
    """统一模板协议声明的 Sheet 与列协议。

    页面据此渲染「Sheet 绑定」步骤：每张 Sheet 列出它可以绑定的节点类型与列定义。
    只返回声明，不含任何示例数据。
    """
    # 保持旧调用方的全局目录响应；显式传入场景时才叠加该场景的列别名和 Sheet 名。
    scenario_identity: dict[str, str] | None = None
    if scenario_code:
        scenario = template_or_404(db, scenario_code)
        schema = scenario_schema.normalize_schema(scenario.schema_json)
        scenario_identity = scenario_schema.identity(schema)
        profile = scenario_schema.effective_profile(schema)
    else:
        profile = unified_profile.load_unified_profile()
    return {
        "request_id": request.state.request_id,
        "profile_version": profile.get("profile_version"),
        "display_name": profile.get("display_name"),
        "scenario_schema_key": scenario_identity["schema_key"] if scenario_identity else None,
        "scenario_schema_version": scenario_identity["schema_version"] if scenario_identity else None,
        # 保留旧字段：schema_version 在这里表示统一执行协议自身的版本，
        # 场景 Schema 版本使用上面的 scenario_schema_version。
        "schema_version": profile.get("schema_version"),
        "instruction_sheet": unified_profile.instruction_sheet_name(profile),
        "node_types": [
            {
                "node_type": node["node_type"],
                "display_name": node.get("display_name", node["node_type"]),
                "depends_on_types": list(node.get("depends_on_types", [])),
            }
            for node in profile["node_types"]
        ],
        "sheets": [
            {
                "sheet_role": sheet["sheet_role"],
                "code_prefix": str(sheet["code_prefix"]),
                "display_name": sheet.get("display_name", sheet["sheet_role"]),
                "aliases": list(sheet.get("aliases", [])),
                "header_signature": list(sheet.get("header_signature", [])),
                "node_types": list(sheet["node_types"]),
                "column_count": len(sheet["columns"]),
                "columns": [
                    {
                        "key": column["key"],
                        "name": column["name"],
                        "required": bool(column.get("required")),
                        "row_required": bool(column.get("row_required")),
                        "data_type": column.get("data_type"),
                        "allowed_values": list(column.get("allowed_values", [])),
                        "value_labels": dict(column.get("value_labels", {})),
                        "aliases": list(column.get("aliases", [])),
                    }
                    for column in sheet["columns"]
                ],
            }
            for sheet in profile["sheets"]
        ],
    }


def _confirmed_sheet_roles(db: Session, scenario_code: str, profile: dict) -> list[str]:
    """导入写入草稿后的 Sheet 角色。没有草稿或尚未写入时返回空列表，不回退到协议全集。"""
    from sqlalchemy import select

    draft = db.scalar(select(ScenarioDraft).where(ScenarioDraft.scenario_code == scenario_code))
    if draft is None:
        return []
    index = unified_profile.sheet_index(profile)
    roles: list[str] = []
    for item in db.scalars(
        select(ScenarioDraftSheet)
        .where(ScenarioDraftSheet.draft_id == draft.id)
        .order_by(ScenarioDraftSheet.sort_no, ScenarioDraftSheet.id)
    ).all():
        if item.sheet_role in index and item.sheet_role not in roles:
            roles.append(item.sheet_role)
    return roles


def _download_only_sheet_roles(profile: dict, requested: list[str]) -> list[str]:
    """校验一次性下载勾选。不写库，也不把未勾选的协议表补进来。

    base Excel 是系统唯一的模板，没有业务族这一层：勾选哪些表就下载哪些表，
    未勾选的不补进来，也不默认生成协议全集。
    """
    cleaned: list[str] = []
    for raw in requested:
        role = str(raw).strip()
        if role and role not in cleaned:
            cleaned.append(role)
    if not cleaned:
        raise BusinessError(
            "TEMPLATE_DOWNLOAD_SELECTION_REQUIRED",
            "当前场景还没有确认的 Sheet 绑定。请明确勾选本次要下载的工作表；"
            "下载选择不会写入场景，也不等于最终绑定",
            status_code=409,
        )
    index = unified_profile.sheet_index(profile)
    unknown = [role for role in cleaned if role not in index]
    if unknown:
        raise BusinessError(
            "TEMPLATE_SHEET_ROLE_UNKNOWN",
            "勾选的工作表角色不在统一模板协议中",
            status_code=409,
            details={"sheet_roles": unknown},
        )
    return cleaned
