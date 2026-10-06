"""Composed integration API."""

from fastapi import APIRouter

from src.api.integrations.actions import router as actions_router
from src.api.integrations.devices import router as devices_router
from src.api.integrations.grants import router as grants_router
from src.api.integrations.selected_context import router as context_router
from src.api.integrations.handoffs import router as handoffs_router
from src.api.integrations.tools import router as tools_router

router = APIRouter(prefix="/integrations", tags=["integrations"])
router.include_router(grants_router)
router.include_router(devices_router)
router.include_router(tools_router)
router.include_router(context_router)
router.include_router(actions_router)
router.include_router(handoffs_router)
