"""规则库导入 API。

- GET /import-jobs 仍是工作台使用的完整数组（兼容旧调用）；
  GET /import-jobs/page 提供真实分页、筛选与 total 的管理列表。
- DELETE 仅允许删除未发布、无活跃转换、未产生场景版本的草稿作业。
- POST /{job_no}/abort 中止未结束的后台转换操作，释放活跃占用；
  它不是删除，也不改写作业状态与草稿数据。
"""

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, File, Query, Request, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..database import get_db
from ..errors import BusinessError
from ..schemas import (
    ImportDecisionsRequest,
    ImportJobResponse,
    ImportJobSummaryResponse,
    OperationAcceptedResponse,
    OperationRequest,
    PagedResponse,
    PrecheckResponse,
    PublishRequest,
    ScenarioVersionResponse,
    SheetBindingsRequest,
)
from ..services import admin_query_service, import_service
from ..services.background_service import assert_no_active_operation, operation_view
from ..services.import_service import (
    create_import_job,
    get_import_job,
    import_job_summaries,
    import_job_view,
    precheck_import,
    publish_import,
    save_decisions,
    update_sheet_bindings,
)
from ..services.operation_gateway import OBJECT_IMPORT_JOB, submit_conversion
from .export_utils import EXPORT_PAGE_SIZE, csv_response


router = APIRouter(prefix="/import-jobs", tags=["规则库导入"])


class DecisionDraftRequest(BaseModel):
    """集中裁决草稿：按 issue_no 提交部分或全部作答，可反复保存后再提交最终裁决。"""

    answers: dict[str, Any] = Field(default_factory=dict)


def _page_params(page: int, page_size: int) -> tuple[int, int]:
    if page < 1:
        raise BusinessError("PAGE_INVALID", "页码必须是大于 0 的整数", status_code=400)
    if page_size < 1 or page_size > 200:
        raise BusinessError("PAGE_SIZE_INVALID", "每页条数必须在 1 到 200 之间", status_code=400)
    return page, page_size


@router.get("", response_model=list[ImportJobSummaryResponse])
def list_jobs(request: Request, db: Session = Depends(get_db)) -> list[ImportJobSummaryResponse]:
    return [
        ImportJobSummaryResponse.model_validate({**item, "request_id": request.state.request_id})
        for item in import_job_summaries(db)
    ]


@router.get("/page", response_model=PagedResponse)
def list_jobs_page(
    request: Request,
    filename: str | None = None,
    status: str | None = None,
    created_from: datetime | None = None,
    created_to: datetime | None = None,
    page: int = Query(default=1),
    page_size: int = Query(default=20),
    db: Session = Depends(get_db),
) -> PagedResponse:
    page, page_size = _page_params(page, page_size)
    result = admin_query_service.list_import_jobs_page(
        db,
        filename=filename,
        status=status,
        created_from=created_from,
        created_to=created_to,
        page=page,
        page_size=page_size,
    )
    return PagedResponse.model_validate({**result, "request_id": request.state.request_id})


@router.get("/export")
def export_jobs(
    filename: str | None = None,
    status: str | None = None,
    created_from: datetime | None = None,
    created_to: datetime | None = None,
    db: Session = Depends(get_db),
):
    result = admin_query_service.list_import_jobs_page(
        db,
        filename=filename,
        status=status,
        created_from=created_from,
        created_to=created_to,
        page=1,
        page_size=EXPORT_PAGE_SIZE,
    )
    rows = [
        [
            item["source_filename"],
            item["target_name"],
            item["target_mode"],
            item["status"],
            item["pending_issue_count"],
            item["created_at"],
        ]
        for item in result["items"]
    ]
    return csv_response("规则库导入列表.csv", ["文件名称", "目标场景", "新建/更新", "导入状态", "待处理数", "创建时间"], rows)


@router.delete("/{job_no}", status_code=204)
def delete_job(job_no: str, db: Session = Depends(get_db)) -> None:
    """删除导入作业（仅限未发布的草稿作业；已发布作业保留来源追溯）。"""
    admin_query_service.delete_import_job(db, job_no)


@router.post("/{job_no}/abort", response_model=OperationAcceptedResponse)
def abort_job(job_no: str, request: Request, db: Session = Depends(get_db)) -> OperationAcceptedResponse:
    """中止该作业尚未结束的后台转换操作。

    只把命令标为终态并释放活跃占用，让作业回到可重试 / 可删除的状态；
    运行中的执行线程不被强杀，作业状态与草稿数据一概不改。
    没有活跃操作时返回 409 OPERATION_NOT_ACTIVE，不静默成功。
    """
    operation = import_service.abort_import_job(db, job_no)
    return OperationAcceptedResponse.model_validate(
        {**operation_view(operation, reused=False), "request_id": request.state.request_id}
    )


