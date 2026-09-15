"""Validated input models for the campaign event contract."""

from datetime import datetime
from copy import deepcopy
import json
from pathlib import Path
from typing import Any, Literal

from jsonschema import Draft202012Validator
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, model_validator


MAX_EVENT_BYTES = 1_000_000
MAX_TRANSCRIPT_LINES = 500


class ContractModel(BaseModel):
    """Base model that ignores forward-compatible contract additions."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)


class TranscriptLine(ContractModel):
    """One speaker turn from a completed call transcript."""

    role: Literal["agent", "user"]
    message: str = Field(max_length=10_000)
    time_in_call_secs: int = Field(ge=0)


class AnsweringMachineDetection(ContractModel):
    """Optional AMD result; mailbox detection has precedence over SIP 200."""

    result: (
        Literal[
            "human",
            "machine-vm",
            "machine-ivr",
            "machine-unavailable",
            "uncertain",
            "not_run",
        ]
        | None
    ) = None
    greeting_transcript: str | None = Field(default=None, max_length=10_000)
    detected_at_secs: float | None = Field(default=None, ge=0)
    source: Literal["livekit_amd", "heuristic_regex", "none"] | None = None


class Telephony(ContractModel):
    """Strict LiveKit SIP metadata required for ``call.ended`` events."""

    provider: Literal["livekit_sip"]
    call_id: str = Field(min_length=1, max_length=200)
    provider_call_id: str | None = Field(default=None, max_length=200)
    dialed_at: datetime
    ringing_at: datetime | None = None
    answered_at: datetime | None = None
    ended_at: datetime
    sip_status_code: int = Field(ge=100, le=699)
    sip_status: str = Field(default="", max_length=200)
    disconnect_reason: Literal[
        "CLIENT_INITIATED",
        "USER_REJECTED",
        "USER_UNAVAILABLE",
        "SIP_TRUNK_FAILURE",
        "ROOM_DELETED",
    ]
    hung_up_by: Literal["callee", "agent"] | None = None
    duration_seconds: int = Field(ge=0)
    amd: AnsweringMachineDetection | None = None


class Campaign(ContractModel):
    """Campaign routing data supplied by the event producer."""

    system_key: str = Field(min_length=1, max_length=200)
    entry_id: str = Field(min_length=1, max_length=200)


class Lead(ContractModel):
    """Contact data used to build CRM request bodies."""

    contact_id: str = Field(min_length=1, max_length=200)
    phone: str = Field(min_length=3, max_length=100)
    full_name: str | None = Field(default=None, max_length=300)
    lead_source: str | None = Field(default=None, max_length=200)
    property_ref: str | None = Field(default=None, max_length=200)
    property_address: str | None = Field(default=None, max_length=500)
    language: str = Field(default="es", min_length=2, max_length=20)


class Appointment(ContractModel):
    """Appointment created by the agent during a call."""

    appointment_id: str = Field(min_length=1, max_length=200)
    start_time: datetime


class AgentOutcome(ContractModel):
    """Trusted deterministic outcome and extracted call slots."""

    call_outcome: Literal[
        "",
        "completed",
        "no_answer",
        "dnc",
        "callback_requested",
        "failed",
    ] = ""
    reason: str | None = Field(default=None, max_length=1_000)
    appointment: Appointment | None = None
    slots_snapshot: dict[str, Any] = Field(default_factory=dict)


class Message(ContractModel):
    """Inbound WhatsApp message payload."""

    channel: Literal["whatsapp"]
    text: str = Field(min_length=1, max_length=20_000)


class Quality(ContractModel):
    """Quality flags that determine whether conversational classification is safe."""

    no_conversation: bool | None = None


class Event(ContractModel):
    """Discriminated event model for calls and inbound messages."""

    _raw_payload: dict[str, Any] = PrivateAttr(default_factory=dict)

    event_id: str = Field(min_length=1, max_length=200)
    type: Literal["call.ended", "message.received"]
    occurred_at: datetime
    organization_id: str = Field(min_length=1, max_length=200)
    idempotency_key: str = Field(min_length=1, max_length=300)
    campaign: Campaign
    lead: Lead
    telephony: Telephony | None = None
    transcript: list[TranscriptLine] | None = None
    agent_outcome: AgentOutcome | None = None
    quality: Quality | None = None
    message: Message | None = None

    @model_validator(mode="after")
    def validate_shape(self):
        """Enforce the conditional blocks for each event type."""
        if self.type == "call.ended":
            if self.telephony is None:
                raise ValueError("call.ended requires telephony")
            if self.transcript is None:
                self.transcript = []
            if self.agent_outcome is None:
                self.agent_outcome = AgentOutcome()
            if self.message is not None:
                raise ValueError("call.ended cannot contain message")
        else:
            if self.message is None:
                raise ValueError("message.received requires message")
            if self.telephony is not None:
                raise ValueError("message.received cannot contain telephony")
            if self.transcript not in (None, []):
                raise ValueError("message.received cannot contain transcript")
            if self.agent_outcome is not None:
                raise ValueError("message.received cannot contain agent_outcome")
        if self.transcript is not None and len(self.transcript) > MAX_TRANSCRIPT_LINES:
            raise ValueError("transcript exceeds the supported size")
        return self

    def output_dict(self) -> dict[str, Any]:
        """Return the complete accepted payload, including forward-compatible fields."""
        if self._raw_payload:
            return deepcopy(self._raw_payload)
        return self.model_dump(mode="json", exclude_none=False)

    @classmethod
    def from_file(cls, event_path: str | Path, schema_path: str | Path) -> "Event":
        """Read, size-check, JSON-Schema-check, and parse one event file."""
        path = Path(event_path).resolve()
        if not path.is_file():
            raise ValueError(f"event file does not exist: {path}")
        if path.stat().st_size > MAX_EVENT_BYTES:
            raise ValueError("event file exceeds the supported size")
        payload = json.loads(path.read_text(encoding="utf-8"))
        schema = json.loads(Path(schema_path).read_text(encoding="utf-8"))
        errors = sorted(Draft202012Validator(schema).iter_errors(payload), key=str)
        if errors:
            raise ValueError(f"event schema validation failed: {errors[0].message}")
        event = cls.model_validate(payload)
        # The typed model intentionally ignores forward-compatible fields. Keep the
        # original validated payload so audit/output consumers do not lose fields such
        # as delivery_attempt, recording, metrics, or future contract additions.
        event._raw_payload = deepcopy(payload)
        return event
