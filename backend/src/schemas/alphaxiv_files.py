"""Observed file references, backed only by authenticated NOUS documents."""

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from src.shared.enums import ApiDocumentStatus


class AlphaXivFileReference(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    content_type: str = Field(alias="contentType", max_length=100)
    url: str = Field(min_length=1, max_length=2048)


class AlphaXivUploadedFile(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    url: str
    # Additive NOUS status fields: ingestion is asynchronous.
    document_id: UUID = Field(alias="documentId")
    processing_status: ApiDocumentStatus = Field(alias="processingStatus")


class AlphaXivUploadResponse(BaseModel):
    data: AlphaXivUploadedFile
