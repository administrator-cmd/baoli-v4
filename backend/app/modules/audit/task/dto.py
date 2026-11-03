"""审核请求模型。"""


from pydantic import BaseModel, Field


class TaskSchemaPreviewRequest(BaseModel):
    """建任务前预览实际执行范围；允许为空表示“全部启用”。"""

    enabled_rule_keys: list[str] | None = None


class TaskUpdateRequest(BaseModel):
    task_name: str | None = Field(default=None, min_length=1, max_length=255)
    remark: str | None = Field(default=None, max_length=4000)


class TaskCopyRequest(BaseModel):
    """复制任务：按源任务所属方案的当前范围重新创建一个新任务。

    它不是「复制原任务的执行快照」：新任务冻结的是方案此刻的范围，
    源任务的冻结快照与已发生的执行事实一概不带过来。
    """

    task_name: str = Field(min_length=1, max_length=255)
    # 复制同样是一次真实写操作：响应丢失时用同一幂等标识重发，返回原任务。
    idempotency_key: str = Field(min_length=1, max_length=120)
