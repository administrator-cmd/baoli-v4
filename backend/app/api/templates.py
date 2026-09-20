from io import BytesIO
from typing import Literal
from urllib.parse import quote

from fastapi import APIRouter, Query, Request
from fastapi.responses import StreamingResponse

from ..services.template_service import available_examples, build_contract_review_template


router = APIRouter(prefix="/rulebook-templates", tags=["规则库模板"])

TemplateExample = Literal["blank", "single_contract", "contract_and_invoice"]

_FILENAMES = {
    "blank": "合同审核规则库模板-空白.xlsx",
    "single_contract": "合同审核规则库模板-示例A-单份主合同.xlsx",
    "contract_and_invoice": "合同审核规则库模板-示例B-主合同与发票.xlsx",
}


@router.get("/contract-review/examples")
def list_contract_review_examples(request: Request) -> dict:
    """当前格式版本真实可下载的模板示例。

    页面据此渲染下载入口，不再写死「示例 A / 示例 B」。节点版 v3/v4 没有真实示例内容，
    这里只会返回 blank；继续展示必然下载失败的示例会让用户点下去只拿到报错。
    """
    return {"request_id": request.state.request_id, "items": available_examples()}


@router.get("/contract-review/download")
def download_contract_review_template(
    example: TemplateExample = Query(
        default="blank",
        description="blank=空白模板；其余取值必须是该格式版本真实提供的示例",
    ),
) -> StreamingResponse:
    """下载规则库模板。

    模板内容由 profile 决定：声明了说明页的格式版本会额外带说明页，v2 只输出业务工作表。
    空白模板只带表头；示例必须是 profile 里真实声明的内容，没有就明确报错，
    不编造示例数据、也不返回一份空文件冒充示例。
    """
    filename = _FILENAMES.get(example, f"合同审核规则库模板-{example}.xlsx")
    headers = {"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"}
    return StreamingResponse(
        BytesIO(build_contract_review_template(example)),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers=headers,
    )
