"""AI Writer API Router - inline text completion, section generation, and outline generation."""

from fastapi import APIRouter, Depends, HTTPException, status
from structlog import get_logger

from src.core.dependencies import get_current_user
from src.models.user import User
from src.services.research.writer_service import WriterService
from src.shared.scispace_schemas import (
    OutlineRequest,
    OutlineResponse,
    WriteRequest,
    WriteResponse,
)

logger = get_logger()
router = APIRouter(prefix="/api/v1/research", tags=["ai-writer"])

_service = WriterService()


@router.post("/write", response_model=WriteResponse)
async def write_text(
    request: WriteRequest,
    current_user: User = Depends(get_current_user),
):
    """Generate text based on cursor context and action."""
    try:
        result = await _service.write(
            action=request.action.value,
            cursor_context=request.cursor_context,
            section_type=request.section_type.value if request.section_type else None,
            style=request.style.value,
            document_ids=(
                [str(d) for d in request.document_ids] if request.document_ids else None
            ),
        )
        return WriteResponse(**result)
    except ValueError:
        logger.warning("Invalid writer request", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid writer request",
        )
    except Exception as e:
        logger.error("write_failed", error=str(e))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to generate text",
        )


@router.post("/outline", response_model=OutlineResponse)
async def generate_outline(
    request: OutlineRequest,
    current_user: User = Depends(get_current_user),
):
    """Generate a paper outline based on a research question."""
    try:
        result = await _service.generate_outline(
            research_question=request.research_question,
            style=request.style.value,
            document_ids=(
                [str(d) for d in request.document_ids] if request.document_ids else None
            ),
            section_types=(
                [s.value for s in request.section_types]
                if request.section_types
                else None
            ),
        )
        return OutlineResponse(**result)
    except ValueError:
        logger.warning("Invalid writer request", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid writer request",
        )
    except Exception as e:
        logger.error("outline_failed", error=str(e))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to generate outline",
        )
