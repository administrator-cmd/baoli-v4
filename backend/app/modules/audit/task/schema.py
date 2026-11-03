"""审核响应模型。"""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class TaskSchemaPreviewResponse(BaseModel):
    """方案范围预览。范围完全等于勾选的规则——系统不补入任何规则，
    因此只有「已启用」与「未启用」两类，没有「自动补入」。"""

    enabled_rule_keys: list[str]
    unenabled_rule_keys: list[str] = Field(default_factory=list)
    scope: dict[str, Any] = Field(default_factory=dict)
    field_count: int = 0


class AuditTaskResponse(BaseModel):
    task_no: str
    task_name: str
    # 任务备注是可编辑基本资料；执行快照不随重命名或备注修改变化。
    remark: str | None = None
    # 基本资料的时间：创建=任务建立时刻，更新=名称/备注/流程推进等任何写入。
    created_at: datetime | None = None
    updated_at: datetime | None = None
    # 任务创建时冻结的方案身份。
    plan: dict[str, Any] | None = None
    scenario_code: str
    scenario_name: str
    version_no: int
    status: str
    current_node: dict[str, Any]
    nodes: list[dict[str, Any]]
    documents: list[dict[str, Any]]
    fields: list[dict[str, Any]]
    # 字段结果统计与按材料/期间/单位汇总的缺项；页面统计与筛选用同一口径。
    field_summary: dict[str, Any] | None = None
    field_correction_candidates: list[dict[str, Any]] = Field(default_factory=list)
    rules: list[dict[str, Any]]
    # 任务输入修订号：首次补传、替换材料、人工有效值变化都会递增。
    # 提交执行/复核命令时带上它，过期修订的命令会被明确拒绝。
    input_revision: int = 0
    # 本任务实际执行范围：未配置 / 未启用 / 不适用 的区分依据。
    scope: dict[str, Any] = Field(default_factory=dict)
    # 任务冻结的技术容器、字段与展示结构；页面按它通用渲染。
    display: dict[str, Any] = Field(default_factory=dict)
    # 该任务上最近一次后台操作的受理状态；有活跃操作时页面据此继续查询。
    latest_operation: dict[str, Any] | None = None


class TaskOptionItem(BaseModel):
    """任务下拉项：按名称或编号模糊匹配，最多 50 条。"""

    task_no: str
    task_name: str
    scenario_name: str
    status: str
