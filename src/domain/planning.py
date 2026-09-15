"""Pure order planning for the closed campaign case catalogue."""

from datetime import timedelta
import hashlib
import json

from .cases import CaseLabel, STATUS_BY_LABEL
from models.event import Event
from models.orders import Order
from .scheduling import (
    add_business_days,
    next_attempt,
    next_cut_off_attempt,
    resolve_callback,
    to_local,
)


def build_order(
    event: Event, operation: str, body: dict, suffix: str | None = None
) -> Order:
    """Build a deterministic order ID from the event fact and operation."""
    key = f"{event.idempotency_key}:{operation}"
    if suffix:
        key += f":{suffix}"
    return Order(
        order_id=f"ord_{hashlib.sha1(key.encode()).hexdigest()[:8]}",
        event_id=event.event_id,
        operation=operation,
        idempotency_key=key,
        body=body,
    )


def _task_due(event: Event, config, days: int | None = None) -> str:
    """Return a timezone-aware task deadline from campaign configuration."""
    amount = (
        config.raw["tareas"]["vencimiento_por_defecto_dias"] if days is None else days
    )
    return (to_local(event.occurred_at, config) + timedelta(days=amount)).isoformat()


def _review_task(event: Event, reason: str, config) -> Order:
    """Create the internal manual-review task used by N4 and degradation."""
    return build_order(
        event,
        "crear_tarea",
        {
            "contact_id": event.lead.contact_id,
            "call_id": event.telephony.call_id if event.telephony else None,
            "tipo": "revisar_llamada",
            "titulo": "Revisar llamada",
            "detalle": reason,
            "vence_el": _task_due(event, config),
            "asignada_a": "cualquiera",
        },
        "review",
    )


def _whatsapp_allowed(event: Event, store) -> bool:
    """Apply durable DNC and explicit per-event WhatsApp consent rules."""
    if store.is_do_not_contact(event.lead.contact_id):
        return False
    slots = event.agent_outcome.slots_snapshot if event.agent_outcome else {}
    if (
        slots.get("whatsapp_rechazado") is True
        or slots.get("canal_consentido") == "email"
    ):
        return False
    return store.is_channel_allowed(event.lead.contact_id, "whatsapp")


def _close_order(
    event: Event, label: CaseLabel, reason: str, confidence: float
) -> Order:
    """Create the required queue-close order for a first-delivery call."""
    return build_order(
        event,
        "cerrar_llamada",
        {
            "entry_id": event.campaign.entry_id,
            "status": STATUS_BY_LABEL[label],
            "etiqueta": label.value,
            "motivo": reason,
            "confianza": confidence,
            "duration_seconds": (
                event.telephony.duration_seconds if event.telephony else 0
            ),
        },
    )


