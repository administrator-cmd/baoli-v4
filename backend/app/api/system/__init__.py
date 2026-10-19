"""系统接口路由汇总。"""

from fastapi import APIRouter

from . import status, catalogs, logs, operations

router = APIRouter()
router.include_router(status.router)
router.include_router(catalogs.router)
router.include_router(logs.router)
router.include_router(operations.router)
