# Plan: Orquestador post-llamada (reto Kontaktu)

> Adaptación del plan genérico "Voice Call CRM Orchestrator" al enunciado real de
> `reto-kontaktu/`. Lo que cambia respecto al plan original está marcado con **[Δ]**.

## 0. Correcciones de partida respecto al plan original

| Plan original                                        | Realidad del reto                                                                                                                                                |
| ---------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Repo nuevo e independiente                           | **[Δ]** Mismo repo. El enunciado exige historial de git intacto empezando por el commit del zip. Ya existe (`9cadcfc first commit`). Solo se añade código encima |
| Servicio que recibe eventos (cola/servidor)          | **[Δ]** CLI: `python run.py <ruta-evento>`. Un proceso por evento, nada en memoria entre eventos. Sin red salvo el modelo                                        |
| `GPT-5.6 Luna`, proveedor por confirmar              | **[Δ]** OpenAI, `gpt-5.6-luna`, clave en `.env.local`. Modelo de coste sensible con Structured Outputs; el identificador no se hardcodea en la lógica |
| Catálogo provisional de 11 casos                     | **[Δ]** Catálogo cerrado: **15 casos, 13 etiquetas de caso + `otro` + `no_aplica`**. El enum está fijado en `decision.schema.json`. No se inventa nada           |
| "Órdenes de dominio" genéricas                       | **[Δ]** Las **7 operaciones** exactas de `crm-openapi.yaml`, escritas como líneas en `output/orders.jsonl`                                                       |
| Almacenamiento de idempotencia por confirmar         | **[Δ]** SQLite local (único permitido). Postgres/Redis prohibidos                                                                                                |
| §10 Evaluación experimental comparando arquitecturas | **[Δ] Fuera de alcance.** Tope de 4 h. Se sustituye por un replay determinista del lote + fichero dorado (§10 nueva)                                             |
| §11 Observabilidad completa                          | **[Δ]** Reducida a logs a `stderr` + una tabla `procesamientos` en SQLite                                                                                        |

Lo que **sí** se conserva del plan original: la separación LLM-clasifica / código-decide, los
contratos Pydantic, la idempotencia por clave determinista, y el grafo explícito de LangGraph.

## 1. Objetivo

Un programa que recibe **un evento** y emite **una decisión** y **cero o más órdenes**:

```
python run.py reto-kontaktu/eventos/01-call-ended-nuria.json
```

- Entrada: ruta de un fichero de evento, único argumento.
- Salida: append en `output/decisions.jsonl` y `output/orders.jsonl`.
- Código de salida: `0` si procesó el evento, `≠0` si no pudo.
- Estado entre procesos: `state/orchestrator.sqlite`.

Se evalúa contra **un segundo lote que no tenemos**, desde cero, con casos sin ejemplo
(`rechazada`, `callback` fuera de ventana, `descartado`) y variantes con otras horas y días.
Todo lo que se ajuste "a ojo" a los 16 eventos de muestra es deuda que se paga en la evaluación.

**El criterio de diseño dominante:** la evaluación es revisión exhaustiva de código centrada en
el manejo de LangGraph. El grafo tiene que ser un grafo de verdad —rutas condicionales que
existen porque el dominio las necesita— y no una cadena lineal disfrazada.

## 2. Decisiones de arquitectura

### 2.1 Dónde entra el LLM y dónde no

El ejemplo resuelto lo dice explícitamente: _"Llamar al modelo aquí es gastar dinero y latencia
para nada"_. Regla:

**Sin LLM (señalización + `agent_outcome`, que el enunciado declara fiable):**

| Señal                                                | Etiqueta                                 |
| ---------------------------------------------------- | ---------------------------------------- |
| `sip 486`                                            | `ocupado`                                |
| `sip 408` / `480`                                    | `sin_respuesta`                          |
| `sip 603`                                            | `rechazada`                              |
| `sip 5xx`                                            | `otro` (N4)                              |
| `amd.result` ∈ {`machine-vm`, `machine-unavailable`} | `buzon` (da igual `amd.source`: caso 13) |
| `amd.result` = `machine-ivr`                         | `otro` (N4)                              |
| `agent_outcome.appointment` presente                 | `visita_reservada`                       |
| `agent_outcome.call_outcome` = `dnc`                 | `no_contactar`                           |
| `agent_outcome.call_outcome` = `callback_requested`  | `callback`                               |
| `agent_outcome.reason` = `wrong_person`              | `persona_equivocada`                     |
| `transcript` vacío / `quality.no_conversation`       | nunca llega al LLM                       |

