"""规则库导入 API。

- GET /import-jobs 提供工作台作业选择所需的完整摘要数组；
  GET /import-jobs/page 提供真实分页、筛选与 total 的管理列表。
- DELETE 删除验证项目中的导入作业；已发布版本解除来源引用后保留。
- POST /{job_no}/abort 中止未结束的后台转换操作，释放活跃占用；
  它不是删除，也不改写作业状态与草稿数据。"""


from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.common.exceptions import BusinessError
from app.common.response import ResponseSchema, ok
from app.core.base_schema import PagedResponse
from app.core.database import SessionLocal, get_db
from app.modules.imports.job.dto import (
    ApplyImportDraftRequest,
    ImportConversionRequest,
    ImportProposalGroup,
    UpdateImportProposalRequest,
)
from app.modules.imports.job.schema import ImportJobResponse, ImportJobSummaryResponse
from app.modules.imports.job.service import (
    lifecycle as imports_lifecycle,
)
from app.modules.imports.job.service import (
    lookup as imports_lookup,
)
from app.modules.imports.job.service import (
    proposals as imports_proposals,
)
from app.modules.imports.job.service import (
    query as imports_query,
)
from app.modules.imports.job.service.draft import ImportsJobDraftService
from app.modules.imports.job.service.operations import ImportsJobOperationsService
from app.modules.imports.job.service.query import ImportsJobQueryService
from app.modules.imports.job.service.upload import ImportsJobUploadService
from app.modules.system.operation.schema import OperationAcceptedResponse
from app.modules.system.operation.service.background import OperationBackgroundService
from app.schema.query.lists import ImportJobListQueryParam
from app.schema.query.pagination import PaginationQueryParam
from app.services.shared.storage import resolve_storage_path

router = APIRouter(prefix="/import-jobs", tags=["规则库导入"])


@router.get("", response_model=ResponseSchema[list[ImportJobSummaryResponse]])
def list_jobs(request: Request, db: Session = Depends(get_db)) -> ResponseSchema[list[ImportJobSummaryResponse]]:
    """工作台作业选择列表（完整摘要数组）；分页管理列表见 GET /import-jobs/page。"""
    return ok(
        [ImportJobSummaryResponse.model_validate(item) for item in ImportsJobQueryService(db).import_job_summaries()],
        request,
    )


@router.get("/page", response_model=ResponseSchema[PagedResponse])
def list_jobs_page(
    request: Request,
    page: Annotated[PaginationQueryParam, Depends()],
    search: Annotated[ImportJobListQueryParam, Query()],
    db: Session = Depends(get_db),
) -> ResponseSchema[PagedResponse]:
    """导入作业分页列表：可限定所属场景，total 与列表使用同一筛选范围。"""
    result = imports_query.ImportsJobQueryService(db).list_import_jobs_page(
        
        scenario_code=search.scenario_code,
        filename=search.filename,
        status=search.status,
        created_from=search.created_from,
        created_to=search.created_to,
        page=page.page,
        page_size=page.page_size,
    )
    return ok(PagedResponse.model_validate(result), request)


@router.delete("/{job_no}", status_code=204)
def delete_job(job_no: str, db: Session = Depends(get_db)) -> None:
    """删除验证项目中的导入作业；已发布版本解除来源引用后保留版本。"""
    imports_lifecycle.ImportsJobLifecycleService(db).delete_import_job( job_no)


@router.post("/{job_no}/abort", response_model=ResponseSchema[OperationAcceptedResponse])
def abort_job(job_no: str, request: Request, db: Session = Depends(get_db)) -> ResponseSchema[OperationAcceptedResponse]:
    """中止该作业尚未结束的后台转换操作。

    只把命令标为终态并释放活跃占用，让作业回到可重试 / 可删除的状态；
    运行中的执行线程不被强杀，作业状态与草稿数据一概不改。
    没有活跃操作时返回 409 OPERATION_NOT_ACTIVE，不静默成功。
    """
    operation = imports_lifecycle.ImportsJobLifecycleService(db).abort_import_job( job_no)
    return ok(OperationAcceptedResponse.model_validate(OperationBackgroundService().operation_view(operation, reused=False)), request)


