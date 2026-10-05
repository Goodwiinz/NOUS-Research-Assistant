"""Safe Research Engine connector capability discovery."""

from fastapi import APIRouter, Depends

from src.core.dependencies import get_current_user
from src.schemas.research_engine import ConnectorCapabilityResponse
from src.services.research_engine.connectors.registry import safe_capability_projection

router = APIRouter(
    prefix="/research-engine/capabilities",
    tags=["research-engine"],
)


@router.get(
    "",
    response_model=list[ConnectorCapabilityResponse],
    dependencies=[Depends(get_current_user)],
)
async def list_connector_capabilities() -> list[ConnectorCapabilityResponse]:
    """Return the safe projection of canonical research connectors."""
    return [
        ConnectorCapabilityResponse.model_validate(item)
        for item in safe_capability_projection()
    ]