`amd.result` = `uncertain` se trata como persona. El orden de precedencia importa: AMD de buzón
manda sobre un `200 OK`; `appointment` manda sobre todo lo conversacional.

**Con LLM (solo si hay conversación y la señalización no cierra el caso):** distinguir
`cortada` / `visita_sin_confirmar` / `documentacion_enviada` / `documentacion_pendiente` /
`descartado` / `no_contactar` / `persona_equivocada` / `otro`, y extraer los datos que el
código necesita para calcular fechas (el `callback_when_raw` tipo _"mañana a las seis"_, el
consentimiento de canal, el email declarado, la visita acordada de palabra).

En el lote visible, solo `evt_04` y `evt_11` necesitan clasificación conversacional adicional; los
demás eventos se resuelven mediante señalización, `agent_outcome`, mensaje, reentrega u organización.
El modelo no se invoca en eventos que ya tienen una señal determinista suficiente.

### 2.2 El LLM extrae, el código decide

El modelo devuelve etiqueta + confianza + motivo + datos extraídos. **Nunca** devuelve fechas
calculadas, ni órdenes, ni nombres de operación. La aritmética de `Europe/Madrid`, ventana de
llamadas, días hábiles, plazos y recuento de intentos vive en código determinista y testeable.

El modelo interpreta _"mañana a las seis"_ → `{fecha_relativa: "mañana", hora: "18:00"}`;
el código lo ancla a `occurred_at`, lo resuelve a `2026-09-16T18:00:00+02:00`, comprueba la
ventana y, si cae fuera, mueve a la primera franja válida y añade `aviso_cambio_hora` (caso 12).

### 2.3 Modelo

`gpt-5.6-luna`, `temperature=0`, `reasoning_effort=none`, **Structured Outputs** (`response_format` con `json_schema`
estricto, vía `with_structured_output` de `langchain-openai`).

Justificación para el README: la parte difícil ya está resuelta de forma determinista; al
modelo le queda una tarea de contexto corto, enum cerrado y extracción de slots. Es el punto
barato/rápido de la curva y soporta salida estructurada estricta. Si en el replay falla
sistemáticamente el par `cortada` / `visita_sin_confirmar`, se sube a `gpt-4.1-mini` cambiando
solo `MODEL` en `.env.local` — el nombre del modelo no se hardcodea en ningún sitio.

### 2.4 Definición declarativa del clasificador

Se adopta conceptualmente el patrón de `definition.json` usado por otros agentes LangGraph del
entorno: una definición versionada declara el rol, el modelo, el prompt y el contrato de salida;
el runtime la carga y construye el componente ejecutable.

En este reto la definición describe **un clasificador**, no una colección de agentes
conversacionales. No se copian jerarquías de país/portal ni se introducen `AgentsRouter`,
`AgentWithTools` o herramientas invocables por el LLM. El grafo de negocio sigue siendo el de
§4 y sus ramas permanecen explícitas en Python.

La definición puede contener:

- nombre y descripción del clasificador;
- variable de entorno del modelo y temperatura;
- ruta y versión del prompt local;
- nombre del schema Pydantic de salida;
- catálogo de etiquetas conversacionales que el LLM puede devolver.

No puede contener órdenes, fechas calculadas, reglas de reintento, transiciones del grafo ni
acciones con efectos secundarios. Esos elementos siguen siendo responsabilidad de
`classifier.py`, `time.py`, `planner.py` y `persistence.py`.

Ejemplo conceptual:

```json
{
  "name": "call_outcome_classifier",
  "description": "Clasifica el resultado conversacional de una llamada",
  "type": "structured_classifier",
  "model": { "env": "MODEL", "temperature": 0 },
  "prompt": { "path": "prompts/classification_v1.md", "version": "v1" },
  "output": {
    "schema": "Clasificacion",
    "labels": [
      "callback",
      "cortada",
      "visita_sin_confirmar",
      "documentacion_enviada",
      "documentacion_pendiente",
      "descartado",
      "no_contactar",
      "persona_equivocada",
      "otro"
    ]
  }
}
```

