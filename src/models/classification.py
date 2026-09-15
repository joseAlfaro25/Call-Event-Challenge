"""Structured classifier output."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

ConversationalLabel = Literal[
    "callback",
    "cortada",
    "visita_sin_confirmar",
    "documentacion_enviada",
    "documentacion_pendiente",
    "descartado",
    "no_contactar",
    "persona_equivocada",
    "otro",
]


class Classification(BaseModel):
    """Only conversational facts may be supplied by the language model."""

    model_config = ConfigDict(
        extra="forbid", populate_by_name=True, validate_assignment=True
    )

    label: ConversationalLabel = Field(alias="etiqueta")
    confidence: float = Field(alias="confianza", ge=0, le=1)
    reason: str = Field(alias="motivo", min_length=1, max_length=1_000)
    callback_when_raw: str | None = Field(default=None, max_length=300)
    callback_day: str | None = Field(default=None, max_length=30)
    callback_time: str | None = Field(
        default=None, max_length=5, pattern=r"^\d{2}:\d{2}$"
    )
    declared_email: str | None = Field(
        default=None, alias="email_declarado", max_length=320
    )
