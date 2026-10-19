"""场景接口路由汇总。"""

from fastapi import APIRouter

from . import lifecycle, configuration, materials, fields, rules, sections, publishing

router = APIRouter()
router.include_router(lifecycle.router)
router.include_router(configuration.router)
router.include_router(materials.router)
router.include_router(fields.router)
router.include_router(rules.router)
router.include_router(sections.router)
router.include_router(publishing.router)
