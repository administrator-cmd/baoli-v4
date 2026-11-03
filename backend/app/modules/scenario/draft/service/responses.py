"""场景草稿写操作的响应组装。"""

from fastapi import Request
from sqlalchemy.orm import Session

from app.common.response import ResponseSchema, ok
from app.modules.scenario.draft.schema import ScenarioDraftViewResponse


class ScenarioDraftResponsesService:
    """本实体的服务方法；db 由构造时注入。"""

    def __init__(self, db: Session | None = None) -> None:
        self.db = db

    def draft_response(self, request: Request, payload: dict) -> ResponseSchema[ScenarioDraftViewResponse]:
        return ok(ScenarioDraftViewResponse.model_validate(payload), request)