`client.py` valida/carga esta definición y crea el modelo; `classifier.py` aplica el prompt y
`with_structured_output(Clasificacion)`. La señalización determinista no pasa por esta
definición: se resuelve antes, en `clasificar_senalizacion`.

## 3. Estructura del repo

```text
Call-Event-Challenge/
├── run.py                       # entrypoint: un evento, un proceso
├── pyproject.toml
├── README.md                    # una página: ejecución, recortes, verificación
├── PLAN.md                      # este documento
├── .env.example                 # configuración de referencia, versionado
├── .env.local                   # configuración local, no versionado
├── prompts/                     # ENTREGABLE OBLIGATORIO
│   ├── classification_v1.md     # system + user template, versionado
│   └── CHANGELOG.md             # qué cambió y por qué entre versiones
├── src/
│   ├── config.py                # carga campana.yaml + .env.local, resuelve rutas
│   ├── dominio/
│   │   ├── event.py             # modelos Pydantic del evento
│   │   ├── cases.py             # enum de 15 etiquetas + tabla etiqueta→status
│   │   ├── classification.py    # schema de salida del LLM
│   │   └── orders.py            # 7 operaciones, cuerpo validado por operación
│   ├── time.py                  # ventana, días hábiles, plazos, Europe/Madrid
│   ├── grafo/
│   │   ├── state.py             # OrchestratorState (TypedDict)
│   │   ├── nodes.py
│   │   └── workflow.py          # StateGraph + aristas condicionales
│   ├── llm/
│   │   ├── definition.json      # contrato declarativo del clasificador; no contiene negocio
│   │   ├── client.py            # carga definition.json, modelo desde env, timeout, reintentos
│   │   └── classifier.py        # prompt + structured output + fallback
│   ├── planner.py               # etiqueta + contexto → lista de órdenes
│   ├── persistence.py           # SQLite: intentos, recordatorios, dnc, idempotencia
│   └── salida.py                # append atómico a los dos .jsonl
├── tools/
│   ├── replay.py                # borra state+output y corre orden.txt entero
│   └── check.py                 # valida output contra esquemas y contra expected.yaml
├── tests/
│   └── esperado.yaml            # etiquetas y órdenes esperadas por evento (fichero dorado)
├── state/                       # sqlite, no versionado
├── output/                      # jsonl, no versionado
└── reto-kontaktu/               # INTOCABLE: eventos/, config/, esquemas/
```

**Resolución de rutas:** `config/campana.yaml` y `esquemas/` se localizan como hermanos del
directorio que contiene el evento (`<base>/eventos/xx.json` → `<base>/config/campana.yaml`),
con `KONTAKTU_BASE` como override. Así funciona tanto desde la raíz del repo como desde dentro
de `reto-kontaktu/`, que es como lo van a ejecutar ellos.

## 4. El grafo LangGraph

```text
                        cargar_evento
                              │
                        triar_evento ──────────────────┐
              ┌───────────┬───┴────┬──────────────┐    │
        (org ajena)  (reentrega) (mensaje)   (call.ended)
              │           │         │              │
              │           │         │      clasificar_senalizacion
              │           │         │              │
              │           │         │      ┌───────┴────────┐
              │           │         │  (cerrado)      (necesita transcript)
              │           │         │      │                │
              │           │         │      │         clasificar_llm
              │           │         │      │                │
              │           │         │      └───────┬────────┘
              │           │         └──────────────┤
              │           │                   planificar
              │           │                        │
              │           │                  validar_ordenes
              │           │                        │
              │           │              ┌─────────┴─────────┐
              │           │          (válidas)          (inválidas)
              │           │              │                   │
              │           │          ejecutar          degradar_a_revision
              │           │              │                   │
              └───────────┴──────────────┴─────────┬─────────┘
                                          persistir_decision
                                                 END
```

### Nodos

