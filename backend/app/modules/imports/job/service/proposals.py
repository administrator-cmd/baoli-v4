"""规则库候选配置维护与业务视图组装。"""

from copy import deepcopy
from typing import Any

from sqlalchemy.orm import Session

from app.common.exceptions import BusinessError
from app.constants.execution import OPERATOR_LLM_EXECUTE
from app.domain.manifest import rule_comparison_view
from app.modules.imports.job import crud
from app.modules.imports.job.model import ImportJob
from app.modules.imports.job.service.lookup import ImportsJobLookupService
from app.modules.system.operation.service.background import OperationBackgroundService
from app.modules.system.operation.service.operation_types import OBJECT_IMPORT_JOB


class ImportsJobProposalsService:
    """本实体的服务方法；db 由构造时注入。"""

    def __init__(self, db: Session | None = None) -> None:
        self.db = db

    def update_import_proposal(self, job_no: str, *, group: str, candidate_ref: str,
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
        job = ImportsJobLookupService(self.db)._job_or_404( job_no, lock=True)
        ImportsJobLookupService(self.db)._ensure_import_mutable( job)
        OperationBackgroundService(self.db).assert_no_active_operation(OBJECT_IMPORT_JOB, job_no)
        if job.draft_revision != expected_job_revision:
            raise BusinessError(
                "IMPORT_DRAFT_REVISION_CONFLICT", "解析结果已修改，请刷新后再保存", status_code=409,
                details={"expected": expected_job_revision, "actual": job.draft_revision},
            )
        unsupported = changes.keys() - editable_attributes[group]
        if unsupported:
            raise BusinessError("IMPORT_PROPOSAL_ATTRIBUTE_INVALID", "提交的属性不属于这类解析配置",
                                details={"group": group, "attributes": sorted(unsupported)})
        proposals = deepcopy(ImportsJobLookupService()._saved_proposals(job))
        item = next((item for item in proposals[group] if item["candidate_ref"] == candidate_ref), None)
        if item is None:
            raise BusinessError("IMPORT_PROPOSAL_NOT_FOUND", "解析结果中不存在该项配置", status_code=404,
                                details={"group": group, "candidate_ref": candidate_ref})
        item.update(changes)
        interpretation = item.get("business_context", {}).get("interpretation")
        if interpretation is not None:
            interpretation["edited_after_interpretation"] = True
        if group == "rules" and changes.get("rule_role") == "CHECK" and item.get("operator") == "RISK_TRIGGER":
            # 管理员明确改为普通核查后，不能仍被旧风险动作识别为风险规则。
            item["operator"] = OPERATOR_LLM_EXECUTE
        job.proposals_json = proposals
        job.draft_revision += 1
        crud.ImportsJobCRUD(self.db).flush()
        crud.ImportsJobCRUD(self.db).refresh(job)
        return job


    def _proposal_business_view(self, job: ImportJob) -> dict[str, Any] | None:
        """直接展示模型的业务配置，不要求来源坐标。"""
        if job.proposals_json is None:
            return None
        proposals = ImportsJobLookupService()._saved_proposals(job)
        material_names = {item["candidate_ref"]: item["name"] for item in proposals["materials"]}
        field_labels = {item["candidate_ref"]: item["name"] for item in proposals["fields"]}
        rule_names = {item["candidate_ref"]: item["name"] for item in proposals["rules"]}
        field_meta = {item["candidate_ref"]: item for item in proposals["fields"]}
        calculation_meta = {item["result_code"]: item for item in proposals["calculations"]}

        def with_interpretation(item: dict[str, Any]) -> dict[str, Any]:
            return {**item, "interpretation": item.get("business_context", {}).get("interpretation")}

        return {
            "materials": [with_interpretation(item) for item in proposals["materials"]],
            "fields": [
                {**with_interpretation(item), "material_name": material_names.get(item["material_ref"])}
                for item in proposals["fields"]
            ],
            "rules": [
                {**with_interpretation(item), "comparison": rule_comparison_view(
                    item, material_names, field_labels, rule_names, field_meta,
                    calculation_meta=calculation_meta,
                )}
                for item in proposals["rules"]
            ],
            "calculations": [with_interpretation(item) for item in proposals["calculations"]],
            "report_sections": [with_interpretation(item) for item in proposals["report_sections"]],
            # None 表示历史记录未保存识别结果，不能显示成“没有辅助资料”。
            "supporting_content": proposals.get("supporting_content"),
            # 第二轮逐项核对后的未绑定输入：页面据此提示配置不完整，不改变启用状态。
            "binding_gaps": proposals.get("binding_gaps") or [],
        }
