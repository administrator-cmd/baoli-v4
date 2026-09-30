from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from ..domain import scenario_schema, unified_profile
from ..common.exceptions import BusinessError
from ..repositories import scenario_repository
from .scenario_admin_service import template_or_404
from .scene_template_service import build_scene_template


@dataclass(frozen=True)
class TemplateDownload:
    filename: str
    content: bytes


def template_catalog() -> dict[str, Any]:
    profile = unified_profile.load_unified_profile()
    return {
        "profile_version": profile.get("profile_version"),
        "display_name": profile.get("display_name"),
        "scenario_schema_key": None,
        "scenario_schema_version": None,
        "download_path": "/rulebook-templates/download",
        "download_requires_scenario": True,
        **unified_profile.profile_summary(profile),
    }


def create_download(db: Session, scenario_code: str, requested_roles: list[str]) -> TemplateDownload:
    template = template_or_404(db, scenario_code)
    schema = scenario_schema.normalize_schema(template.schema_json)
    profile = scenario_schema.effective_profile(schema)
    bound = _confirmed_sheet_roles(db, scenario_code, profile)
    if bound:
        selected = bound
        download_only = False
    else:
        selected = _download_only_sheet_roles(profile, requested_roles)
        download_only = True
    content = build_scene_template(
        profile,
        scene_name=template.scenario_name,
        sheet_roles=selected,
        download_only=download_only,
        scenario_schema_identity=scenario_schema.identity(schema),
    )
    return TemplateDownload(filename=f"{template.scenario_name}-规则库模板.xlsx", content=content)


def sheet_catalog(db: Session, scenario_code: str | None) -> dict[str, Any]:
    identity: dict[str, str] | None = None
    if scenario_code:
        scenario = template_or_404(db, scenario_code)
        schema = scenario_schema.normalize_schema(scenario.schema_json)
        identity = scenario_schema.identity(schema)
        profile = scenario_schema.effective_profile(schema)
    else:
        profile = unified_profile.load_unified_profile()
    return {
        "profile_version": profile.get("profile_version"),
        "display_name": profile.get("display_name"),
        "scenario_schema_key": identity["schema_key"] if identity else None,
        "scenario_schema_version": identity["schema_version"] if identity else None,
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


def _confirmed_sheet_roles(db: Session, scenario_code: str, profile: dict[str, Any]) -> list[str]:
    index = unified_profile.sheet_index(profile)
    roles: list[str] = []
    for role in scenario_repository.draft_sheet_roles(db, scenario_code):
        if role in index and role not in roles:
            roles.append(role)
    return roles


def _download_only_sheet_roles(profile: dict[str, Any], requested: list[str]) -> list[str]:
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

