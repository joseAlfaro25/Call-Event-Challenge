"""Validate replay output against the input contract and golden expectations."""

import json
from datetime import datetime
from pathlib import Path
import sys

import yaml
from jsonschema import Draft202012Validator

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from config import CampaignConfig  # noqa: E402
from models.event import Event  # noqa: E402
from models.orders import Order, validate_order  # noqa: E402
from domain.cases import STATUS_BY_LABEL  # noqa: E402
from domain.scheduling import is_in_call_window  # noqa: E402

INPUT_ROOT = PROJECT_ROOT / "reto-kontaktu"
OUTPUT_ROOT = PROJECT_ROOT / "output"
EXPECTED = yaml.safe_load((PROJECT_ROOT / "tests" / "expected.yaml").read_text())[
    "events"
]
DECISION_SCHEMA = json.loads(
    (INPUT_ROOT / "esquemas" / "decision.schema.json").read_text()
)
CONFIG = CampaignConfig.load(INPUT_ROOT / "eventos" / "01-call-ended-nuria.json")


def load_json_lines(path: Path) -> list[dict]:
    """Load non-empty JSONL rows and fail clearly when an artifact is absent."""
    if not path.is_file():
        raise AssertionError(f"missing output: {path}")
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def input_paths() -> list[Path]:
    """Return the ordered sample events followed by synthetic hidden-case fixtures."""
    names = [
        line.strip()
        for line in (INPUT_ROOT / "eventos" / "orden.txt").read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    return [INPUT_ROOT / "eventos" / name for name in names] + sorted(
        (PROJECT_ROOT / "tests" / "extra_events").glob("*.json")
    )


def validate_inputs() -> None:
    """Validate every replay input against the official schema and model limits."""
    for path in input_paths():
        Event.from_file(path, CONFIG.event_schema_path)


def validate_outputs(decisions: list[dict], orders: list[dict]) -> None:
    """Validate contracts, golden labels, domain invariants, and audit coverage."""
    for decision in decisions:
        Draft202012Validator(DECISION_SCHEMA).validate(decision)
        assert isinstance(decision.get("razonamiento"), str)
        assert len(decision["razonamiento"]) >= 200
        event_snapshot = decision.get("evento")
        assert isinstance(event_snapshot, dict)
        assert event_snapshot.get("event_id") == decision["event_id"]
    parsed_orders = [Order.model_validate(order) for order in orders]
    for order in parsed_orders:
        validate_order(order)
    assert len({order.idempotency_key for order in parsed_orders}) == len(
        parsed_orders
    ), "duplicate order idempotency key"
    assert len({order.order_id for order in parsed_orders}) == len(
        parsed_orders
    ), "duplicate order id"

    decisions_by_event = {}
    for decision in decisions:
        decisions_by_event[decision["event_id"]] = decision
    assert set(decisions_by_event) == set(EXPECTED), "golden event set mismatch"

    orders_by_event = {
        event_id: [order for order in parsed_orders if order.event_id == event_id]
        for event_id in decisions_by_event
    }
    for event_id, expected in EXPECTED.items():
        decision = decisions_by_event[event_id]
        emitted = orders_by_event[event_id]
        assert decision["etiqueta"] == expected["label"], event_id
        assert sorted(order.operation for order in emitted) == sorted(
            expected["operations"]
        ), event_id
        if event_id != "evt_15":
            assert decision["ordenes"] == [order.order_id for order in emitted]

    for event_id, event_orders in orders_by_event.items():
        label = decisions_by_event[event_id]["etiqueta"]
        if label == "no_aplica":
            assert not any(
                order.operation == "cerrar_llamada" for order in event_orders
            )
        if label == "no_contactar":
            assert {order.operation for order in event_orders} <= {
                "cerrar_llamada",
                "marcar_no_contactar",
            }
        close_orders = [
            order for order in event_orders if order.operation == "cerrar_llamada"
        ]
        if close_orders:
            assert close_orders[0].body["status"] == STATUS_BY_LABEL[label]
        for order in event_orders:
            if order.operation == "programar_llamada":
                scheduled = datetime.fromisoformat(order.body["no_antes_de"])
                assert scheduled.tzinfo and scheduled.utcoffset() is not None
                assert is_in_call_window(scheduled, CONFIG)

    database = __import__("sqlite3").connect(
        PROJECT_ROOT / "state" / "orchestrator.sqlite"
    )
    audit_count = database.execute("SELECT COUNT(*) FROM processing_audit").fetchone()[
        0
    ]
    assert audit_count >= len(EXPECTED), "processing audit is incomplete"
    database.close()


def main() -> int:
    """Run all output checks and return a shell-friendly status code."""
    validate_inputs()
    decisions = load_json_lines(OUTPUT_ROOT / "decisions.jsonl")
    orders = load_json_lines(OUTPUT_ROOT / "orders.jsonl")
    validate_outputs(decisions, orders)
    print(
        f"OK: {len(decisions)} decisions, {len(orders)} orders; "
        "contracts, golden file, audit, and invariants passed"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
