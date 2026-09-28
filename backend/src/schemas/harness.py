"""Strict bridge wire protocol. Lifecycle events remain server-owned."""

import hashlib
import json
from datetime import datetime, timezone
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    StrictInt,
    StrictStr,
    model_validator,
)

from src.services.agent.run_event_types import (
    BRIDGE_PRODUCER_EVENTS,
    PAYLOAD_MODELS,
    ApprovalRequiredPayload,
    ApprovalResolvedPayload,
    AssistantDeltaPayload,
    RunEventType,
    ToolCompletedPayload,
    ToolStartedPayload,
    UsageUpdatedPayload,
)


class WireModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Start(WireModel):
    kind: Literal["start"]
    input: str = Field(min_length=1, max_length=65536)
    sessionId: str | None = Field(default=None, min_length=1, max_length=255)


class Interrupt(WireModel):
    kind: Literal["interrupt"]
    sessionId: str = Field(min_length=1, max_length=255)
    turnId: str = Field(min_length=1, max_length=255)


class Decision(WireModel):
    kind: Literal["decision"]
    allow: bool = Field(strict=True)


class Answers(WireModel):
    kind: Literal["answers"]
    answers: dict[str, list[str]]


class Respond(WireModel):
    kind: Literal["respond"]
    requestId: StrictStr | Annotated[StrictInt, Field(ge=-(2**53 - 1), le=2**53 - 1)]
    response: Annotated[Decision | Answers, Field(discriminator="kind")]
    approvalRecordId: UUID


class Envelope(WireModel):
    deviceId: UUID
    runId: UUID
    commandId: UUID
    workspaceId: UUID
    generation: int = Field(ge=1, le=2**53 - 1, strict=True)


class BridgeCommand(Envelope):
    expiresAt: datetime
    body: Annotated[Start | Interrupt | Respond, Field(discriminator="kind")]

    @model_validator(mode="after")
    def utc_expiry(self) -> "BridgeCommand":
        if self.expiresAt.utcoffset() != timezone.utc.utcoffset(None):
            raise ValueError("expiresAt must be UTC")
        return self


TypedRunPayload = (
    AssistantDeltaPayload
    | ToolStartedPayload
    | ToolCompletedPayload
    | ApprovalRequiredPayload
    | ApprovalResolvedPayload
    | UsageUpdatedPayload
)


class ProducerEvent(WireModel):
    kind: Literal["event"]
    eventType: RunEventType
    payload: TypedRunPayload

    @model_validator(mode="before")
    @classmethod
    def typed_payload(cls, value: Any) -> Any:
        if isinstance(value, dict):
            event_type = RunEventType(value.get("eventType", ""))
            if event_type not in BRIDGE_PRODUCER_EVENTS:
                raise ValueError("bridge cannot write lifecycle events")
            value = dict(value)
            value["payload"] = PAYLOAD_MODELS[event_type].model_validate(
                value["payload"]
            )
        return value


class Observation(WireModel):
    kind: Literal["observation"]
    state: Literal["unknown", "running", "completed", "failed", "interrupted"]
    sessionId: str = Field(min_length=1, max_length=255)
    turnId: str | None = Field(max_length=255)


class BridgeEvent(Envelope):
    sourceId: str = Field(min_length=1, max_length=128)
    sourceSeq: int = Field(ge=1, le=2**53 - 1, strict=True)
    body: Annotated[ProducerEvent | Observation, Field(discriminator="kind")]
    _wire_digest: str = PrivateAttr(default="")

    @model_validator(mode="wrap")
    @classmethod
    def bounded_wire(cls, value: Any, handler: Any) -> Any:
        raw = value.model_dump(mode="json") if isinstance(value, cls) else value
        encoded = json.dumps(
            raw, sort_keys=True, separators=(",", ":"), default=str
        ).encode()
        if len(encoded) > 16 * 1024:
            raise ValueError("bridge event exceeds 16 KiB")
        result = handler(value)
        result._wire_digest = hashlib.sha256(encoded).hexdigest()
        return result
