"""规则库候选配置维护与业务视图组装。"""

from copy import deepcopy
from typing import Any

from sqlalchemy.orm import Session

from ...common.exceptions import BusinessError
from ...constants.execution import OPERATOR_LLM_EXECUTE
from ...domain.manifest import rule_comparison_view
from ...entity.imports import ImportJob
from ..system.background import assert_no_active_operation
from ..system.operation_types import OBJECT_IMPORT_JOB
from .lookup import _ensure_import_mutable, _job_or_404, _saved_proposals


def update_import_proposal(
    db: Session, job_no: str, *, group: str, candidate_ref: str,
    expected_job_revision: int, changes: dict[str, Any],
) -> ImportJob:
    """修改一项已解析业务配置，保留其身份、依赖和未编辑属性。"""
    editable_attributes = {
        "materials": {"name", "description", "required"},
        "fields": {"name", "instruction", "required", "config_status"},
        "rules": {"name", "requirement", "execution_mode", "rule_role", "config_status"},
        "calculations": {"rule_name", "formula", "requirement", "status"},
        "report_sections": {"section_name", "content_instruction", "config_status"},
    }
    job = _job_or_404(db, job_no, lock=True)
    _ensure_import_mutable(db, job)
    assert_no_active_operation(db, OBJECT_IMPORT_JOB, job_no)
    if job.draft_revision != expected_job_revision:
        raise BusinessError(
            "IMPORT_DRAFT_REVISION_CONFLICT", "解析结果已修改，请刷新后再保存", status_code=409,
            details={"expected": expected_job_revision, "actual": job.draft_revision},
        )
    unsupported = changes.keys() - editable_attributes[group]
    if unsupported:
        raise BusinessError("IMPORT_PROPOSAL_ATTRIBUTE_INVALID", "提交的属性不属于这类解析配置",
                            details={"group": group, "attributes": sorted(unsupported)})
    proposals = deepcopy(_saved_proposals(job))
    item = next((item for item in proposals[group] if item["candidate_ref"] == candidate_ref), None)
    if item is None:
        raise BusinessError("IMPORT_PROPOSAL_NOT_FOUND", "解析结果中不存在该项配置", status_code=404,
                            details={"group": group, "candidate_ref": candidate_ref})
    item.update(changes)
    if group == "rules" and changes.get("rule_role") == "CHECK" and item.get("operator") == "RISK_TRIGGER":
        # 管理员明确改为普通核查后，不能仍被旧风险动作识别为风险规则。
        item["operator"] = OPERATOR_LLM_EXECUTE
    job.proposals_json = proposals
    job.draft_revision += 1
    try:
        db.commit()
    except Exception:
        db.rollback()
        raise
    db.refresh(job)
    return job


def _proposal_business_view(job: ImportJob) -> dict[str, Any] | None:
    """直接展示模型的业务配置，不要求来源坐标。"""
    if job.proposals_json is None:
        return None
    proposals = _saved_proposals(job)
    material_names = {item["candidate_ref"]: item["name"] for item in proposals["materials"]}
    field_labels = {item["candidate_ref"]: item["name"] for item in proposals["fields"]}
    rule_names = {item["candidate_ref"]: item["name"] for item in proposals["rules"]}
    field_meta = {item["candidate_ref"]: item for item in proposals["fields"]}
    calculation_meta = {item["result_code"]: item for item in proposals["calculations"]}
    return {
        "materials": proposals["materials"],
        "fields": [
            {**item, "material_name": material_names.get(item["material_ref"])}
            for item in proposals["fields"]
        ],
        "rules": [
            {**item, "comparison": rule_comparison_view(
                item, material_names, field_labels, rule_names, field_meta,
                calculation_meta=calculation_meta,
            )}
            for item in proposals["rules"]
        ],
        "calculations": proposals["calculations"],
        "report_sections": proposals["report_sections"],
    }
