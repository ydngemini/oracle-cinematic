"""Validated wire contracts for the private NEOH assistant."""

from __future__ import annotations

import uuid
import re
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

RecordType = Literal["client", "lead", "listing", "contract"]
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class ChatContext(BaseModel):
    type: RecordType
    id: uuid.UUID


class ChatSendFrame(BaseModel):
    type: Literal["AI_CHAT_SEND"]
    version: Literal[1]
    request_id: uuid.UUID
    content: str = Field(default="", max_length=8_000)
    context: Optional[ChatContext] = None
    attachment_ids: list[uuid.UUID] = Field(default_factory=list, max_length=5)

    @field_validator("content")
    @classmethod
    def clean_content(cls, value: str) -> str:
        return value.strip()

    @model_validator(mode="after")
    def require_content_or_attachment(self):
        if not self.content and not self.attachment_ids:
            raise ValueError("A message or attachment is required")
        if self.attachment_ids and self.context is None:
            raise ValueError("Attachments require a selected record")
        if len(set(self.attachment_ids)) != len(self.attachment_ids):
            raise ValueError("Duplicate attachment IDs are not allowed")
        return self


class SafeClientUpdate(BaseModel):
    client_id: uuid.UUID
    full_name: Optional[str] = Field(None, min_length=1, max_length=160)
    email: Optional[str] = Field(None, max_length=254)
    phone: Optional[str] = Field(None, max_length=40)
    client_type: Optional[Literal["seller", "buyer", "both"]] = None
    stage: Optional[
        Literal["lead", "active", "nurture", "under_contract", "closed", "lost"]
    ] = None
    lead_score: Optional[int] = Field(None, ge=0, le=100)
    assignee_id: Optional[str] = Field(None, max_length=160)
    company: Optional[str] = Field(None, max_length=200)
    source: Optional[str] = Field(None, max_length=80)

    @field_validator("email")
    @classmethod
    def valid_email(cls, value: Optional[str]) -> Optional[str]:
        if value is None or value == "":
            return value
        value = value.strip()
        if not _EMAIL_RE.match(value):
            raise ValueError("not a valid email address")
        return value


class SafeClientNote(BaseModel):
    client_id: uuid.UUID
    body: str = Field(..., min_length=1, max_length=4_000)


class SafeListingUpdate(BaseModel):
    listing_id: uuid.UUID
    address: Optional[str] = Field(None, min_length=3, max_length=300)
    status: Optional[Literal["draft", "active", "pending", "sold", "withdrawn"]] = None
