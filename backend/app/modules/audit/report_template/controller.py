"""报告模板管理接口。"""

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.common.exceptions import BusinessError
from app.common.response import ResponseSchema, ok
from app.core.database import get_db
from app.modules.audit.report_template.crud import ReportTemplateCRUD
from app.modules.audit.report_template.dto import ReportTemplateAnalyzeRequest, ReportTemplateUpdateRequest
from app.modules.audit.report_template.schema import (
    ReportTemplateListResponse,
    ReportTemplateOptionsResponse,
    ReportTemplateResponse,
    ReportTemplateUploadAcceptedResponse,
)
from app.modules.audit.report_template.service.analysis import validate_style_profile
from app.modules.audit.report_template.service.lifecycle import ReportTemplateLifecycleService
from app.modules.system.operation.schema import OperationAcceptedResponse
from app.modules.system.operation.service.background import OperationBackgroundService
from app.modules.system.operation.service.operation_types import OBJECT_REPORT_TEMPLATE
from app.services.shared.storage import resolve_storage_path

router = APIRouter(prefix="/report-templates", tags=["报告模板"])


def _view(template, latest_operation=None, *, operation_reused: bool = False):
    profile = None
    status = template.status
    analysis_error = template.analysis_error
    if template.style_profile_json is not None or status == "READY":
        try:
            profile = validate_style_profile(template.style_profile_json)
        except BusinessError as exc:
            # 单行异常只影响该模板的可用性；保留原始档案供重新分析，不写回数据库。
            analysis_error = f"{analysis_error}；{exc.message}" if analysis_error else exc.message
            if status == "READY":
                status = "FAILED"
    return {
        "template_no": template.template_no,
        "name": template.name,
        "source_filename": template.source_filename,
        "source_size": template.source_size,
        "page_count": template.page_count,
        "status": status,
        "style_profile": profile,
        "analysis_error": analysis_error,
        "latest_operation": (
            OperationBackgroundService().operation_view(latest_operation, reused=operation_reused)
            if latest_operation is not None else None
        ),
        "created_at": template.created_at,
        "updated_at": template.updated_at,
    }


@router.get("", response_model=ResponseSchema[ReportTemplateListResponse])
def list_templates(request: Request, db: Session = Depends(get_db)):
    templates = ReportTemplateCRUD(db).list_all()
    operations = ReportTemplateCRUD(db).latest_operations([item.template_no for item in templates])
    latest_by_template = {}
    for operation in operations:
        current = latest_by_template.get(operation.object_no)
        if current is None or operation.id > current.id:
            latest_by_template[operation.object_no] = operation
    items = [_view(item, latest_by_template.get(item.template_no)) for item in templates]
    return ok(ReportTemplateListResponse(items=items), request)


@router.get("/options", response_model=ResponseSchema[ReportTemplateOptionsResponse])
def template_options(request: Request, db: Session = Depends(get_db)):
    items = []
    for template in ReportTemplateCRUD(db).list_all():
        view = _view(template)
        if view["status"] == "READY" and view["style_profile"] is not None:
            items.append({"template_no": template.template_no, "name": template.name})
    return ok(ReportTemplateOptionsResponse(items=items), request)


@router.post("", response_model=ResponseSchema[ReportTemplateUploadAcceptedResponse], status_code=202)
def upload_template(
    request: Request,
    file: UploadFile = File(...),
    name: str | None = Form(default=None, max_length=255),
    idempotency_key: str = Form(..., min_length=1, max_length=120),
    db: Session = Depends(get_db),
):
    template, operation, reused = ReportTemplateLifecycleService(db).upload(
        file, name=name, idempotency_key=idempotency_key,
    )
    operation_view = OperationAcceptedResponse.model_validate(
        OperationBackgroundService().operation_view(operation, reused=reused),
    )
    template_view = ReportTemplateResponse.model_validate(_view(template, operation, operation_reused=reused))
    return ok(ReportTemplateUploadAcceptedResponse(template=template_view, operation=operation_view), request)


@router.get("/{template_no}", response_model=ResponseSchema[ReportTemplateResponse])
def get_template(template_no: str, request: Request, db: Session = Depends(get_db)):
    template = ReportTemplateLifecycleService(db).template_or_404(template_no)
    latest_operation = OperationBackgroundService(db).latest_operation(OBJECT_REPORT_TEMPLATE, template_no)
    return ok(ReportTemplateResponse.model_validate(_view(template, latest_operation)), request)


@router.get("/{template_no}/pdf")
def preview_template_pdf(template_no: str, db: Session = Depends(get_db)) -> FileResponse:
    """以内联 PDF 响应返回已保存的原件，供浏览器预览。"""
    template = ReportTemplateLifecycleService(db).template_or_404(template_no)
    path = resolve_storage_path(template.source_path)
    if not path.is_file():
        raise BusinessError(
            "REPORT_TEMPLATE_SOURCE_MISSING", "报告模板 PDF 原件已不在存储目录中，无法预览",
            status_code=404, details={"template_no": template_no},
        )
    return FileResponse(
        path,
        media_type="application/pdf",
        filename=template.source_filename,
        content_disposition_type="inline",
    )


@router.put("/{template_no}", response_model=ResponseSchema[ReportTemplateResponse])
def update_template(
    template_no: str, payload: ReportTemplateUpdateRequest, request: Request, db: Session = Depends(get_db),
):
    template = ReportTemplateLifecycleService(db).update(
        template_no, name=payload.name, style_profile=payload.style_profile,
    )
    latest_operation = OperationBackgroundService(db).latest_operation(OBJECT_REPORT_TEMPLATE, template_no)
    return ok(ReportTemplateResponse.model_validate(_view(template, latest_operation)), request)


@router.post("/{template_no}/reanalyze", response_model=ResponseSchema[OperationAcceptedResponse], status_code=202)
def reanalyze_template(
    template_no: str, payload: ReportTemplateAnalyzeRequest, request: Request, db: Session = Depends(get_db),
):
    _, operation, reused = ReportTemplateLifecycleService(db).reanalyze(
        template_no, idempotency_key=payload.idempotency_key,
    )
    return ok(OperationAcceptedResponse.model_validate(
        OperationBackgroundService().operation_view(operation, reused=reused),
    ), request)