def plan_orders(
    event: Event,
    label: CaseLabel,
    reason: str,
    confidence: float,
    config,
    effective_attempt: int,
    store,
    extracted: dict | None = None,
    cut_off_count: int = 0,
) -> list[Order]:
    """Return validated-domain intents without performing side effects."""
    extracted = extracted or {}
    orders: list[Order] = []
    if event.type == "call.ended":
        orders.append(_close_order(event, label, reason, confidence))

    if label is CaseLabel.DO_NOT_CONTACT:
        orders.append(
            build_order(
                event,
                "marcar_no_contactar",
                {
                    "telefono": event.lead.phone,
                    "contact_id": event.lead.contact_id,
                    "canal": "todos",
                    "motivo": reason,
                    "origen": "call",
                },
            )
        )
        return orders

    dnc = store.is_do_not_contact(event.lead.contact_id)
    can_send_whatsapp = _whatsapp_allowed(event, store)
    max_attempts = config.raw["reintentos"]["max_intentos"]

    if label is CaseLabel.RESERVED_VISIT:
        appointment = event.agent_outcome.appointment if event.agent_outcome else None
        if appointment is None:
            raise ValueError("reserved visit is missing appointment data")
        margin = config.raw["tareas"]["confirmar_visita_margen_horas"]
        due_at = (
            to_local(appointment.start_time, config) - timedelta(hours=margin)
        ).isoformat()
        orders.append(
            build_order(
                event,
                "crear_tarea",
                {
                    "contact_id": event.lead.contact_id,
                    "call_id": event.telephony.call_id,
                    "tipo": "confirmar_visita_direccion",
                    "titulo": "Confirmar dirección de visita",
                    "detalle": event.lead.property_address
                    or "Dirección de la visita no disponible",
                    "vence_el": due_at,
                    "asignada_a": "comercial_asignado",
                },
            )
        )
        return orders

    if label is CaseLabel.DOCUMENTATION_SENT:
        if not dnc and can_send_whatsapp:
            orders.append(
                build_order(
                    event,
                    "programar_recordatorio",
                    {
                        "contact_id": event.lead.contact_id,
                        "canal": "whatsapp_lead",
                        "plantilla": "recordatorio_documentacion",
                        "cuando": (
                            to_local(event.occurred_at, config)
                            + timedelta(
                                hours=config.raw["recordatorios"][
                                    "documentacion_lead_horas"
                                ]
                            )
                        ).isoformat(),
                        "cancelar_si": "lead_responde",
                    },
                    "lead",
                )
            )
        orders.append(
            build_order(
                event,
                "programar_recordatorio",
                {
                    "contact_id": event.lead.contact_id,
                    "canal": "tarea_comercial",
                    "tipo_tarea": "llamar_a_mano",
                    "cuando": add_business_days(
                        event.occurred_at,
                        config.raw["recordatorios"][
                            "seguimiento_comercial_dias_habiles"
                        ],
                        config,
                    ).isoformat(),
                    "cancelar_si": "lead_responde",
                },
                "commercial",
            )
        )
        return orders

    if label is CaseLabel.DOCUMENTATION_PENDING:
        if not dnc:
            orders.append(
                build_order(
                    event,
                    "crear_tarea",
                    {
                        "contact_id": event.lead.contact_id,
                        "call_id": event.telephony.call_id,
                        "tipo": "enviar_documentacion_email",
                        "titulo": "Enviar documentación por email",
                        "detalle": extracted.get(
                            "declared_email", "Email declarado en llamada"
                        ),
                        "vence_el": _task_due(event, config),
                        "asignada_a": "comercial_asignado",
                    },
                )
            )
        return orders

    if label is CaseLabel.WRONG_PERSON:
        orders.append(
            build_order(
                event,
                "crear_tarea",
                {
                    "contact_id": event.lead.contact_id,
                    "call_id": event.telephony.call_id,
                    "tipo": "verificar_telefono",
                    "titulo": "Verificar teléfono del lead",
                    "detalle": reason,
                    "vence_el": _task_due(event, config),
                    "asignada_a": "comercial_asignado",
                },
            )
        )
        return orders

    if label is CaseLabel.DISCARDED:
        return orders

    if label is CaseLabel.OTHER:
        orders.append(_review_task(event, reason, config))
        return orders

    if dnc or effective_attempt > max_attempts:
        return orders

    if label is CaseLabel.CALLBACK:
        raw = extracted.get("callback_when_raw") or ""
        resolution = resolve_callback(
            raw,
            event.occurred_at,
            config,
            callback_day=extracted.get("callback_day"),
            callback_time=extracted.get("callback_time"),
        )
        orders.append(
            build_order(
                event,
                "programar_llamada",
                {
                    "entry_id": event.campaign.entry_id,
                    "telefono": event.lead.phone,
                    "no_antes_de": resolution.scheduled.isoformat(),
                    "motivo": "callback solicitado",
                    "nota_contexto": reason,
                },
            )
        )
        if resolution.adjusted and can_send_whatsapp:
            orders.append(
                build_order(
                    event,
                    "enviar_plantilla_whatsapp",
                    {
                        "organization_id": event.organization_id,
                        "telefono": event.lead.phone,
                        "plantilla": "aviso_cambio_hora",
                        "parametros": {},
                        "idioma": event.lead.language,
                    },
                )
            )
        return orders

    if label is CaseLabel.REJECTED:
        if can_send_whatsapp:
            orders.append(
                build_order(
                    event,
                    "enviar_plantilla_whatsapp",
                    {
                        "organization_id": event.organization_id,
                        "telefono": event.lead.phone,
                        "plantilla": "primer_toque_respaldo",
                        "parametros": {},
                        "idioma": event.lead.language,
                    },
                )
            )
        return orders

    if label in {CaseLabel.VOICEMAIL, CaseLabel.NO_ANSWER, CaseLabel.BUSY}:
        if effective_attempt >= max_attempts:
            if can_send_whatsapp:
                orders.append(
                    build_order(
                        event,
                        "enviar_plantilla_whatsapp",
                        {
                            "organization_id": event.organization_id,
                            "telefono": event.lead.phone,
                            "plantilla": "primer_toque_respaldo",
                            "parametros": {},
                            "idioma": event.lead.language,
                        },
                    )
                )
            return orders
        if label is CaseLabel.BUSY:
            when = next_attempt(
                event.occurred_at,
                config,
                config.raw["reintentos"]["ocupado_minutos_min"],
                config.raw["reintentos"]["ocupado_minutos_max"],
            )
        else:
            when = next_attempt(
                event.occurred_at,
                config,
                config.raw["reintentos"]["separacion_minima_horas"] * 60,
            )
        orders.append(
            build_order(
                event,
                "programar_llamada",
                {
                    "entry_id": event.campaign.entry_id,
                    "telefono": event.lead.phone,
                    "no_antes_de": when.isoformat(),
                    "motivo": reason,
                },
            )
        )
        return orders

    if label in {CaseLabel.CUT_OFF, CaseLabel.VISIT_UNCONFIRMED}:
        if cut_off_count >= 1:
            orders.append(_review_task(event, "Segundo corte de llamada", config))
        if effective_attempt >= max_attempts:
            return orders
        when, adjusted = next_cut_off_attempt(
            event.occurred_at,
            config,
            config.raw["reintentos"]["cortada_minutos_min"],
            config.raw["reintentos"]["cortada_horas_max"] * 60,
        )
        context = json.dumps(
            event.agent_outcome.slots_snapshot if event.agent_outcome else {},
            ensure_ascii=False,
            sort_keys=True,
        )
        if adjusted:
            context += "; ajuste_fuera_de_limite"
        orders.append(
            build_order(
                event,
                "programar_llamada",
                {
                    "entry_id": event.campaign.entry_id,
                    "telefono": event.lead.phone,
                    "no_antes_de": when.isoformat(),
                    "motivo": reason,
                    "nota_contexto": context,
                },
            )
        )
        return orders

    return orders
