"""Strict CRM order envelopes and request-body validation."""

from datetime import datetime
from typing import Literal, Type

from pydantic import BaseModel, ConfigDict, Field, field_validator

from domain.cases import CaseLabel

Status = Literal[
    "pending",
    "scheduled",
    "in_progress",
    "completed",
    "successful",
    "failed",
    "no_answer",
    "callback_requested",
    "dnc",
    "refused",
    "needs_review",
    "skipped",
]
Template = Literal[
    "primer_toque_respaldo",
    "recordatorio_documentacion",
    "aviso_cambio_hora",
]
TaskType = Literal[
    "confirmar_visita_direccion",
    "verificar_telefono",
    "enviar_documentacion_email",
    "llamar_a_mano",
    "revisar_llamada",
]


class RequestModel(BaseModel):
    """Shared strict configuration for all CRM request bodies."""

    model_config = ConfigDict(
        extra="forbid", populate_by_name=True, validate_assignment=True
    )


def require_aware(value: datetime) -> datetime:
    """Reject datetimes without an explicit timezone offset."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must include an explicit UTC offset")
    return value


class CloseRequest(RequestModel):
    """Request body for closing a queue entry."""

    entry_id: str = Field(min_length=1)
    status: Status
    label: str = Field(alias="etiqueta")
    reason: str = Field(alias="motivo", min_length=1)
    confidence: float = Field(alias="confianza", ge=0, le=1)
    duration_seconds: int | None = Field(default=None, ge=0)

    @field_validator("label")
    @classmethod
    def validate_label(cls, value: str) -> str:
        if value not in {
            label.value for label in CaseLabel if label is not CaseLabel.NOT_APPLICABLE
        }:
            raise ValueError("invalid case label")
        return value


class ScheduleCallRequest(RequestModel):
    """Request body for a new voice attempt."""

    entry_id: str = Field(min_length=1)
    phone: str = Field(alias="telefono", min_length=3)
    no_earlier_than: datetime = Field(alias="no_antes_de")
    reason: str = Field(alias="motivo", min_length=1)
    context_note: str | None = Field(default=None, alias="nota_contexto")

    _validate_datetime = field_validator("no_earlier_than")(require_aware)


class SendTemplateRequest(RequestModel):
    """Request body for an approved WhatsApp template."""

    organization_id: str = Field(min_length=1)
    phone: str = Field(alias="telefono", min_length=3)
    template: Template = Field(alias="plantilla")
    parameters: dict[str, str] = Field(default_factory=dict, alias="parametros")
    language: str = Field(default="es", alias="idioma", min_length=2)


class ScheduleReminderRequest(RequestModel):
    """Request body for either a lead or commercial reminder."""

    contact_id: str = Field(min_length=1)
    channel: Literal["whatsapp_lead", "tarea_comercial"] = Field(alias="canal")
    template: Template | None = Field(default=None, alias="plantilla")
    task_type: TaskType | None = Field(default=None, alias="tipo_tarea")
    when: datetime = Field(alias="cuando")
    cancel_if: Literal["lead_responde", "ninguna"] = Field(
        default="ninguna", alias="cancelar_si"
    )

    _validate_datetime = field_validator("when")(require_aware)

    def model_post_init(self, __context) -> None:
        if self.channel == "whatsapp_lead":
            if self.template is None or self.task_type is not None:
                raise ValueError("WhatsApp reminder requires only plantilla")
        elif self.task_type is None or self.template is not None:
            raise ValueError("commercial reminder requires only tipo_tarea")


class CancelReminderRequest(RequestModel):
    """Request body for cancelling a previously generated reminder."""

    reminder_id: str = Field(min_length=1)
    reason: str = Field(alias="motivo", min_length=1)


class CreateTaskRequest(RequestModel):
    """Request body for a human CRM task."""

    contact_id: str = Field(min_length=1)
    call_id: str | None = None
    task_type: TaskType = Field(alias="tipo")
    title: str = Field(alias="titulo", min_length=1)
    detail: str | None = Field(default=None, alias="detalle")
    due_at: datetime = Field(alias="vence_el")
    assigned_to: Literal["comercial_asignado", "cualquiera"] = Field(
        default="comercial_asignado", alias="asignada_a"
    )

    _validate_datetime = field_validator("due_at")(require_aware)


class DoNotContactRequest(RequestModel):
    """Request body for an all-channel or channel-specific opt-out."""

    phone: str = Field(alias="telefono", min_length=3)
    contact_id: str | None = None
    channel: Literal["todos", "voz", "whatsapp"] = Field(alias="canal")
    reason: str = Field(alias="motivo", min_length=1)
    origin: str | None = Field(default=None, alias="origen")


REQUEST_MODELS: dict[str, Type[RequestModel]] = {
    "cerrar_llamada": CloseRequest,
    "programar_llamada": ScheduleCallRequest,
    "enviar_plantilla_whatsapp": SendTemplateRequest,
    "programar_recordatorio": ScheduleReminderRequest,
    "cancelar_recordatorio": CancelReminderRequest,
    "crear_tarea": CreateTaskRequest,
    "marcar_no_contactar": DoNotContactRequest,
}


class Order(RequestModel):
    """Idempotent order envelope written to the JSONL contract."""

    order_id: str = Field(alias="orden_id", min_length=1)
    event_id: str = Field(min_length=1)
    operation: str = Field(alias="operacion", min_length=1)
    idempotency_key: str = Field(min_length=1)
    body: dict = Field(alias="cuerpo")

    def external_dict(self) -> dict:
        """Serialize English model attributes using the external contract aliases."""
        return self.model_dump(by_alias=True, exclude_none=True)


def validate_order(order: Order) -> None:
    """Validate an order body against its operation-specific OpenAPI equivalent."""
    request_model = REQUEST_MODELS.get(order.operation)
    if request_model is None:
        raise ValueError(f"unsupported CRM operation: {order.operation}")
    request_model.model_validate(order.body)
