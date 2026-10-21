"""Raw-body upload alias using canonical NOUS storage and ingestion."""

from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.agent import execute
from src.core.database import get_db
from src.core.dependencies import get_current_user
from src.core.rate_limit import create_rate_limiter
from src.models.user import User
from src.schemas.alphaxiv_files import AlphaXivUploadResponse
from src.services.compat.alphaxiv import CompatibilityError
from src.services.compat.alphaxiv_files import upload_file

router = APIRouter(tags=["alphaXiv compatibility"])

# This router is mounted at the application root, so ApiRateLimitMiddleware
# (which only counts /api/ paths) never sees it. Mirror the native
# ``documents_upload`` bucket (30 POSTs/minute per user) in the route itself.
_UPLOAD_RATE_LIMIT_RPM = 30
_UPLOAD_RATE_WINDOW_MINUTES = 1
_UPLOAD_RATE_PREFIX = "alphaxiv_upload"
_upload_rate_limiter = create_rate_limiter(
    max_attempts=_UPLOAD_RATE_LIMIT_RPM,
    window_minutes=_UPLOAD_RATE_WINDOW_MINUTES,
)


@router.post(
    "/v1/assistant/upload-file",
    response_model=AlphaXivUploadResponse,
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                mime: {"schema": {"type": "string", "format": "binary"}}
                for mime in ("application/pdf", "text/plain", "text/markdown")
            },
        }
    },
)
async def upload_assistant_file(
    request: Request,
    x_filename: str = Header(..., alias="X-Filename", max_length=4500),
    content_type: str = Header(default="", alias="Content-Type", max_length=100),
    content_length: int | None = Header(default=None, alias="Content-Length", ge=0),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> AlphaXivUploadResponse:
    # Before the body is read: a limited caller must not cost a spool/upload.
    await execute._enforce_rate_limit(
        _upload_rate_limiter, str(current_user.id), _UPLOAD_RATE_PREFIX
    )
    try:
        return await upload_file(
            db,
            actor_id=UUID(str(current_user.id)),
            encoded_filename=x_filename,
            content_type=content_type,
            content_length=content_length,
            chunks=request.stream(),
        )
    except CompatibilityError as exc:
        raise HTTPException(exc.status_code, str(exc)) from exc
