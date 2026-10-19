"""证据管理列表接口。"""

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session
from ...database import get_db
from ...schema.query import validate_page_params
from ...services.material import evidence as material_evidence
from ...schema.vo import PagedResponse


evidence_router = APIRouter(prefix="/evidence", tags=["证据记录"])


@evidence_router.get("", response_model=PagedResponse)
def list_evidence(
    request: Request,
    task_no: str | None = None,
    reference_type: str | None = None,
    keyword: str | None = None,
    page: int = Query(default=1),
    page_size: int = Query(default=20),
    db: Session = Depends(get_db),
) -> PagedResponse:
    """证据记录分页列表（跨任务）：所属任务、文件、原文与依据类型。"""
    page, page_size = validate_page_params(page, page_size)
    result = material_evidence.list_evidence_page(
        db,
        task_no=task_no,
        reference_type=reference_type,
        keyword=keyword,
        page=page,
        page_size=page_size,
    )
    return PagedResponse.model_validate({**result, "request_id": request.state.request_id})