@router.post("", response_model=ResponseSchema[ImportJobResponse], status_code=201)
def create_job(
    request: Request,
    file: UploadFile = File(...),
    scenario_code: str = Form(..., min_length=2, max_length=80, pattern=r"^[A-Za-z0-9_-]+$"),
) -> ResponseSchema[ImportJobResponse]:
    """在指定场景下上传规则库 Excel，读取为工作簿 IR 并返回新建作业。

    `scenario_code` 必填：上传流程以场景为起点（用户先建场景、再在该场景下上传），
    场景不由 Excel 表头推断。此时还没有规则配置，解析时使用全部非空工作表。
    """
    # 同步路由由 FastAPI 放入工作线程，Session 与文件解析在同一线程内完成。
    with SessionLocal() as db:
        job = ImportsJobUploadService(db).create_import_job( file, scenario_code=scenario_code)
        return ok(ImportJobResponse.model_validate(ImportsJobQueryService(db).import_job_view( job)), request)


# 规则库文件只接受 Excel；按扩展名给出真实的下载类型，未知扩展名按二进制流返回。
_RULEBOOK_MEDIA_TYPES = {
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xlsm": "application/vnd.ms-excel.sheet.macroEnabled.12",
    ".xls": "application/vnd.ms-excel",
}


@router.get("/{job_no}/file")
def download_job_file(job_no: str, db: Session = Depends(get_db)) -> FileResponse:
    """下载该导入作业当时上传的原始规则库 Excel。

    返回的是磁盘上保存的原件，不从候选配置重新生成；文件缺失时明确 404，不返回空文件。
    """
    job = ImportsJobQueryService(db).get_import_job( job_no)
    path = resolve_storage_path(job.source_path)
    if not path.is_file():
        raise BusinessError(
            "IMPORT_SOURCE_FILE_MISSING", "该作业的规则库原文件已不在存储目录中，无法下载",
            status_code=404, details={"job_no": job_no},
        )
    return FileResponse(
        path,
        media_type=_RULEBOOK_MEDIA_TYPES.get(path.suffix.lower(), "application/octet-stream"),
        filename=job.source_filename,
    )


@router.get("/{job_no}", response_model=ResponseSchema[ImportJobResponse])
def get_job(job_no: str, request: Request, db: Session = Depends(get_db)) -> ResponseSchema[ImportJobResponse]:
    """导入作业详情：二轮整理结果与修订号。"""
    return ok(ImportJobResponse.model_validate(ImportsJobQueryService(db).import_job_view( ImportsJobQueryService(db).get_import_job( job_no))), request)


@router.post("/{job_no}/retry", response_model=ResponseSchema[OperationAcceptedResponse], status_code=202)
def retry_job(
    job_no: str,
    request: Request,
    payload: ImportConversionRequest | None = None,
    db: Session = Depends(get_db),
) -> ResponseSchema[OperationAcceptedResponse]:
    """提交规则转换为后台操作。

    返回 202 只代表操作已受理，不代表转换成功；真实状态用 operation_no 查询
    GET /api/operations/{operation_no}，或刷新作业详情查看。
    `mode=RESUME_REFINEMENT` 只重跑第二轮，需存在可复用的第一轮结果。
    """
    job = imports_lookup.ImportsJobLookupService(db)._job_or_404( job_no, lock=True)
    operation, reused = ImportsJobOperationsService(db).submit_conversion(
        
        job,
        idempotency_key=payload.idempotency_key if payload else None,
        mode=payload.mode if payload else "FULL",
    )
    return ok(OperationAcceptedResponse.model_validate(OperationBackgroundService().operation_view(operation, reused=reused)), request)


@router.post("/{job_no}/apply-draft", response_model=ResponseSchema[ImportJobResponse])
def apply_draft(
    job_no: str, payload: ApplyImportDraftRequest, request: Request, db: Session = Depends(get_db)
) -> ResponseSchema[ImportJobResponse]:
    """把已确认的导入结果写入场景草稿；发布必须在场景页显式执行。"""
    job = ImportsJobDraftService(db).apply_import_to_draft(
         job_no,
        expected_job_revision=payload.expected_job_revision,
        expected_scene_revision=payload.expected_scene_revision,
    )
    return ok(ImportJobResponse.model_validate(ImportsJobQueryService(db).import_job_view( job)), request)


@router.patch("/{job_no}/proposals/{group}/{candidate_ref}", response_model=ResponseSchema[ImportJobResponse])
def update_proposal(
    job_no: str, group: ImportProposalGroup, candidate_ref: str,
    payload: UpdateImportProposalRequest, request: Request, db: Session = Depends(get_db),
) -> ResponseSchema[ImportJobResponse]:
    """修改解析结果中的单项配置，供后续写入场景草稿。"""
    job = imports_proposals.ImportsJobProposalsService(db).update_import_proposal(
         job_no, group=group, candidate_ref=candidate_ref,
        expected_job_revision=payload.expected_job_revision,
        changes=payload.changes.model_dump(exclude_unset=True),
    )
    return ok(ImportJobResponse.model_validate(ImportsJobQueryService(db).import_job_view( job)), request)
