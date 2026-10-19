"""方案接口路由汇总。"""

from fastapi import APIRouter

from . import lifecycle, configuration, tasks

router = APIRouter()
router.include_router(lifecycle.router)
router.include_router(configuration.router)
router.include_router(tasks.router)