@router.post("", response_model=ImportJobResponse, status_code=201)
async def create_job(request: Request, file: UploadFile = File(...), db: Session = Depends(get_db)) -> ImportJobResponse:
    job = await create_import_job(db, file)
    return ImportJobResponse.model_validate({**import_job_view(db, job), "request_id": request.state.request_id})


@router.get("/{job_no}", response_model=ImportJobResponse)
def get_job(job_no: str, request: Request, db: Session = Depends(get_db)) -> ImportJobResponse:
    return ImportJobResponse.model_validate({**import_job_view(db, get_import_job(db, job_no)), "request_id": request.state.request_id})


@router.put("/{job_no}/sheet-bindings", response_model=ImportJobResponse)
def put_sheet_bindings(job_no: str, payload: SheetBindingsRequest, request: Request, db: Session = Depends(get_db)) -> ImportJobResponse:
    # 先锁再查：先取作业行锁，再检查活跃操作，最后才提交修改，消除「先查后锁」的 TOCTOU。
    # update_sheet_bindings 内部尚未接管该检查，故在此保持锁后检查；服务层补上后这里可移除。
    import_service._job_or_404(db, job_no, lock=True)
    assert_no_active_operation(db, OBJECT_IMPORT_JOB, job_no)
    job = update_sheet_bindings(db, job_no, payload)
    return ImportJobResponse.model_validate({**import_job_view(db, job), "request_id": request.state.request_id})


@router.post("/{job_no}/retry", response_model=OperationAcceptedResponse, status_code=202)
def retry_job(
    job_no: str,
    request: Request,
    payload: OperationRequest | None = None,
    db: Session = Depends(get_db),
) -> OperationAcceptedResponse:
    """提交规则转换为后台操作。

    返回 202 只代表操作已受理，不代表转换成功；真实状态用 operation_no 查询
    GET /api/operations/{operation_no}，或刷新作业详情查看。
    """
    job = import_service._job_or_404(db, job_no, lock=True)
    operation, reused = submit_conversion(
        db,
        job,
        idempotency_key=payload.idempotency_key if payload else None,
    )
    return OperationAcceptedResponse.model_validate(
        {**operation_view(operation, reused=reused), "request_id": request.state.request_id}
    )


@router.put("/{job_no}/decisions", response_model=ImportJobResponse)
def put_decisions(job_no: str, payload: ImportDecisionsRequest, request: Request, db: Session = Depends(get_db)) -> ImportJobResponse:
    # 先锁再查：先取作业行锁，再检查活跃操作，最后才提交裁决，消除「先查后锁」的 TOCTOU。
    # save_decisions 内部尚未接管该检查，故在此保持锁后检查；服务层补上后这里可移除。
    import_service._job_or_404(db, job_no, lock=True)
    assert_no_active_operation(db, OBJECT_IMPORT_JOB, job_no)
    job = save_decisions(db, job_no, payload)
    return ImportJobResponse.model_validate({**import_job_view(db, job), "request_id": request.state.request_id})


@router.put("/{job_no}/decision-draft", response_model=ImportJobResponse)
def put_decision_draft(
    job_no: str,
    payload: DecisionDraftRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> ImportJobResponse:
    """保存集中裁决草稿：只落库当前作答，不触发图恢复，可反复保存。"""
    # 先锁再查由服务函数负责；该入口与后台转换同样互斥。
    job = import_service.save_decision_draft(db, job_no, payload.answers)
    return ImportJobResponse.model_validate({**import_job_view(db, job), "request_id": request.state.request_id})


@router.post("/{job_no}/precheck", response_model=PrecheckResponse)
def precheck(job_no: str, payload: PublishRequest, request: Request, db: Session = Depends(get_db)) -> PrecheckResponse:
    _, result = precheck_import(db, job_no, payload)
    return PrecheckResponse.model_validate({**result, "request_id": request.state.request_id})


@router.post("/{job_no}/publish", response_model=ScenarioVersionResponse, status_code=201)
def publish(job_no: str, payload: PublishRequest, request: Request, db: Session = Depends(get_db)) -> ScenarioVersionResponse:
    version = publish_import(db, job_no, payload)
    return ScenarioVersionResponse(
        request_id=request.state.request_id,
        scenario_code=version.scenario_code,
        scenario_name=version.scenario_name,
        version_no=version.version_no,
        status=version.status,
        manifest=version.manifest_json,
        published_at=version.published_at,
    )
