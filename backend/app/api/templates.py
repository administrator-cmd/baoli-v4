from io import BytesIO
from urllib.parse import quote

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from ..database import get_db
from ..services import template_application_service


router = APIRouter(prefix="/rulebook-templates", tags=["规则库模板"])


@router.get("")
def template_catalog(request: Request) -> dict:
    """**唯一一份**规则库模板的目录：系统支持的全部 Sheet、节点类型与列协议。

    系统使用 `backend/config/unified_profile.json` 声明唯一的模板协议和下载入口。

    `sheets` 是协议声明的全部 Sheet，也是 base Excel 的完整表清单。
    场景已有确认绑定时，下载只含这些角色；还没有绑定时，必须由调用方传入本次要下载的
    `sheet_roles`，该选择不写入场景。
    """
    return {**template_application_service.template_catalog(), "request_id": request.state.request_id}


@router.get("/download")
def download_template(
    db: Session = Depends(get_db),
    scenario_code: str = Query(
        ...,
        min_length=1,
        description="场景编码。已有确认绑定时只导出这些 Sheet；没有绑定时必须另传 sheet_roles",
    ),
    sheet_roles: list[str] = Query(
        default_factory=list,
        description="仅本次下载的 Sheet 角色。场景已有确认绑定时忽略本参数，且不会写入场景",
    ),
) -> StreamingResponse:
    """下载规则库模板。

    已确认绑定（scenario_draft_sheet）是下载范围的真源，查询参数里的勾选一律忽略。
    还没有确认绑定时，必须由调用方逐项传入 `sheet_roles`。该列表只生成这一份文件，
    不写库、不要求节点、也不默认包含协议全集。

    一次下载就是**一份 base Excel**：同一份模板里的表可以一起下载，导入时按 Sheet
    独立转换，不存在「只能属于一个业务族」的限制。
    """
    download = template_application_service.create_download(db, scenario_code, sheet_roles)
    headers = {"Content-Disposition": f"attachment; filename*=UTF-8''{quote(download.filename)}"}
    return StreamingResponse(
        BytesIO(download.content),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers=headers,
    )


@router.get("/sheets")
def list_sheets(
    request: Request,
    scenario_code: str | None = Query(default=None, min_length=2, max_length=80),
    db: Session = Depends(get_db),
) -> dict:
    """统一模板协议声明的 Sheet 与列协议。

    页面据此渲染「Sheet 绑定」步骤：每张 Sheet 列出它可以绑定的节点类型与列定义。
    只返回声明，不含任何示例数据。
    """
    return {**template_application_service.sheet_catalog(db, scenario_code), "request_id": request.state.request_id}
