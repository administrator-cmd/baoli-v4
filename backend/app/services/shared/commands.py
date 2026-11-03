"""业务命令共用的幂等标识与恢复状态处理。"""

from app.common.exceptions import BusinessError
from app.modules.system.operation.service import background as background_service


def _idempotency_key(provided: str | None) -> str:
    """未提供幂等标识时生成一次性标识。

    这样同一对象的并发提交由活跃操作互斥拦下（明确 409），而不是被误当成同一请求复用。
    调用方提供标识时（前端每次点击生成一个），重复请求才会返回已有操作。
    """
    from app.domain.identity import public_no

    return (provided or "").strip() or public_no("IDEM")


def _reject_unsupported_resume(command: background_service.CommandHandle) -> None:
    """命令已提交本地业务但图未推进时，只能补齐图推进，绝不能重做业务写入。

    审核服务的业务写入与图推进目前在同一函数内完成，无法只补齐图推进；
    因此明确拒绝，而不是重放一次可能重复生效的业务写入。
    """
    if command.resume_committed:
        raise BusinessError(
            "OPERATION_RESUME_UNSUPPORTED",
            "该命令的本地业务结果已提交但图未推进，无法在不重做业务写入的前提下补齐，请重新发起",
            status_code=409,
        )
