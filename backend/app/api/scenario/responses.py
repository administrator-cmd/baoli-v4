"""场景草稿写操作的响应组装。"""

from fastapi import Request

from ...schema.vo.scenarios import ScenarioDraftViewResponse


def _draft_response(request: Request, payload: dict) -> ScenarioDraftViewResponse:
    return ScenarioDraftViewResponse.model_validate({**payload, "request_id": request.state.request_id})
