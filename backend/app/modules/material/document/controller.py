"""材料管理列表与详情接口。"""

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from app.common.response import ResponseSchema, ok
from app.core.base_schema import PagedResponse
from app.core.database import get_db
from app.modules.material.document import service as material_documents
from app.modules.material.document.schema import DocumentDetailResponse
from app.schema.query.lists import DocumentListQueryParam
from app.schema.query.pagination import PaginationQueryParam

router = APIRouter(prefix="/documents", tags=["审核材料"])


@router.get("", response_model=ResponseSchema[PagedResponse])
def list_documents(
    request: Request,
    page: Annotated[PaginationQueryParam, Depends()],
    search: Annotated[DocumentListQueryParam, Query()],
    db: Session = Depends(get_db),
) -> ResponseSchema[PagedResponse]:
    """审核材料分页列表：以任务材料位为行，显示当前文件与处理状态。"""
    result = material_documents.MaterialDocumentService(db).list_documents_page(
        
        task_no=search.task_no,
        task_name=search.task_name,
        keyword=search.keyword,
        material_name=search.material_name,
        has_file=search.has_file,
        page=page.page,
        page_size=page.page_size,
    )
    return ok(PagedResponse.model_validate(result), request)


@router.get("/{document_no}", response_model=ResponseSchema[DocumentDetailResponse])
def get_document(document_no: str, request: Request, db: Session = Depends(get_db)) -> ResponseSchema[DocumentDetailResponse]:
    """材料位详情：当前文件与历史上传版本。"""
    return ok(DocumentDetailResponse.model_validate(material_documents.MaterialDocumentService(db).document_detail( document_no)), request)