| Nodo                      | Responsabilidad                                                                                                                                                                                                                                  |
| ------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `cargar_evento`           | Lee el JSON, valida contra `evento.schema.json`, construye el modelo Pydantic y aplica validación condicional según `type`: `call.ended` requiere los bloques de llamada y `message.received` requiere `message` y no procesa bloques de llamada |
| `triar_evento`            | Decide la rama: `organization_id` ≠ la de `campana.yaml` → ajeno; `idempotency_key` ya procesada → reentrega; `type` = `message.received` → mensaje; resto → llamada                                                                             |
| `rama_org_ajena`          | Etiqueta `no_aplica`, cero órdenes. **Ni siquiera `cerrar_llamada`** (R6)                                                                                                                                                                        |
| `rama_reentrega`          | Recupera de SQLite la decisión original por `idempotency_key`, **repite su etiqueta**, cero órdenes nuevas (R5)                                                                                                                                  |
| `rama_mensaje`            | `no_aplica` + un `cancelar_recordatorio` por cada recordatorio pendiente del lead, con los `reminder_id` que generamos nosotros en otro proceso (R7)                                                                                             |
| `clasificar_senalizacion` | Clasificador determinista (§2.1). Devuelve etiqueta cerrada o `None`                                                                                                                                                                             |
| `clasificar_llm`          | Solo si hay transcripción y la señalización no cerró. Carga la definición del clasificador y devuelve salida estructurada estricta; no invoca herramientas                                                                                       |
| `planificar`              | Tabla etiqueta → órdenes, aplicando intentos, ventana, plazos y N1–N5                                                                                                                                                                            |
| `validar_ordenes`         | Pydantic por operación + invariantes (§7)                                                                                                                                                                                                        |
| `degradar_a_revision`     | Si la validación falla o el LLM no responde: `otro` + `revisar_llamada` + `cerrar_llamada` con `needs_review`. Nunca una orden a medias                                                                                                          |
| `ejecutar`                | Escribe `ordenes.jsonl` y persiste efectos (intentos, `reminder_id`, dnc, cierre)                                                                                                                                                                |
| `persistir_decision`      | Escribe la línea de `decisiones.jsonl`. **Todas las ramas pasan por aquí**, incluidas las de cero órdenes                                                                                                                                        |

### Por qué estas aristas condicionales y no otras

- El triaje es una arista de 4 salidas porque son cuatro contratos de salida distintos, no
  cuatro `if` dentro de un nodo. Un revisor tiene que poder leer `workflow.py` y ver el dominio.
- La bifurcación señalización/LLM es la que materializa la decisión de coste de §2.1.
- `degradar_a_revision` es el nodo que garantiza R8 y N4 a la vez: cualquier fallo tiene un
  destino seguro que no toca el CRM de forma incorrecta.

### Checkpointer

`SqliteSaver` usa el mismo fichero SQLite, pero el `thread_id` es `event_id` (la entrega que se
está procesando), no `idempotency_key`. Así, una reejecución de la misma entrega puede retomar el
grafo, mientras que una reentrega real —nuevo `event_id`, misma `idempotency_key`— entra en una
ejecución nueva y pasa por la rama `reentrega`.

La garantía de no duplicar órdenes sigue siendo el índice único de §6 sobre la clave de orden;
el checkpointer solo evita repetir nodos cuando el proceso muere a mitad de una entrega. Nunca
se usa el estado del checkpointer para decidir si un hecho ya fue procesado: esa decisión la toma
SQLite mediante `eventos_procesados`.

## 5. Estado del grafo

```python
class EstadoOrquestador(TypedDict):
    ruta_evento: str
    evento: Evento | None
    config: ConfigCampana
    ruta: Literal["llamada", "mensaje", "reentrega", "org_ajena"] | None
    contexto: ContextoLead        # intentos previos, recordatorios pendientes, dnc, cortadas previas
    clasificacion: Clasificacion | None
    origen_clasificacion: Literal["senalizacion", "llm", "reentrega", "degradado"] | None
    ordenes: list[Orden]
    ordenes_emitidas: list[str]   # orden_id
    decision: Decision | None
    error: ErrorProceso | None
```

`ContextoLead` se carga una sola vez, en `triar_evento`, y es lo único que el proceso sabe del
pasado. Todo lo que el planificador necesite recordar tiene que estar ahí.

## 6. Persistencia (SQLite)

