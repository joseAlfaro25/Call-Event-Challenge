"""SQLite-backed idempotency, domain effects, and processing audit."""

from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
from typing import Iterator

from models.event import Event
from models.orders import Order
from .jsonl_output import append_jsonl


class Store:
    """Owns all durable domain state for one orchestrator installation."""

    def __init__(self, path: str | Path):
        """Open SQLite and create only the tables owned by this application."""
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.database = sqlite3.connect(
            self.path, timeout=30, isolation_level=None, check_same_thread=False
        )
        self.database.execute("PRAGMA busy_timeout=30000")
        self.database.execute("PRAGMA journal_mode=WAL")
        self.database.executescript("""
            CREATE TABLE IF NOT EXISTS processed_events (
                idempotency_key TEXT PRIMARY KEY,
                decision_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS emitted_orders (
                idempotency_key TEXT PRIMARY KEY,
                order_id TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS attempts (
                contact_id TEXT PRIMARY KEY,
                count INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS attempt_events (
                event_id TEXT PRIMARY KEY,
                contact_id TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS reminders (
                reminder_id TEXT PRIMARY KEY,
                contact_id TEXT NOT NULL,
                status TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS do_not_contact (
                contact_id TEXT PRIMARY KEY
            );
            CREATE TABLE IF NOT EXISTS channel_preferences (
                contact_id TEXT NOT NULL,
                channel TEXT NOT NULL,
                allowed INTEGER NOT NULL,
                PRIMARY KEY (contact_id, channel)
            );
            CREATE TABLE IF NOT EXISTS cut_off_calls (
                event_id TEXT PRIMARY KEY,
                contact_id TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS processing_audit (
                event_id TEXT PRIMARY KEY,
                origin TEXT,
                model TEXT,
                prompt_version TEXT,
                latency_ms REAL
            );
            """)

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Serialize writers and roll back every partial domain effect."""
        self.database.execute("BEGIN IMMEDIATE")
        try:
            yield self.database
        except Exception:
            self.database.rollback()
            raise
        else:
            self.database.commit()

    def close(self) -> None:
        """Close the process-local SQLite connection."""
        self.database.close()

    def get_processed(self, idempotency_key: str) -> dict | None:
        """Return the original decision for an idempotent fact, if committed."""
        row = self.database.execute(
            "SELECT decision_json FROM processed_events WHERE idempotency_key=?",
            (idempotency_key,),
        ).fetchone()
        return json.loads(row[0]) if row else None

    def get_attempts(self, contact_id: str) -> int:
        """Return first-delivery voice attempts for a contact."""
        row = self.database.execute(
            "SELECT count FROM attempts WHERE contact_id=?", (contact_id,)
        ).fetchone()
        return int(row[0]) if row else 0

    def get_cut_off_count(self, contact_id: str) -> int:
        """Return prior cut-off outcomes used by the N4 review rule."""
        row = self.database.execute(
            "SELECT COUNT(*) FROM cut_off_calls WHERE contact_id=?", (contact_id,)
        ).fetchone()
        return int(row[0])

    def get_pending_reminders(self, contact_id: str) -> list[str]:
        """List reminder IDs that can be cancelled by a lead response."""
        rows = self.database.execute(
            "SELECT reminder_id FROM reminders WHERE contact_id=? AND status='pending'",
            (contact_id,),
        ).fetchall()
        return [row[0] for row in rows]

    def is_do_not_contact(self, contact_id: str) -> bool:
        """Check the durable all-channel do-not-contact flag."""
        return (
            self.database.execute(
                "SELECT 1 FROM do_not_contact WHERE contact_id=?", (contact_id,)
            ).fetchone()
            is not None
        )

    def is_channel_allowed(self, contact_id: str, channel: str) -> bool:
        """Return the last channel preference, defaulting to allowed."""
        row = self.database.execute(
            "SELECT allowed FROM channel_preferences WHERE contact_id=? AND channel=?",
            (contact_id, channel),
        ).fetchone()
        return row is None or bool(row[0])

    def persist_effects(
        self,
        event: Event,
        orders: list[Order],
        output_dir: Path,
        origin: str,
        model: str,
        prompt_version: str,
        latency_ms: float,
        label=None,
        count_attempt: bool = False,
    ) -> None:
        """Apply order effects using append-before-commit recovery semantics."""
        order_path = output_dir / "orders.jsonl"
        with self.transaction() as database:
            if count_attempt:
                self._record_attempt(database, event)
            self._record_channel_preferences(database, event)
            for order in orders:
                if self.has_order(order.idempotency_key):
                    continue
                self._append_or_reconcile(order_path, order)
                database.execute(
                    "INSERT OR IGNORE INTO emitted_orders VALUES (?, ?)",
                    (order.idempotency_key, order.order_id),
                )
                self._apply_order_effect(database, event, order)
            self._record_cut_off(database, event, label)
            database.execute(
                """
                INSERT INTO processing_audit
                    (event_id, origin, model, prompt_version, latency_ms)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(event_id) DO UPDATE SET
                    origin=excluded.origin,
                    model=excluded.model,
                    prompt_version=excluded.prompt_version,
                    latency_ms=excluded.latency_ms
                """,
                (event.event_id, origin, model, prompt_version, latency_ms),
            )

    def persist_decision(
        self,
        event: Event,
        decision: dict,
        output_dir: Path,
        save_as_original: bool,
    ) -> None:
        """Append a decision and optionally register it as the original fact."""
        decision_path = output_dir / "decisions.jsonl"
        with self.transaction() as database:
            append_jsonl(decision_path, decision)
            if save_as_original:
                database.execute(
                    "INSERT OR IGNORE INTO processed_events VALUES (?, ?)",
                    (
                        event.idempotency_key,
                        json.dumps(decision, ensure_ascii=False),
                    ),
                )

    def has_order(self, idempotency_key: str) -> bool:
        """Check whether an order effect was already committed."""
        return (
            self.database.execute(
                "SELECT 1 FROM emitted_orders WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            is not None
        )

    def _record_attempt(self, database: sqlite3.Connection, event: Event) -> None:
        """Consume one attempt once per delivery event, including retries."""
        if event.type != "call.ended":
            return
        inserted = database.execute(
            "INSERT OR IGNORE INTO attempt_events VALUES (?, ?)",
            (event.event_id, event.lead.contact_id),
        ).rowcount
        if inserted:
            database.execute(
                """
                INSERT INTO attempts(contact_id, count) VALUES (?, 1)
                ON CONFLICT(contact_id) DO UPDATE SET count=count+1
                """,
                (event.lead.contact_id,),
            )

    def _record_channel_preferences(
        self, database: sqlite3.Connection, event: Event
    ) -> None:
        """Persist explicit WhatsApp consent or rejection found in call slots."""
        outcome = event.agent_outcome
        if outcome is None:
            return
        slots = outcome.slots_snapshot
        consent = slots.get("canal_consentido")
        rejected = slots.get("whatsapp_rechazado") is True or consent == "email"
        if rejected:
            database.execute(
                """
                INSERT INTO channel_preferences(contact_id, channel, allowed)
                VALUES (?, 'whatsapp', 0)
                ON CONFLICT(contact_id, channel) DO UPDATE SET allowed=0
                """,
                (event.lead.contact_id,),
            )
        elif consent == "whatsapp":
            database.execute(
                """
                INSERT INTO channel_preferences(contact_id, channel, allowed)
                VALUES (?, 'whatsapp', 1)
                ON CONFLICT(contact_id, channel) DO UPDATE SET allowed=1
                """,
                (event.lead.contact_id,),
            )

    def _record_cut_off(
        self, database: sqlite3.Connection, event: Event, label
    ) -> None:
        """Record cut-off labels for repeated-cut review detection."""
        if event.type != "call.ended" or label is None:
            return
        if getattr(label, "value", label) in {"cortada", "visita_sin_confirmar"}:
            database.execute(
                "INSERT OR IGNORE INTO cut_off_calls VALUES (?, ?)",
                (event.event_id, event.lead.contact_id),
            )

    def _apply_order_effect(
        self, database: sqlite3.Connection, event: Event, order: Order
    ) -> None:
        """Apply SQLite-side state coupled to an already appended order."""
        body = order.body
        if order.operation == "programar_recordatorio":
            database.execute(
                "INSERT OR IGNORE INTO reminders VALUES (?, ?, 'pending')",
                (order.order_id, event.lead.contact_id),
            )
        elif order.operation == "cancelar_recordatorio":
            database.execute(
                "UPDATE reminders SET status='cancelled' WHERE reminder_id=?",
                (body.get("reminder_id"),),
            )
        elif order.operation == "marcar_no_contactar":
            database.execute(
                "INSERT OR IGNORE INTO do_not_contact VALUES (?)",
                (event.lead.contact_id,),
            )

    @staticmethod
    def _append_or_reconcile(path: Path, order: Order) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        expected = order.external_dict()
        if path.exists():
            raw = path.read_bytes()
            valid_end = 0
            for line in raw.splitlines(keepends=True):
                next_end = valid_end + len(line)
                if not line.endswith(b"\n"):
                    break
                try:
                    candidate = json.loads(line)
                except json.JSONDecodeError:
                    break
                if candidate.get("idempotency_key") == order.idempotency_key:
                    if candidate != expected:
                        raise ValueError(
                            "order idempotency key has conflicting payload"
                        )
                    return
                valid_end = next_end
            if valid_end != len(raw):
                path.write_bytes(raw[:valid_end])
        append_jsonl(path, expected)
