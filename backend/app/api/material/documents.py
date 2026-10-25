"""材料管理列表与详情接口。"""

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from ...core.database import get_db
from ...schema.query.pagination import validate_page_params
from ...schema.vo.common import PagedResponse
from ...services.material import documents as material_documents

router = APIRouter(prefix="/documents", tags=["审核材料"])


@router.get("", response_model=PagedResponse)
def list_documents(
    request: Request,
    task_no: str | None = None,
    keyword: str | None = None,
    material_name: str | None = None,
    has_file: bool | None = None,
    page: int = Query(default=1),
    page_size: int = Query(default=20),
    db: Session = Depends(get_db),
) -> PagedResponse:
    """审核材料分页列表：以任务材料位为行，显示当前文件与处理状态。"""
    page, page_size = validate_page_params(page, page_size)
    result = material_documents.list_documents_page(
        db,
        task_no=task_no,
        keyword=keyword,
        material_name=material_name,
        has_file=has_file,
        page=page,
        page_size=page_size,
    )
    return PagedResponse.model_validate({**result, "request_id": request.state.request_id})


@router.get("/{document_no}")
def get_document(document_no: str, request: Request, db: Session = Depends(get_db)) -> dict:
    """材料位详情：当前文件与历史上传版本。"""
    return {**material_documents.document_detail(db, document_no), "request_id": request.state.request_id}
