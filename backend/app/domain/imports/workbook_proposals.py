"""规则库模型输出格式与完整 Excel 输入；不做业务检查或候选筛除。"""

from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.common.exceptions import BusinessError
from app.domain.configuration.execution_mode import ExecutionMode
from app.domain.imports.proposal_contract import (
    BindingGap,
    CalculationCandidate,
    FieldProposal,
    MaterialProposal,
    ProposalModel,
    ReportSectionProposal,
    RuleProposal,
    SourceOrigin,
)
from app.domain.imports.workbook import WorkbookIR, WorkbookSheet

ProposalGroup = Literal["materials", "fields", "rules", "calculations", "report_sections"]


class DefinitionInterpretation(BaseModel):
    """模型对定义职责的简短说明，不是推理过程或业务完整性门槛。"""

    model_config = ConfigDict(extra="forbid")
    basis: str = Field(min_length=1, description="原文要求的具体动作或对象，以及为何应独立保留；不能只引用表名、类型标签或关联编号。")
    missing_details: list[str] = Field(description="原文尚未明确的算法、条件或口径；没有则为空，不因缺项删除真实要求或自行停用。")


class SupportingTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")
    group: ProposalGroup
    candidate_ref: str = Field(min_length=1)
    usage: str = Field(min_length=1, description="这份资料具体补充该对象的什么含义、属性或关系；有关联不等于执行依赖。")


class SupportingContent(BaseModel):
    """参与理解的资料，不投影成独立执行规则。"""

    model_config = ConfigDict(extra="forbid")
    support_ref: str = Field(min_length=1)
    kind: Literal["MATERIAL_SCOPE", "DICTIONARY", "MAPPING", "EXPLANATION"]
    title: str = Field(min_length=1)
    content: str = Field(min_length=1, description="保留材料要求、字典条目或映射的实际业务内容，可以分行表达。")
    reason: str = Field(min_length=1, description="为何作为辅助资料，以及怎样使用；不是无关或丢弃的理由。")
    targets: list[SupportingTarget]
    unresolved_references: list[str] = Field(description="原文提到但未能对应到真实定义的名称、编号或关系，不编造目标；无则为空。")
    origin: SourceOrigin


class ModelMaterial(MaterialProposal):
    interpretation: DefinitionInterpretation
    origin: SourceOrigin
    config_status: Literal["CLEAR", "PENDING", "DISABLED"] = Field(description="CLEAR 启用；PENDING 待确认、暂不执行；DISABLED 暂停、不执行。按原文状态填写。")


class ModelField(FieldProposal):
    interpretation: DefinitionInterpretation
    origin: SourceOrigin
    config_status: Literal["CLEAR", "PENDING", "DISABLED"] = Field(description="CLEAR 启用；PENDING 待确认、不抽取；DISABLED 暂停、不抽取。按原文状态填写。")


class ModelRule(RuleProposal):
    interpretation: DefinitionInterpretation
    origin: SourceOrigin
    config_status: Literal["CLEAR", "PENDING", "DISABLED"] = Field(description="CLEAR 启用；PENDING 待确认、暂不执行；DISABLED 暂停、不执行。按原文状态填写。")


class ModelReportSection(ReportSectionProposal):
    interpretation: DefinitionInterpretation
    origin: SourceOrigin
    config_status: Literal["CLEAR", "PENDING", "DISABLED"] = Field(description="CLEAR 启用；PENDING 待确认、不生成；DISABLED 暂停、不生成。按原文状态填写。")


class ModelCalculationParameter(ProposalModel):
    parameter_code: str
    source_type: str
    source_code: str | None = None
    constant_value: Any = None


class ModelCalculation(CalculationCandidate):
    interpretation: DefinitionInterpretation
    origin: SourceOrigin
    status: Literal["CLEAR", "PENDING", "DISABLED"] = Field(description="CLEAR 启用；PENDING 待确认、暂不执行；DISABLED 暂停、不执行。按原文状态填写。")
    parameters: list[ModelCalculationParameter] = Field(default_factory=list)


class InventoryDefinition(BaseModel):
    """第一轮仅定位业务定义；执行属性在第二轮对照原文组织。"""

    candidate_ref: str
    source_id: str | None
    name: str
    interpretation: DefinitionInterpretation
    config_status: Literal["CLEAR", "PENDING", "DISABLED"]
    # 第一轮给出执行方式候选，第二轮对照原行确认；无法判定时为 UNKNOWN。
    execution_mode: ExecutionMode = "UNKNOWN"
    origin: SourceOrigin


class WorkbookInventory(BaseModel):
    materials: list[InventoryDefinition]
    fields: list[InventoryDefinition]
    rules: list[InventoryDefinition]
    calculations: list[InventoryDefinition]
    report_sections: list[InventoryDefinition]
    supporting_content: list[SupportingContent] = Field(description="参与理解的材料适用说明、字典、映射及其他补充；没有则为空，不能强塞入执行目录。")


class WorkbookProposals(BaseModel):
    materials: list[ModelMaterial]
    fields: list[ModelField]
    rules: list[ModelRule]
    calculations: list[ModelCalculation]
    report_sections: list[ModelReportSection]
    supporting_content: list[SupportingContent] = Field(description="第一轮辅助资料完整保留并对照原文修正归属；新增发现的资料也保留。")
    # 第二轮逐项核对公式/条件与输入绑定后的未建立项；空列表表示没有缺项。
    # 它只暴露配置完整性，不改变任何对象的启用/停用状态。必填：模型不能省略核对结果。
    binding_gaps: list[BindingGap] = Field(
        description="逐项核对后仍未建立绑定的输入；没有缺项时返回空数组。",
    )


@dataclass
class PreparedSheet:
    sheet: WorkbookSheet

    def model_input(self) -> dict[str, Any]:
        # 完整原文供两轮理解业务；来源行引用用于保存未经模型改写的单元格。
        return {
            "sheet_name": self.sheet.name,
            "merged_ranges": self.sheet.merged_ranges,
            "rows": [{"row_no": row.row_no, "cells": row.cells} for row in self.sheet.rows],
        }


def prepare_sheets(ir: WorkbookIR) -> dict[str, PreparedSheet]:
    """把全部非空工作表交给模型；空表没有可识别的业务内容，不发送。"""
    sheets = {
        sheet.sheet_key: PreparedSheet(sheet)
        for sheet in ir.sheets if sheet.state != "EMPTY"
    }
    if not sheets:
        raise BusinessError("IMPORT_NO_BOUND_SHEET", "本次导入没有可解析的工作表")
    return sheets