| Tabla                | Para qué                                                                                                                                         | Clave                                |
| -------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------ | ------------------------------------ |
| `eventos_procesados` | Detectar reentregas y guardar la decisión original completa serializada, incluyendo etiqueta, motivo, confianza, `call_id` y `orden_id` emitidos | `idempotency_key` UNIQUE             |
| `ordenes_emitidas`   | Garantía dura de no duplicar                                                                                                                     | `idempotency_key` de la orden UNIQUE |
| `intentos`           | Contar intentos de voz por lead                                                                                                                  | `contact_id`                         |
| `recordatorios`      | `reminder_id` generados, estado pendiente/cancelado                                                                                              | `reminder_id`                        |
| `no_contactar`       | Bajas registradas                                                                                                                                | `contact_id` / `telefono`            |
| `llamadas_cortadas`  | Historial de `cortada`/`visita_sin_confirmar` por lead, para N4                                                                                  | `contact_id`                         |
| `procesamientos`     | Auditoría: modelo, versión de prompt, latencia, origen de la clasificación                                                                       | `event_id`                           |

**Intentos (R4):** cuenta todo `call.ended` procesado por primera vez, conteste o no. Una
reentrega no cuenta. `triar_evento` carga `intentos_previos`; para planificar se usa siempre
`intento_efectivo = intentos_previos + 1`. La persistencia de ese incremento se hace de forma
idempotente junto con los efectos del evento, antes de confirmar la transacción. Así el intento
actual ya está consumido cuando se decide si quedan reintentos. La traza de Nuria lo fija:
`evt_01` (intento 1, 480) → reintento; `evt_05` (intento 2, 480) → reintento;
`evt_12` (intento 3, buzón) → agotados → canal de respaldo.

**Orden de escritura:** transacción SQLite abierta → insertar orden → append a `ordenes.jsonl`
→ commit. Si el proceso muere antes del commit, la línea puede quedar en el fichero sin fila;
en la reejecución el índice único la bloquea y `ejecutar` comprueba la cola del fichero antes
de reescribir. Si la línea no está completa o no contiene el mismo `orden_id`, se descarta la
línea incompleta antes de volver a escribirla. El efecto se confirma en SQLite solo después de
que la línea válida esté en el fichero. Es el compromiso razonable sin dos-fases.

La decisión se construye y se registra por entrega: una reentrega deja una nueva línea de
`decisiones.jsonl`, pero recupera de `eventos_procesados` la decisión original y no genera órdenes.
Una repetición deliberada del replay también añade nuevas líneas de decisión; la deduplicación
solo aplica a órdenes y efectos de dominio. Si un proceso muere después de escribir una decisión,
la reejecución puede dejar otra línea para esa entrega; las órdenes y los contadores siguen siendo
idempotentes y no se duplican.

**`orden_id` (verificado contra el ejemplo resuelto):**

```python
orden_id = "ord_" + hashlib.sha1(idempotency_key_de_la_orden.encode()).hexdigest()[:8]
```

`sha1("lk-out-0306:cerrar_llamada")[:8]` = `96decc21` → `ord_96decc21`, que es exactamente el
del ejemplo. Determinista, estable entre ejecuciones y reproducible por el evaluador.

**Claves de idempotencia de órdenes:** `<idempotency_key del evento>:<operacion>`, más
discriminante cuando un evento emite dos órdenes de la misma operación:
`…:programar_recordatorio:lead` y `…:programar_recordatorio:comercial` (caso 2),
`…:cancelar_recordatorio:<reminder_id>` (R7).

## 7. Tabla etiqueta → órdenes

Toda etiqueta de llamada procesada por primera vez emite `cerrar_llamada` con el `status` que
fija la tabla de `casos.md`. Encima de eso:

