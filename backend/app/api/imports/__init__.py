"""规则库导入接口路由汇总。"""

from fastapi import APIRouter

from . import jobs

router = APIRouter()
router.include_router(jobs.router)
