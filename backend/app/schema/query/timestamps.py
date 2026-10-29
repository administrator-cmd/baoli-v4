"""时间筛选参数的统一校验。

数据库的时间列统一按 UTC 存储，比较时由 `UTCDateTime` 把带时区的值换算成 UTC。
因此凡是作为时间条件传入的 `datetime` 必须自带时区；缺时区时无法判断它指的是哪个
时刻，明确拒绝而不是按服务器本地时区猜一个。

只写日期的 `2026-10-19` 同样被拒绝：边界应该落在哪个时刻取决于调用方所在时区，
由前端换算成本地日界对应的 UTC 时刻后提交（见 `frontend/src/api/query.ts`）。
"""

from datetime import datetime
from typing import Annotated

from pydantic import AfterValidator

from ...common.exceptions import BusinessError


def _reject_naive(value: datetime) -> datetime:
    if value.utcoffset() is None:
        raise BusinessError(
            "DATETIME_TIMEZONE_REQUIRED",
            "时间筛选参数必须带时区偏移（例如 2026-10-19T00:00:00Z 或 2026-10-19T00:00:00+08:00）",
            status_code=400,
            details={"received": value.isoformat()},
        )
    return value


# 时间区间端点：必须是带时区的时间点。
FilterDateTime = Annotated[datetime, AfterValidator(_reject_naive)]