| Etiqueta                  | Órdenes adicionales                                                                                                                                                                                                                                      |
| ------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `visita_reservada`        | `crear_tarea` `confirmar_visita_direccion`, vence a `start_time − confirmar_visita_margen_horas`                                                                                                                                                         |
| `documentacion_enviada`   | `programar_recordatorio` ×2: lead (`whatsapp_lead`, `recordatorio_documentacion`, +48 h naturales, `cancelar_si: lead_responde`) y comercial (`tarea_comercial`, `llamar_a_mano`, +3 días hábiles)                                                       |
| `documentacion_pendiente` | `crear_tarea` `enviar_documentacion_email`. **Ningún WhatsApp** (N1)                                                                                                                                                                                     |
| `callback`                | `programar_llamada` al momento pedido; si cae fuera de ventana → primera franja válida + `enviar_plantilla_whatsapp` `aviso_cambio_hora` (caso 12, sujeto a N1). Si WhatsApp fue rechazado, no se envía la plantilla ni se programa un canal alternativo |
| `sin_respuesta`           | `programar_llamada` a `+separacion_minima_horas`                                                                                                                                                                                                         |
| `ocupado`                 | `programar_llamada` entre `ocupado_minutos_min` y `max` (el plazo específico gana)                                                                                                                                                                       |
| `buzon`                   | Si quedan intentos: `programar_llamada` a `+separacion_minima_horas`. Si no: canal de respaldo                                                                                                                                                           |
| `cortada`                 | `programar_llamada` con plazos de cortada (`+30 min`, tope `+4 h`), lo antes posible dentro de la ventana, con `nota_contexto` arrastrando los slots ya recogidos                                                                                        |
| `visita_sin_confirmar`    | Igual que `cortada`. **No se reserva la visita** (N5)                                                                                                                                                                                                    |
| `persona_equivocada`      | `crear_tarea` `verificar_telefono`. Sin reintentos                                                                                                                                                                                                       |
| `no_contactar`            | `marcar_no_contactar` con `canal: todos`. **Nada más** (N2)                                                                                                                                                                                              |
| `rechazada`               | Canal de respaldo directamente. Sin reintento por voz                                                                                                                                                                                                    |
| `descartado`              | Nada más allá de `cerrar_llamada`                                                                                                                                                                                                                        |
| `otro`                    | `crear_tarea` `revisar_llamada` con el motivo (N4)                                                                                                                                                                                                       |

**Canal de respaldo (N3):** `enviar_plantilla_whatsapp` con `primer_toque_respaldo`, sujeto a
N1. Se aplica cuando se agotan los intentos de voz —no solo en `buzon`: `sin_respuesta` y
`ocupado` en el intento 3 también van al respaldo— y en `rechazada` sin consumir intentos.
Si el canal configurado es WhatsApp pero el lead lo rechazó, no se emite plantilla ni se programa
otro contacto: queda únicamente `cerrar_llamada` y el motivo deja constancia de que el respaldo
fue bloqueado por N1.

**N4 con memoria:** `otro` siempre, y además una **segunda** `cortada`/`visita_sin_confirmar`
del mismo lead (de ahí la tabla `llamadas_cortadas`). La tarea `revisar_llamada` se suma a lo
que toque por la etiqueta, no la sustituye.

**Invariantes que comprueba `validar_ordenes`** (y que son la red de seguridad real):

1. Todo `no_antes_de` cae dentro de la ventana de `campana.yaml`, con offset explícito.
2. Si el lead ya está en `no_contactar`, cero órdenes de contacto saliente. Las órdenes internas
   de CRM de mantenimiento, como `marcar_no_contactar` para registrar la petición actual o
   `cancelar_recordatorio`, sí están permitidas.
3. Si el lead rechazó WhatsApp, ninguna orden de canal `whatsapp_lead` ni
   `enviar_plantilla_whatsapp`.
4. Con `etiqueta` = `no_contactar`, la única orden además de `cerrar_llamada` es
   `marcar_no_contactar`.
5. Ninguna `programar_llamada` si los intentos están agotados.
6. `cerrar_llamada` presente en todo `call.ended` de primera entrega, ausente en reentrega y
   en org ajena.
7. `status` de `cerrar_llamada` coincide con la tabla de `casos.md` para esa etiqueta.

## 8. Aritmética de fechas (`time.py`)

