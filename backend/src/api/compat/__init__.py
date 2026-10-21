"""Compatibility transports backed by the native NOUS execution pipeline."""

from .alphaxiv import router
from .alphaxiv_history import router as history_router
from .alphaxiv_live import router as live_router
from .alphaxiv_upload import router as upload_router

router.include_router(live_router)
router.include_router(history_router)
router.include_router(upload_router)

__all__ = ["router"]
