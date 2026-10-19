"""材料与证据接口路由汇总。"""

from fastapi import APIRouter

from . import documents, evidence

router = APIRouter()
router.include_router(documents.router)
router.include_router(evidence.evidence_router)
