from ...common.exceptions import BusinessError


def validate_page_params(page: int, page_size: int) -> tuple[int, int]:
    """统一验证管理端分页查询条件。"""
    if page < 1:
        raise BusinessError("PAGE_INVALID", "页码必须是大于 0 的整数", status_code=400)
    if page_size < 1 or page_size > 200:
        raise BusinessError("PAGE_SIZE_INVALID", "每页条数必须在 1 到 200 之间", status_code=400)
    return page, page_size

