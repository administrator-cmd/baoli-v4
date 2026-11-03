"""审核请求模型。"""


from pydantic import BaseModel, Field


class ExecuteNodeRequest(BaseModel):
    node_code: str
    # 幂等标识与请求内容绑定：同一节点重复提交才复用；换节点必须换标识。
    idempotency_key: str | None = Field(default=None, max_length=120)
    # 提交者看到的任务输入修订号；与任务当前修订不一致时明确冲突，不执行旧输入。
    input_revision: int | None = Field(default=None, ge=0)


class ResumeExecutionRequest(BaseModel):
    """从中断的执行批次继续；目标批次由服务端按任务当前节点解析。"""

    idempotency_key: str | None = Field(default=None, max_length=120)
    input_revision: int | None = Field(default=None, ge=0)
