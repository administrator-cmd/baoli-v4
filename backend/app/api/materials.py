"""审核材料与证据记录 API（跨任务的管理列表）。

- 材料列表以任务材料位为行：显示当前文件、版本号与处理状态；
  历史上传版本在详情里按版本列出。
- 上传、替换、移除是任务级写操作（/audit-tasks/...），本模块只做查询，
  保证“上传必须指定任务和材料位”，不建设游离文件仓库。
- 证据列表显示证据所属任务/文件/对象/引用原文及依据类型；
  读取失败与无证据分开，历史证据按历史身份查看。
"""

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from ..database import get_db
from ..schema.query import validate_page_params
from ..services import admin_query_service
from ..schema.vo import PagedResponse

router = APIRouter(prefix="/documents", tags=["审核材料"])
evidence_router = APIRouter(prefix="/evidence", tags=["证据记录"])


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
    result = admin_query_service.list_documents_page(
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
    return {**admin_query_service.document_detail(db, document_no), "request_id": request.state.request_id}


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
    result = admin_query_service.list_evidence_page(
        db,
        task_no=task_no,
        reference_type=reference_type,
        keyword=keyword,
        page=page,
        page_size=page_size,
    )
    return PagedResponse.model_validate({**result, "request_id": request.state.request_id})
