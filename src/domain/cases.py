"""Closed case catalogue and CRM queue status mapping."""

from enum import StrEnum


class CaseLabel(StrEnum):
    """English code identifiers mapped to the contract's Spanish labels."""

    RESERVED_VISIT = "visita_reservada"
    DOCUMENTATION_SENT = "documentacion_enviada"
    CALLBACK = "callback"
    NO_ANSWER = "sin_respuesta"
    BUSY = "ocupado"
    VOICEMAIL = "buzon"
    CUT_OFF = "cortada"
    VISIT_UNCONFIRMED = "visita_sin_confirmar"
    WRONG_PERSON = "persona_equivocada"
    DO_NOT_CONTACT = "no_contactar"
    REJECTED = "rechazada"
    DOCUMENTATION_PENDING = "documentacion_pendiente"
    DISCARDED = "descartado"
    OTHER = "otro"
    NOT_APPLICABLE = "no_aplica"


STATUS_BY_LABEL = {
    CaseLabel.RESERVED_VISIT: "successful",
    CaseLabel.DOCUMENTATION_SENT: "completed",
    CaseLabel.DOCUMENTATION_PENDING: "completed",
    CaseLabel.CALLBACK: "callback_requested",
    CaseLabel.NO_ANSWER: "no_answer",
    CaseLabel.BUSY: "no_answer",
    CaseLabel.VOICEMAIL: "no_answer",
    CaseLabel.CUT_OFF: "needs_review",
    CaseLabel.VISIT_UNCONFIRMED: "needs_review",
    CaseLabel.WRONG_PERSON: "failed",
    CaseLabel.DO_NOT_CONTACT: "dnc",
    CaseLabel.REJECTED: "refused",
    CaseLabel.DISCARDED: "skipped",
    CaseLabel.OTHER: "needs_review",
}
