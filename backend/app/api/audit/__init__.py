"""审核接口路由汇总。"""

from fastapi import APIRouter

from . import tasks, executions, documents, results, history

router = APIRouter()
router.include_router(tasks.router)
router.include_router(executions.router)
router.include_router(documents.router)
router.include_router(results.router)
router.include_router(history.executions_router)
