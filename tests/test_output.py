import json
from pathlib import Path

from jsonschema import Draft202012Validator

from domain.decision import build_decision
from models.event import Event


PROJECT_ROOT = Path(__file__).resolve().parents[1]
EVENT_SCHEMA = PROJECT_ROOT / "reto-kontaktu/esquemas/evento.schema.json"
DECISION_SCHEMA = PROJECT_ROOT / "reto-kontaktu/esquemas/decision.schema.json"


def test_output_dict_preserves_forward_compatible_event_fields():
    event = Event.from_file(
        PROJECT_ROOT / "reto-kontaktu/eventos/04-call-ended-rosa.json",
        EVENT_SCHEMA,
    )

    payload = event.output_dict()

    assert payload["delivery_attempt"] == 1
    assert payload["recording"]["gcs_path"].endswith(".ogg")
    assert payload["metrics"]["turns"] == 6


def test_decision_contains_complete_event_and_auditable_reasoning():
    event = Event.from_file(
        PROJECT_ROOT / "reto-kontaktu/eventos/04-call-ended-rosa.json",
        EVENT_SCHEMA,
    )
    decision = build_decision(
        event,
        "cortada",
        "La conversación terminó durante la cualificación",
        0.60,
        [],
        "llm",
    )

    Draft202012Validator(json.loads(DECISION_SCHEMA.read_text())).validate(decision)
    assert decision["evento"] == event.output_dict()
    assert len(decision["razonamiento"]) >= 200
    assert "SIP 200" in decision["razonamiento"]
