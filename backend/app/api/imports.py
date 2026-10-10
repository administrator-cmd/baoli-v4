"""规则库导入 API。

- GET /import-jobs 提供工作台作业选择所需的完整摘要数组；
  GET /import-jobs/page 提供真实分页、筛选与 total 的管理列表。
- DELETE 仅允许删除未发布、无活跃转换、未产生场景版本的草稿作业。
- POST /{job_no}/abort 中止未结束的后台转换操作，释放活跃占用；
  它不是删除，也不改写作业状态与草稿数据。
"""

from datetime import datetime
from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from sqlalchemy.orm import Session

from ..database import get_db
from ..schema.dto import (
    ApplyImportDraftRequest,
    DecisionDraftRequest,
    ImportDecisionsRequest,
    OperationRequest,
    PublishRequest,
    SheetBindingsRequest,
)
from ..schema.query import validate_page_params
from ..schema.vo import OperationAcceptedResponse
from ..schema.vo.scenarios import ScenarioVersionResponse
from ..services import admin_query_service, import_service
from ..services.background_service import operation_view
from ..services.import_service import (
    create_import_job,
    get_import_job,
    import_job_summaries,
    import_job_view,
    apply_import_to_draft,
    publish_import,
    save_decisions,
    update_sheet_bindings,
)
from ..services.operation_gateway import submit_conversion
from ..schema.vo import ImportJobResponse, ImportJobSummaryResponse, PagedResponse
from .export_utils import EXPORT_PAGE_SIZE, csv_response


router = APIRouter(prefix="/import-jobs", tags=["规则库导入"])


@router.get("", response_model=list[ImportJobSummaryResponse])
def list_jobs(request: Request, db: Session = Depends(get_db)) -> list[ImportJobSummaryResponse]:
    """工作台作业选择列表（完整摘要数组）；分页管理列表见 GET /import-jobs/page。"""
    return [
        ImportJobSummaryResponse.model_validate({**item, "request_id": request.state.request_id})
        for item in import_job_summaries(db)
    ]


@router.get("/page", response_model=PagedResponse)
def list_jobs_page(
    request: Request,
    scenario_code: str | None = None,
    filename: str | None = None,
    status: str | None = None,
    created_from: datetime | None = None,
    created_to: datetime | None = None,
    page: int = Query(default=1),
    page_size: int = Query(default=20),
    db: Session = Depends(get_db),
) -> PagedResponse:
    """导入作业分页列表：可限定所属场景，total 与列表使用同一筛选范围。"""
    page, page_size = validate_page_params(page, page_size)
    result = admin_query_service.list_import_jobs_page(
        db,
        scenario_code=scenario_code,
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
    scenario_code: str | None = None,
    filename: str | None = None,
    status: str | None = None,
    created_from: datetime | None = None,
    created_to: datetime | None = None,
    db: Session = Depends(get_db),
):
    """按当前筛选导出导入作业列表 CSV。"""
    result = admin_query_service.list_import_jobs_page(
        db,
        scenario_code=scenario_code,
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
            item["scenario_name"] or item["scenario_code"] or "",
            item["status"],
            item["pending_issue_count"],
            item["created_at"],
        ]
        for item in result["items"]
    ]
    return csv_response("规则库导入列表.csv", ["文件名称", "所属场景", "导入状态", "待处理数", "创建时间"], rows)


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
async def create_job(
    request: Request,
    file: UploadFile = File(...),
    scenario_code: str = Form(..., min_length=2, max_length=80, pattern=r"^[A-Za-z0-9_-]+$"),
    db: Session = Depends(get_db),
) -> ImportJobResponse:
    """在指定场景下上传规则库 Excel，读取为工作簿 IR 并返回新建作业。

    `scenario_code` 必填：上传流程以场景为起点（用户先建场景、再在该场景下上传），
    场景不由 Excel 表头推断。此时还没有规则候选；管理员选择参与解析的工作表，
    LLM 将表内字段、规则和计算分别归到当前场景的实际节点。
    """
    job = await create_import_job(db, file, scenario_code=scenario_code)
    return ImportJobResponse.model_validate({**import_job_view(db, job), "request_id": request.state.request_id})


@router.get("/{job_no}", response_model=ImportJobResponse)
def get_job(job_no: str, request: Request, db: Session = Depends(get_db)) -> ImportJobResponse:
    """导入作业详情：工作表、问题、候选与草稿修订号。"""
    return ImportJobResponse.model_validate({**import_job_view(db, get_import_job(db, job_no)), "request_id": request.state.request_id})


@router.put("/{job_no}/sheet-bindings", response_model=ImportJobResponse)
def put_sheet_bindings(job_no: str, payload: SheetBindingsRequest, request: Request, db: Session = Depends(get_db)) -> ImportJobResponse:
    """保存参与解析的工作表；与后台转换互斥（先锁作业行再检查活跃操作）。"""
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
    """提交裁决：必须一次处理全部待裁决问题，并带页面看到的草稿修订号，过期即 409。"""
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


@router.post("/{job_no}/apply-draft", response_model=ImportJobResponse)
def apply_draft(
    job_no: str, payload: ApplyImportDraftRequest, request: Request, db: Session = Depends(get_db)
) -> ImportJobResponse:
    """把已确认的导入结果写入场景草稿；发布必须在场景页显式执行。"""
    job = apply_import_to_draft(
        db, job_no,
        expected_job_revision=payload.expected_job_revision,
        expected_scene_revision=payload.expected_scene_revision,
    )
    return ImportJobResponse.model_validate({**import_job_view(db, job), "request_id": request.state.request_id})


@router.post("/{job_no}/publish", response_model=ScenarioVersionResponse, status_code=201)
def publish(job_no: str, payload: PublishRequest, request: Request, db: Session = Depends(get_db)) -> ScenarioVersionResponse:
    """历史作业若已有场景版本则返回该版本。新的导入不能从这里发布场景。"""
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