El módulo con más superficie de error y donde el segundo lote va a apretar ("otras horas y
otros días"). Funciones:

- `en_ventana(dt) -> bool` — extremos inclusive; domingo siempre falso.
- `primera_franja_valida(dt) -> datetime` — si `dt` está fuera, avanza a la apertura siguiente.
- `proximo_intento(desde, minimo, maximo=None)` — aplica el plazo y luego la ventana. El plazo
  específico (ocupado, cortada) gana al general. Para `ocupado`, si hay rango, el instante base
  determinista es el punto medio entero entre mínimo y máximo: con 30–90 minutos, `+60` minutos.
  Para `cortada`, se busca el primer instante válido desde `+30` minutos hasta `+4` horas; si
  ninguna franja de ese intervalo es válida, se usa la siguiente apertura válida y se registra
  `ajuste_fuera_de_limite` en el motivo o `nota_contexto`.
- `sumar_dias_habiles(dt, n)` — L–V. **El sábado se puede llamar pero no es día hábil**: la
  configuración lo avisa expresamente.
- `vencimiento_tarea(etiqueta, cita)` — `confirmar_visita_direccion` vence a
  `start_time − margen`; el resto, `+vencimiento_por_defecto_dias` naturales.
- `resolver_callback(raw, ancla)` — traduce lo que extrajo el LLM a instante absoluto.

Todo con `zoneinfo.ZoneInfo("Europe/Madrid")` y salida con offset explícito. Nada de `utcnow()`
ni de la hora del sistema: **el instante de referencia siempre es `occurred_at` del evento**,
nunca "ahora" — si no, el mismo evento da resultados distintos según cuándo se procese.

## 9. Prompts (entregable obligatorio)

`prompts/clasificacion_v1.md`, versionado y cargado desde fichero, nunca inline en el código.
Contenido:

- Rol y contexto mínimo (campaña de primer toque inmobiliario, en español).
- El catálogo **solo de las etiquetas que el LLM puede devolver** (las conversacionales). Las
  que decide la señalización no se le ofrecen: reducir el enum reduce el error.
- Las distinciones que el propio `casos.md` marca como peligrosas: 7 vs 8 (si se llegó a
  acordar visita, no cómo se cortó), 9 vs 10 (un número equivocado no es una baja),
  14 (documentación con WhatsApp rechazado).
- Instrucción explícita de no calcular fechas: extraer el texto crudo.
- Few-shot corto con los casos frontera, no con los fáciles.
- El JSON Schema de salida va en `with_structured_output`, no en el prompt.

`src/agents/call_outcome_classifier/definition.json` referencia este prompt y fija el catálogo conversacional
que puede devolver el LLM. La definición no sustituye al prompt versionado ni duplica las reglas
del dominio; funciona como configuración del runtime, de forma análoga al patrón conceptual de
los agentes existentes.

`prompts/CHANGELOG.md` registra qué cambió entre versiones y qué evento del replay lo motivó.
La versión del prompt se guarda en `procesamientos` para poder auditar decisiones pasadas.

## 10. Verificación (sustituye a la evaluación experimental del plan original)

Sin tests formales —no los piden— pero con un método reproducible que contar en el README:

1. **`tools/replay.py`**: hace un reset explícito y destructivo únicamente de los artefactos
   generados esperados (`state/orchestrator.sqlite`, `output/decisions.jsonl` y
   `output/orders.jsonl`), crea las carpetas si faltan, y recorre `eventos/orden.txt` lanzando
   **un subproceso por evento**. No elimina otros ficheros de `state/` o `output/` sin enumerarlos.
   El README documenta este reset y su alcance.
2. **`tests/expected.yaml`**: fichero dorado con etiqueta y órdenes esperadas por evento,
   escrito **leyendo `casos.md`, no la salida del programa**. Incluye los tres casos sin
   ejemplo mediante eventos sintéticos propios en `tests/extra_events/` (603, callback a
   las 22:00, lead que ya compró), que no tocan `eventos/`.
3. **`tools/check.py`**: valida cada línea contra `decision.schema.json`, cada
   cuerpo de orden contra el OpenAPI, y contrasta con `esperado.yaml`. Comprueba además los
   invariantes de §7 sobre la salida completa.
4. **Pruebas de propiedades del calendario**: `time.py` contra un barrido de instantes
   (cada 30 min durante dos semanas, incluyendo sábados y domingos), verificando que ninguna
   fecha devuelta cae fuera de la ventana.
5. **Prueba de reentrega**: correr el lote dos veces seguidas sin borrar estado. La segunda
   pasada debe producir 16 decisiones nuevas y **cero órdenes nuevas**. Cada nueva entrega usa
   su propio `event_id` como `thread_id`; la clave de orden basada en `idempotency_key` impide
   duplicados.
6. **Prueba de fallo**: corromper un evento a mitad de `orden.txt`; el proceso sale con código
   ≠0 y los siguientes se procesan con normalidad y con el estado intacto (R8).
7. **`visor/index.html`** con la carpeta `output/` para la revisión visual final.

Criterios de aceptación: los 16 eventos con la etiqueta esperada, cero órdenes fuera de la
tabla de §7, cero duplicados en la segunda pasada, cero fechas fuera de ventana.

## 11. Fases con presupuesto de 4 horas

| Fase                      | Tiempo | Contenido                                                                                                                             |
| ------------------------- | ------ | ------------------------------------------------------------------------------------------------------------------------------------- |
| 0. Andamiaje              | 20 min | `pyproject`, config, carga de `campana.yaml`, `run.py` que valida y escribe una decisión vacía                                        |
| 1. `time.py`              | 40 min | Ventana, días hábiles, plazos. Con el barrido de propiedades. **Es lo que más caro sale si se deja para el final**                    |
| 2. Dominio + persistencia | 40 min | Modelos, enum de casos, tabla de status, esquema SQLite, `orden_id` e idempotencia                                                    |
| 3. Grafo + señalización   | 45 min | `StateGraph`, triaje, clasificador determinista, planificador, `ejecutar`. **A esta altura los 9 eventos sin LLM ya salen correctos** |
| 4. LLM                    | 40 min | Prompt versionado, structured output, fallback a `degradar_a_revision`                                                                |
| 5. Reglas finas           | 30 min | N1–N5, casos 12/13/15, canal de respaldo, cancelación de recordatorios                                                                |
| 6. Verificación y README  | 25 min | Replay, fichero dorado, comprobador, README de una página                                                                             |

**Si el tiempo se acaba:** la fase 4 es la sacrificable. Un sistema que resuelve bien todo lo
determinista y manda lo conversacional a `otro` + `revisar_llamada` es un sistema honesto y se
explica en el README; uno que clasifica bonito pero programa llamadas en domingo, no.

## 12. Entregables del enunciado

| #   | Requisito                                        | Cómo se cumple                                                                                         |
| --- | ------------------------------------------------ | ------------------------------------------------------------------------------------------------------ |
| 1   | Código                                           | El repo                                                                                                |
| 2   | Historial de git intacto desde el commit del zip | Ya está (`9cadcfc`). Solo commits encima, nunca rebase ni amend sobre él                               |
| 3   | Prompts versionados                              | `prompts/`. Además, este `PLAN.md` y la conversación con el asistente, que el enunciado dice que suman |
| 4   | README de una página                             | Ejecución, modelo elegido y por qué, qué se dejó fuera (§0 y §11), cómo se verificó (§10)              |

## 13. Riesgos y puntos a vigilar

1. **El instante de referencia.** Usar `datetime.now()` en cualquier punto rompe la
   reproducibilidad y el segundo lote (otros días). Siempre `occurred_at`.
2. **Cuándo se incrementa el intento.** Si se cuenta después de planificar, Nuria recibe un
   cuarto intento que no existe.
3. **El 486 disfrazado.** LiveKit lo expone como `USER_REJECTED`; si se enruta por
   `disconnect_reason` en vez de por `sip_status_code`, `ocupado` y `rechazada` se confunden —
   y son justo el par 5 vs 11 que el enunciado señala.
4. **El buzón contesta 200 OK.** La única señal está en `amd`. Clasificar por SIP lo pierde.
5. **`agent_outcome.call_outcome` vacío** (`""`) en eventos 04 y 11: no es `completed`, es
   ausencia de desenlace. Tratarlo como "no concluyó" y pasar al LLM.
6. **`property_address` puede venir a `null`** (aviso explícito en el caso 1): la tarea de
   confirmar dirección tiene que existir igualmente y decirlo en el detalle.
7. **Sábado llamable pero no hábil.** Los dos calendarios son distintos y la config lo avisa.
8. **La reentrega repite etiqueta, no `no_aplica`.** Es la excepción de la tabla "eventos que
   no son casos" y es fácil de leer mal.
9. **Org ajena no emite ni `cerrar_llamada`.** Ni siquiera esa.

## 14. Lo que queda fuera a propósito

Se declara en el README, no se esconde:

- Comparación experimental de arquitecturas (§10 del plan original). Con 4 horas, elegir la
  arquitectura por criterio de diseño y justificarla es mejor inversión que medirla.
- Reintentos con backoff contra el CRM: no hay CRM, la ejecución es un append.
- Métricas de coste y latencia por evento más allá del registro en `procesamientos`.
- Política de retención de transcripciones: se anota como pendiente de producción, pero aquí
  no hay logs que las expongan porque no se loguea el transcript completo.
- Tests unitarios formales: el enunciado no los pide y el replay + fichero dorado cubre más
  superficie por minuto invertido.
