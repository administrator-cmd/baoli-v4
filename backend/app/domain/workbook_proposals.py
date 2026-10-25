"""规则库模型输出格式与完整 Excel 输入；不做业务检查或候选筛除。"""

from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, Field

from ..common.exceptions import BusinessError
from .execution_mode import ExecutionMode
from .proposals import (
    CalculationCandidate,
    FieldProposal,
    MaterialProposal,
    ProposalModel,
    ReportSectionProposal,
    RuleProposal,
    SourceOrigin,
)
from .workbook import WorkbookIR, WorkbookSheet


class ModelMaterial(MaterialProposal):
    origin: SourceOrigin
    config_status: Literal["CLEAR", "PENDING", "DISABLED"] = Field(description="CLEAR 启用；PENDING 待确认、暂不执行；DISABLED 暂停、不执行。按原文状态填写。")


class ModelField(FieldProposal):
    origin: SourceOrigin
    config_status: Literal["CLEAR", "PENDING", "DISABLED"] = Field(description="CLEAR 启用；PENDING 待确认、不抽取；DISABLED 暂停、不抽取。按原文状态填写。")


class ModelRule(RuleProposal):
    origin: SourceOrigin
    config_status: Literal["CLEAR", "PENDING", "DISABLED"] = Field(description="CLEAR 启用；PENDING 待确认、暂不执行；DISABLED 暂停、不执行。按原文状态填写。")


class ModelReportSection(ReportSectionProposal):
    origin: SourceOrigin
    config_status: Literal["CLEAR", "PENDING", "DISABLED"] = Field(description="CLEAR 启用；PENDING 待确认、不生成；DISABLED 暂停、不生成。按原文状态填写。")


class ModelCalculationParameter(ProposalModel):
    parameter_code: str
    source_type: str
    source_code: str | None = None
    constant_value: Any = None


class ModelCalculation(CalculationCandidate):
    origin: SourceOrigin
    status: Literal["CLEAR", "PENDING", "DISABLED"] = Field(description="CLEAR 启用；PENDING 待确认、暂不执行；DISABLED 暂停、不执行。按原文状态填写。")
    parameters: list[ModelCalculationParameter] = Field(default_factory=list)


class InventoryDefinition(BaseModel):
    """第一轮仅定位业务定义；执行属性在第二轮对照原文组织。"""

    candidate_ref: str
    source_id: str | None
    name: str
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


class WorkbookProposals(BaseModel):
    materials: list[ModelMaterial]
    fields: list[ModelField]
    rules: list[ModelRule]
    calculations: list[ModelCalculation]
    report_sections: list[ModelReportSection]


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
