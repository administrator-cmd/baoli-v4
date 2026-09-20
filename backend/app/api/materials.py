"""审核材料与证据记录 API（跨任务的管理列表）。

- 材料列表以任务材料位为行：显示当前文件、版本号与处理状态；
  历史上传版本在详情里按版本列出并可直接下载/预览。
- 上传、替换、移除是任务级写操作（/audit-tasks/...），本模块只做查询与文件读取，
  保证“上传必须指定任务和材料位”，不建设游离文件仓库。
- 证据列表显示证据所属任务/文件/对象/页码/依据类型与定位核对状态；
  读取失败与无证据分开，历史证据按历史身份查看。
"""

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from ..database import get_db
from ..errors import BusinessError
from ..schemas import PagedResponse
from ..services import admin_query_service
from .export_utils import EXPORT_PAGE_SIZE, csv_response

router = APIRouter(prefix="/documents", tags=["审核材料"])
evidence_router = APIRouter(prefix="/evidence", tags=["证据记录"])


def _page_params(page: int, page_size: int) -> tuple[int, int]:
    if page < 1:
        raise BusinessError("PAGE_INVALID", "页码必须是大于 0 的整数", status_code=400)
    if page_size < 1 or page_size > 200:
        raise BusinessError("PAGE_SIZE_INVALID", "每页条数必须在 1 到 200 之间", status_code=400)
    return page, page_size


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
    page, page_size = _page_params(page, page_size)
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


@router.get("/export")
def export_documents(
    task_no: str | None = None,
    keyword: str | None = None,
    material_name: str | None = None,
    has_file: bool | None = None,
    db: Session = Depends(get_db),
):
    result = admin_query_service.list_documents_page(
        db,
        task_no=task_no,
        keyword=keyword,
        material_name=material_name,
        has_file=has_file,
        page=1,
        page_size=EXPORT_PAGE_SIZE,
    )
    rows = [
        [
            item["task_name"],
            item["material_name"],
            item["filename"],
            item["version_no"],
            item["size_bytes"],
            item["uploaded_at"],
            item["status"],
        ]
        for item in result["items"]
    ]
    return csv_response(
        "审核材料列表.csv",
        ["任务", "材料位", "文件名", "文件版本", "大小（字节）", "上传时间", "处理状态"],
        rows,
    )


@router.get("/{document_no}")
def get_document(document_no: str, request: Request, db: Session = Depends(get_db)) -> dict:
    return {**admin_query_service.document_detail(db, document_no), "request_id": request.state.request_id}


@router.get("/{document_no}/files/{upload_no}/download")
def download_upload_file(
    document_no: str,
    upload_no: str,
    preview: bool = False,
    db: Session = Depends(get_db),
) -> FileResponse:
    """下载/预览一个上传版本（含历史版本）；文件缺失时明确报错。"""
    return admin_query_service.upload_file_response(db, document_no, upload_no, preview=preview)


@evidence_router.get("", response_model=PagedResponse)
def list_evidence(
    request: Request,
    task_no: str | None = None,
    reference_type: str | None = None,
    locator_status: str | None = None,
    keyword: str | None = None,
    page: int = Query(default=1),
    page_size: int = Query(default=20),
    db: Session = Depends(get_db),
) -> PagedResponse:
    page, page_size = _page_params(page, page_size)
    result = admin_query_service.list_evidence_page(
        db,
        task_no=task_no,
        reference_type=reference_type,
        locator_status=locator_status,
        keyword=keyword,
        page=page,
        page_size=page_size,
    )
    return PagedResponse.model_validate({**result, "request_id": request.state.request_id})


@evidence_router.get("/export")
def export_evidence(
    task_no: str | None = None,
    reference_type: str | None = None,
    locator_status: str | None = None,
    keyword: str | None = None,
    db: Session = Depends(get_db),
):
    result = admin_query_service.list_evidence_page(
        db,
        task_no=task_no,
        reference_type=reference_type,
        locator_status=locator_status,
        keyword=keyword,
        page=1,
        page_size=EXPORT_PAGE_SIZE,
    )
    rows = [
        [
            item["evidence_no"],
            item["task_name"],
            item["filename"],
            item["object_name"],
            item["page_no"],
            item["reference_type"],
            item["locator_status"],
            item["quote_text"],
            item["historical"],
        ]
        for item in result["items"]
    ]
    return csv_response(
        "证据记录列表.csv",
        ["证据编号", "任务", "来源文件", "关联对象", "页码", "依据类型", "定位核对", "引用原文", "历史版本"],
        rows,
    )
