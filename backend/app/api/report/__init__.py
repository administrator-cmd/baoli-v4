"""报告与风险接口路由汇总。"""

from fastapi import APIRouter

from . import reports, risks

router = APIRouter()
router.include_router(reports.router)
router.include_router(risks.risks_router)
