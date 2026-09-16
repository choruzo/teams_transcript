# Plan de mejoras: de "transcriptor" a "memoria del equipo"

Redactado el 2026-09-07. **Fases 0, 1, 2, 3, 4 y 5 implementadas** (las fases
1 y 2 el 2026-09-08; la 4 y la 5 después; la 3 el 2026-09-10); queda la 6
(map-reduce). **Fases 7 y 8** (corrección humana de acciones y sugerencias de
duplicados) añadidas como propuesta el 2026-09-16.

Este documento cubre el **motor** (glosario, JSON estructurado, SQLite,
arrastres, consulta y perfiles de voz). La **interfaz web** que lo consume
—timeline, métricas, tablero de acciones, chat— se planifica aparte, en
[`PLAN_INTERFAZ.md`](PLAN_INTERFAZ.md).

## 0. Decisiones tomadas (2026-09-07)

| Cuestión | Decisión | Consecuencia en el plan |
|---|---|---|
| Tipos de reunión | Principalmente dailys; también workshops y retros/plannings. Prompts distintos seleccionables, **daily por defecto** | Fase 1: campo `tipo` en `meetings` y flag `--tipo {daily,workshop,retro,planning}` en `summarize_teams.py`, con una plantilla de prompt por tipo |
| Ubicación de `meetings.db` | **Servidor Linux con GPU** | Transcripción y resumen se ejecutan allí; `ask_teams.py`/`report_teams.py` también. LiteLLM debe ser accesible desde el servidor (`--base-url`), no solo desde el portátil. El glosario tiene que existir en el servidor |
| Exportación a Planner/Jira | **No de momento**, el Markdown basta | Fase 5 genera solo Markdown. Sin integraciones ni credenciales; se mantiene el "todo local" |
| Umbral de similitud de voz | Sin decidir: se calibra con datos reales en la Fase 3 | Fase 3 arranca con `--dry-run` que muestra similitudes sin aplicarlas |

## 1. Objetivo

Hoy el pipeline es `grabar -> transcribir -> resumir` y **cada reunión es un
universo aislado**: se genera un `.md` legible y ahí muere la información. El
objetivo de este plan es que el histórico de reuniones sea **consultable,
acumulativo y con seguimiento**, de forma que la herramienta sirva para
gestionar el equipo y no solo para no tomar notas:

- Saber qué se dijo hace tres semanas sin releer seis ficheros.
- Saber qué tareas siguen abiertas y cuántas dailys llevan estancadas.
- Preguntar en lenguaje natural sobre el pasado.
- Que los hablantes se llamen igual en todas las reuniones.

Restricciones que se mantienen:

- **Todo local**: Whisper + pyannote + LLM vía LiteLLM en `localhost:4000`.
  Nada sale de la red.
- **Dependencias mínimas**: `summarize_teams.py` usa solo `urllib` de la
  stdlib; el almacén usará `sqlite3`, también stdlib. No se añaden ORMs,
  frameworks de RAG ni bases de datos vectoriales.
- **Scripts independientes y componibles**, como los actuales. Nada de un
  monolito.
- Compatibilidad con el flujo de dos máquinas (grabación en Windows,
  transcripción en el servidor Linux con GPU, ver `DEPLOY_OFFLINE.md`).

## 2. Diagnóstico

Problemas concretos observados en la salida real del 2026-09-07:

| # | Problema | Evidencia |
|---|---|---|
| P1 | Las etiquetas de hablante no persisten entre reuniones | `SPEAKER_03` es Pablo Gil hoy; mañana puede ser otro. pyannote reasigna en cada ejecución |
| P2 | La identificación depende de que alguien te nombre en voz alta | `SPEAKER_00 -> no identificado, confianza baja` porque el facilitador le dijo "te toca" sin nombrarle |
| P3 | La salida es prosa, no datos | `_resumen.md` no se puede filtrar, agregar ni comparar |
| P4 | No hay continuidad entre reuniones | Nadie sabe que "Javi: reinstalar WLS" ya venía de la daily anterior |
| P5 | El vocabulario del proyecto se pierde cada vez | Whisper transcribe "Goverity" (Coverity), "cuisine", "el son" (JSON), "Tales", "GmU" — errores que se repetirán idénticos mañana |
| P6 | No escala en tokens | Se manda la transcripción completa en una llamada; consultar varias reuniones a la vez es imposible con ese diseño |

## 3. Arquitectura propuesta

```
teams_transcript/
  record_teams.py        (sin cambios)
  transcribe_teams.py    (+ glosario, + perfiles de voz, + volcado a BD)
  summarize_teams.py     (+ salida JSON, + arrastres, + map-reduce)
  ask_teams.py           NUEVO  - preguntas en lenguaje natural
  report_teams.py        NUEVO  - informes agregados
  memoria.py             NUEVO  - capa de acceso a SQLite (compartida)
  datos/                 NUEVO  - en .gitignore, contiene datos de reuniones
    meetings.db          SQLite: reuniones, personas, acciones, FTS5
    perfiles_voz.json    Centroides de embedding por persona
    glosario.md          Vocabulario del proyecto
```

`datos/` va a `.gitignore` junto a `grabaciones/`: contiene contenido literal
de reuniones internas y datos biométricos (ver sección 6).

Flujo objetivo:

```
audio.wav
  -> transcribe_teams.py --diarize
       usa glosario.md (initial_prompt de Whisper)
       usa perfiles_voz.json (asigna nombres reales, no SPEAKER_XX)
       escribe .txt/.srt + inserta segmentos en meetings.db
  -> summarize_teams.py
       lee acciones abiertas de meetings.db (arrastres)
       pide JSON estructurado al LLM
       inserta en meetings.db + renderiza el .md
  -> ask_teams.py / report_teams.py
       consultan meetings.db, no reprocesan audio
```

---

## Fase 0 — Glosario del proyecto — IMPLEMENTADA

**Resuelve:** P5. **Esfuerzo:** bajo. **Riesgo:** ninguno.

Ficheros añadidos: `glosario.py` (módulo compartido, solo stdlib),
`datos/glosario.md` (real, no versionado) y `datos/glosario.ejemplo.md`
(plantilla, versionada).

El glosario es Markdown normal. La parte destinada a Whisper se delimita con
comentarios HTML `<!-- whisper -->` / `<!-- /whisper -->`; el resto del
documento se usa solo con el LLM. Se usa en dos puntos:

1. **Whisper** (`transcribe_teams.py`): la sección marcada alimenta el
   parámetro `initial_prompt`, formateada como frase ("Reunión de trabajo en
   español. Términos que aparecen: ...") porque Whisper responde mejor a eso
   que a una lista pelada.
   - *Limitación real*: Whisper recorta `initial_prompt` a `n_ctx // 2 - 1`
     tokens (~224) **quedándose con los últimos**, así que pasarse no trunca
     el final sino que borra el principio. `glosario.py` trunca antes, por
     coma, en `LIMITE_CARACTERES_WHISPER = 700`, y avisa por stderr.
2. **LLM** (`summarize_teams.py`): el glosario completo se inyecta en el
   system prompt con instrucción explícita de **corregir** los términos
   deformados y de tratar con cautela la sección "Dudosos".

Ambos scripts: se usa `datos/glosario.md` automáticamente si existe,
`--glosario RUTA` para otro fichero, `--sin-glosario` para desactivarlo. Si se
pasa `--glosario` con una ruta inexistente es error; que falte el fichero por
defecto no lo es.

### Resultado verificado (2026-09-07, `large-v3` en GPU sobre la daily del 7-sep)

| Término | Sin glosario | Con glosario | Con glosario + carry |
|---|---|---|---|
| ULS | "OLS" ×4, "WLS" ×1 | ULS ×3, "VLS" ×1 | **ULS ×5** |
| Thales | "Tales" | "tales" | **Thales** |
| Coverity | "Goverity" | "Goverity" | **Coverity** |
| eqsim | "cuisine" | "Cusin" | **eqsim** |
| planner | "el plan" | "el plan" | **planner** |

**El `carry` resultó imprescindible.** Whisper inyecta `initial_prompt` solo en
la primera ventana de 30 s; después, `condition_on_previous_text` hace que el
contexto sea el texto ya transcrito y el glosario se diluye. Sin
`carry_initial_prompt=True` el efecto fue irregular (3 de 5 aciertos en ULS,
más una regresión: "con OLS" pasó a "no el ese"). Con carry, todos los
objetivos salvo uno.

**Efecto secundario del carry:** bucles de repetición al final del audio
("VTS 8, en la estación 8..." ×5, "Gracias." ×10). Whisper alucina sobre el
silencio final y el prompt reinyectado lo realimenta. Resuelto con
`colapsar_repeticiones()` en `transcribe_teams.py`, que elimina los artefactos
de forma determinista antes de escribir `.txt`/`.srt`: 16 segmentos eliminados
sobre la salida real, conservando el 96 % de las palabras y los cinco términos
del glosario intactos. Desactivable con `--sin-limpieza`.

**Reproducibilidad:** una tercera pasada, ya con `--diarize`, dio exactamente
los mismos recuentos en los cinco términos (ULS ×5, Thales ×2, Coverity ×1,
eqsim ×3, GACF ×2, planner ×2). El efecto del glosario es estable entre
ejecuciones, pese a que Whisper no es determinista.

**Hallazgo colateral:** el misterioso "cuisine" era **eqsim**, el simulador de
equipos. El glosario lo resolvió.

**Limitación conocida:** `colapsar_repeticiones` solo detecta repeticiones
*idénticas y cercanas*. Whisper a veces repite al final del audio una frase
dicha mucho antes y con variaciones mínimas ("en la VTS 8, ... correcto." vs
"en la VTS8, ... correcto"), y eso no lo captura. Ampliar la ventana o usar
comparación difusa dispararía los falsos positivos, así que se deja como está:
el prompt de `summarize_teams.py` ya instruye al LLM para ignorar los
fragmentos repetitivos del final.

**Único objetivo no cumplido:** "el son" no pasó a JSON; Whisper produjo "el
SOAM" por su cuenta. Queda en la sección "Dudosos" del glosario, pendiente de
confirmar si SOAM es un término real del proyecto.

**Mantenimiento:** la sección "Dudosos" de `datos/glosario.md` conserva lo que
sigue sin confirmar (GCS4-1 / 4.1, SOAM).

---

## Fase 1 — Salida estructurada y almacén SQLite — IMPLEMENTADA

**Resuelve:** P3, y es la base de todas las fases siguientes.
**Esfuerzo:** medio. **Riesgo:** bajo (no toca la parte frágil del pipeline).

### 1.1 Esquema de `datos/meetings.db`

```sql
CREATE TABLE meetings (
  id              INTEGER PRIMARY KEY,
  fecha           TEXT NOT NULL,        -- ISO-8601, de la fecha del audio
  titulo          TEXT,
  tipo            TEXT NOT NULL DEFAULT 'daily',  -- daily|workshop|retro|planning
  audio_path      TEXT,
  transcript_path TEXT,
  duracion_seg    REAL,
  modelo_whisper  TEXT,
  modelo_llm      TEXT,
  creado_en       TEXT NOT NULL
);

CREATE TABLE personas (
  id       INTEGER PRIMARY KEY,
  nombre   TEXT NOT NULL UNIQUE,
  alias    TEXT,                        -- JSON: ["Javi", "Javier M."]
  activo   INTEGER NOT NULL DEFAULT 1
);

-- Qué etiqueta de pyannote fue quién, en cada reunión
CREATE TABLE meeting_speakers (
  meeting_id  INTEGER NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
  etiqueta    TEXT NOT NULL,            -- SPEAKER_03
  persona_id  INTEGER REFERENCES personas(id),
  confianza   REAL,
  metodo      TEXT,                     -- 'embedding' | 'llm' | 'manual'
  PRIMARY KEY (meeting_id, etiqueta)
);

-- Qué contó cada persona en cada reunión
CREATE TABLE updates (
  id             INTEGER PRIMARY KEY,
  meeting_id     INTEGER NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
  persona_id     INTEGER REFERENCES personas(id),
  trabajo        TEXT,
  bloqueos       TEXT,
  proximos_pasos TEXT
);

-- Acciones con vida propia entre reuniones (el corazón del seguimiento)
CREATE TABLE actions (
  id                  INTEGER PRIMARY KEY,
  descripcion         TEXT NOT NULL,
  persona_id          INTEGER REFERENCES personas(id),
  meeting_id_origen   INTEGER NOT NULL REFERENCES meetings(id),
  meeting_id_ultima   INTEGER REFERENCES meetings(id),
  estado              TEXT NOT NULL,    -- abierta|en_progreso|completada|bloqueada|abandonada
  menciones           INTEGER NOT NULL DEFAULT 1,
  cerrada_en          TEXT
);

CREATE TABLE risks (
  id          INTEGER PRIMARY KEY,
  meeting_id  INTEGER NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
  descripcion TEXT NOT NULL,
  area        TEXT,
  severidad   TEXT              -- alta|media|baja
);

-- Transcripción segmento a segmento, para poder citar y buscar
CREATE TABLE segments (
  id          INTEGER PRIMARY KEY,
  meeting_id  INTEGER NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
  idx         INTEGER NOT NULL,
  inicio      REAL,
  fin         REAL,
  etiqueta    TEXT,
  persona_id  INTEGER REFERENCES personas(id),
  texto       TEXT NOT NULL
);

-- Búsqueda de texto completo (FTS5 viene en el sqlite3 de la stdlib)
CREATE VIRTUAL TABLE segments_fts USING fts5(
  texto,
  content='segments', content_rowid='id',
  tokenize="unicode61 remove_diacritics 2"
);
```

`memoria.py` encapsula la creación del esquema (idempotente, con `PRAGMA
user_version` para migraciones), las inserciones y las consultas. Los demás
scripts no escriben SQL suelto.

### 1.2 Prompts por tipo de reunión

Flag `--tipo {daily,workshop,retro,planning}` en `summarize_teams.py`, **daily
por defecto** (el prompt actual, sin cambios de comportamiento). Cada tipo
tiene su plantilla, porque las secciones útiles no son las mismas:

- **daily**: por persona / acciones / bloqueos (lo actual).
- **retro**: qué fue bien, qué no, acciones de mejora acordadas.
- **planning**: alcance comprometido, estimaciones, dudas abiertas.
- **workshop**: temas tratados, decisiones, preguntas sin resolver.

Las plantillas viven en un diccionario en `summarize_teams.py` (o en
`prompts/*.md` si crecen). El esquema JSON de salida se mantiene común salvo
la sección específica de cada tipo, para que la BD y los informes no tengan
que saber de tipos.

### 1.3 El LLM devuelve JSON, no Markdown

`summarize_teams.py` cambia de estrategia: pide al modelo un objeto JSON con
un esquema fijo, y el Markdown pasa a **renderizarse desde ese JSON** en
Python. Ventaja: la misma llamada alimenta la BD y el `.md`, sin parsear prosa.

```json
{
  "resumen": "2-4 frases",
  "hablantes": [{"etiqueta": "SPEAKER_03", "nombre": "Pablo Gil", "confianza": "alta"}],
  "por_persona": [{"persona": "Francisco", "trabajo": "...", "bloqueos": "...", "proximos_pasos": "..."}],
  "acciones": [{"persona": "Javi", "descripcion": "Reinstalar WLS desde cero", "estado": "abierta"}],
  "arrastres": [{"action_id": 12, "estado": "bloqueada", "comentario": "..."}],
  "riesgos": [{"descripcion": "OLS inestable", "area": "OLS", "severidad": "alta"}]
}
```

Detalles de robustez, porque un LLM local no siempre obedece:

- `temperature` baja (0.1) para la llamada estructurada.
- Se acepta el JSON envuelto en un bloque de código y se extrae.
- Si `json.loads` falla, **un reintento** pidiendo solo el JSON corregido; si
  vuelve a fallar, se guarda la respuesta cruda como `.md` (comportamiento
  actual) y se avisa por stderr. Nunca se pierde el trabajo del LLM.
- Validación mínima de claves y tipos en Python antes de insertar en la BD.

El `.md` renderizado mantiene el formato actual (que ya funciona bien) más una
sección nueva de **Arrastres**.

**Criterio de aceptación:** procesar la transcripción del 2026-09-07 y obtener
un `.md` equivalente al actual *y* filas coherentes en `meetings`, `updates`,
`actions` y `risks`.

### Resultado (2026-09-08)

Ficheros: `memoria.py` nuevo; `summarize_teams.py` reescrito en su parte de
prompt, salida y persistencia. `transcribe_teams.py` **sin tocar**.

Verificado de extremo a extremo contra un servidor que imita
`/chat/completions` (no hacía falta GPU ni LiteLLM para probar el pipeline):
sobre la transcripción real del 7-sep se obtiene el `.md` con las secciones de
siempre y 326 segmentos, 2 updates, 2 acciones y 1 riesgo en la BD, con FTS5
sincronizada (326 filas) y búsqueda insensible a acentos. Probados también el
JSON envuelto en bloque de código, un tipo distinto (`retro`, con sus tres
secciones extra), el reproceso de la misma transcripción (sustituye, no
duplica) y el caso en que el modelo no devuelve JSON (guarda la respuesta
cruda, sale con código 2, no toca la BD).

Desviaciones respecto a lo planificado, todas conscientes:

1. **Dos columnas más en `meetings`**: `resumen` (texto del resumen, para no
   tener que abrir el JSON en las consultas) y `datos_json` (el objeto
   completo del LLM). Esto último salva la sección específica de cada tipo
   (`retro`, `planning`, `workshop`), que no tiene tabla propia: sin ella se
   perdería al salir del `.md`.
2. **Los segmentos los inserta `summarize_teams.py`**, leyéndolos del `.srt`
   hermano (el `.txt` no tiene marcas de tiempo), en vez de volcarlos desde
   `transcribe_teams.py`. Mantiene la Fase 1 fuera de la parte frágil del
   pipeline; si en la Fase 3 hay que tocar `transcribe_teams.py` de todos
   modos, se puede mover allí.
3. **`--importar`** (sección 5) no está hecho: solo hay una reunión anterior en
   `grabaciones/`, y reprocesarla con el script normal sale más barato que
   escribir el importador.
4. La **infraestructura de arrastres ya existe** en `memoria.py`
   (`acciones_abiertas`, `aplicar_arrastres`) y la clave `arrastres` viaja en
   el JSON y se renderiza, pero todavía no se inyectan las acciones abiertas
   en el prompt: eso es la Fase 2, y es lo único que falta para cerrarla.
   *(Hecho el 2026-09-08, ver el resultado de la Fase 2.)*

---

## Fase 2 — Seguimiento de acciones entre reuniones — IMPLEMENTADA

**Resuelve:** P4. **Esfuerzo:** bajo una vez existe la Fase 1.
**Es la mejora que da el enfoque de gestión.**

Antes de resumir la reunión de hoy, `summarize_teams.py` consulta las acciones
no cerradas de las últimas N reuniones (por defecto 5, flag `--arrastres N`) y
las inyecta en el prompt **con su identificador**:

```
Acciones abiertas de reuniones anteriores:
  [12] Javi — Reinstalar WLS desde cero siguiendo los pasos de Tales (abierta, 3 menciones)
  [15] Pablo — Registrar la non-regression en la tarea (abierta, 1 mención)
```

Se le pide que, para cada una, devuelva en `arrastres` el `action_id` y el
nuevo estado: `completada`, `en_progreso`, `bloqueada` o `sin_mencion`.

**Por qué por identificador y no por texto:** emparejar acciones entre
reuniones comparando cadenas es frágil ("reinstalar WLS" vs "la instalación de
WLS"). Dando al modelo la lista cerrada con identificadores, el emparejamiento
es una elección entre opciones, no una generación libre. Los `action_id` que
no existan se descartan en Python.

Con eso, `actions.menciones` y `estado` se actualizan solos, y aparece la
detección de estancamiento: *"WLS de Javi: 3 dailys abierta, sin avance"*.

**Criterio de aceptación:** procesar dos dailys consecutivas y comprobar que
una acción de la primera aparece en la sección "Arrastres" de la segunda con
el estado correcto.

### Resultado (2026-09-08)

Ficheros tocados: `summarize_teams.py` (flags, prompt, validación y
renderizado) y `memoria.py` (`acciones_abiertas` reescrita,
`id_reunion_por_transcripcion` nueva). `transcribe_teams.py` sigue sin
tocarse.

Flujo implementado: `cargar_acciones_abiertas` lee las acciones sin cerrar de
las últimas N reuniones (`--arrastres N`, por defecto 5; `--sin-arrastres` y
`--sin-bd` lo desactivan) y las inyecta en el *user prompt* con su id. El
`ARRASTRES_PROMPT` solo se añade al *system prompt* cuando esa lista existe,
así que una base vacía deja el comportamiento de la Fase 1 intacto.

Decisiones tomadas al implementar:

1. **La validación va contra la lista ofrecida, no contra la BD.** Sin lista
   no se acepta ningún arrastre (el modelo se los estaría inventando); con
   ella se descartan ids ajenos, duplicados y estados fuera de
   `ESTADOS_ACCION`. `sin_mencion` se acepta del modelo y se descarta al
   normalizar: no cambia nada en la BD y solo ensuciaría el `.md`.
2. **Estados ampliados a los cinco de `ESTADOS_ACCION`.** El plan proponía
   cuatro; faltaban `abierta` ("se menciona pero sigue igual") y `abandonada`
   ("se decide no hacerla"), que son estados reales del ciclo de vida y ya
   existían en el esquema.
3. **El reproceso tenía que ser idempotente y no lo era.**
   `acciones_abiertas(excluir_meeting_id=...)` ya no se limita a filtrar: si
   la transcripción ya estaba registrada, simula el efecto de
   `_deshacer_arrastres` y devuelve las acciones ajenas con el estado y las
   menciones **anteriores** a aquella pasada. Sin eso, una acción cerrada por
   la pasada previa no volvía a ofrecerse al modelo y el segundo procesado
   daba un resultado distinto del primero.
4. **Estancamiento visible en el `.md`**, no solo consultable: a partir de
   `UMBRAL_ESTANCAMIENTO = 3` menciones sin cerrarse, el arrastre se marca
   `ESTANCADA`. La sección "Arrastres" muestra además la descripción y la
   persona reales (que salen de la BD, no del modelo), las reuniones que lleva
   y el comentario.

Verificado de extremo a extremo contra un servidor que imita
`/chat/completions`, con dos dailys consecutivas:

- Criterio de aceptación cumplido: las dos acciones de la daily 1 aparecen en
  la sección "Arrastres" de la daily 2 con el estado que dio el modelo
  (`bloqueada` y `completada`), y en la BD suben a 2 menciones con
  `meeting_id_ultima` apuntando a la segunda reunión.
- Robustez: de cuatro arrastres devueltos por el "modelo" (dos válidos, uno
  con id inventado, uno `sin_mencion`) solo se aplican los dos válidos.
- Idempotencia: reprocesar la daily 2 deja exactamente las mismas filas.
- `--sin-arrastres` no consulta la BD, no inyecta la lista y deshace el efecto
  de la pasada anterior (las menciones vuelven a 1), como corresponde a
  reprocesar la reunión sin arrastres.

**Pendiente, menor — resuelto en la Fase 5:** las acciones abiertas que el
modelo marca `sin_mencion` no aparecen en el `.md`. Su seguimiento (cuántas
reuniones llevan sin mencionarse) lo da ahora `report_teams.py`, deduciéndolo
de las reuniones posteriores a la última mención (`memoria.acciones_sin_mencion`).

---

## Fase 3 — Perfiles de voz persistentes — **implementada (2026-09-10)**

**Resuelve:** P1 y P2. **Esfuerzo:** medio-alto. **Riesgo:** el más alto del
plan — toca `transcribe_teams.py` y el entorno offline con GPU.

Idea: además de la diarización, extraer el **embedding de voz** de cada
cluster y compararlo con centroides guardados por persona.

- pyannote 3.1+ permite `pipeline(audio, return_embeddings=True)`, que
  devuelve la diarización **y** un embedding por hablante detectado. Con eso
  **no hace falta ninguna dependencia nueva**: el modelo
  `wespeaker-voxceleb-resnet34-LM` ya está en la caché (ver `DEPLOY_OFFLINE.md`
  sección 5, ya se descarga hoy como parte del pipeline).
- `datos/perfiles_voz.json`: por persona, el centroide (vector) y el número de
  muestras acumuladas. Al confirmar una asignación, el centroide se actualiza
  como media incremental, de modo que el perfil mejora con cada reunión.
- Asignación por similitud coseno con un umbral configurable
  (`--umbral-voz`, en torno a 0,6 como punto de partida, a calibrar con datos
  reales). Por debajo del umbral el hablante queda como `SPEAKER_XX` y se deja
  que el LLM lo infiera por contexto, como ahora.
- Alta de personas nuevas: `transcribe_teams.py --enroll`, que lista los
  hablantes detectados con su duración de habla y un fragmento de su
  transcripción, y pide interactivamente el nombre de cada uno. Es el único
  punto interactivo del plan y se ejecuta rara vez.
- **Degradación elegante**: si `perfiles_voz.json` no existe o falla la
  extracción de embeddings, se cae al comportamiento actual (etiquetas
  genéricas + inferencia del LLM). Igual que hoy con la diarización, la
  transcripción se guarda **antes** de intentar nada de esto.

**Calibración necesaria:** el umbral y la fiabilidad hay que medirlos con
grabaciones reales antes de fiarse. Primera versión con `--dry-run` que
muestre las similitudes calculadas sin aplicarlas.

**Criterio de aceptación:** tras dar de alta al equipo, una daily nueva
produce un `.txt` con `[Pablo Gil]` en vez de `[SPEAKER_03]`, y el mismo
nombre para la misma persona en dos reuniones distintas.

### Resultado (2026-09-10)

Implementada tal cual se planteó, con un módulo nuevo `perfiles_voz.py` (solo
stdlib) y cinco flags en `transcribe_teams.py`: `--enroll`, `--perfiles`,
`--umbral-voz`, `--dry-run-voz`, `--sin-perfiles` (y `--sin-aprender`, que no
estaba en el plan). Verificado en el servidor con GPU sobre dos reuniones
reales del 7 y el 10 de septiembre.

**Lo que cambió respecto a lo planificado:**

1. **El umbral no es 0,6 sino 0,70.** Es el punto que había que medir y se
   midió comparando los embeddings de cada hablante entre tramos y días
   distintos:

   | | similitud coseno |
   |---|---|
   | Misma persona, tramos y reuniones distintas | 0,729 – 0,968 (típico ~0,89) |
   | Personas distintas | 0,05 – 0,52, y **0,638** en un tramo corto |

   Con 0,6, ese 0,638 habría puesto el nombre de otra persona en la
   transcripción sin avisar de nada.

2. **`return_embeddings=True` ya no existe en pyannote 4.x**, que es la
   versión del servidor (4.0.7). Los embeddings vienen siempre en
   `DiarizeOutput.speaker_embeddings`, ordenados como `annotation.labels()`;
   pasar el argumento solo suelta un *"Ignoring unexpected keyword
   arguments"*. Se detecta mirando la firma, así que sirven las dos versiones.
   Confirmado lo importante del plan: **no hace falta ninguna dependencia ni
   modelo nuevo**, los 256 valores por hablante salen del pipeline que ya
   diariza.

3. **Mínimo de habla para aprender (`MINIMO_SEGUNDOS_MUESTRA`, 15 s).** Los
   dos extremos de la calibración —el peor acierto (0,729) y el peor fallo
   (0,638)— eran de hablantes con menos de 20 s. Identificar con una muestra
   así se permite, porque solo afecta a esa reunión; crear o actualizar un
   centroide con ella no, porque el error queda guardado para siempre.

4. **La asignación es exclusiva**: dos hablantes distintos de la misma
   reunión no pueden resolverse como la misma persona.

**Prueba de aceptación (servidor, GPU, Whisper small):** dado de alta el
equipo sobre un tramo de la daily del 7-sep, un tramo de la del 10-sep
reconoció por su nombre a 3 de los 4 hablantes (0,802 / 0,840 / 0,899) y dejó
como `SPEAKER_02` al cuarto, que no tenía perfil (mejor candidato 0,465). Es
exactamente la degradación buscada: quien no se reconoce se queda con la
etiqueta genérica y lo resuelve el LLM por contexto.

**Privacidad:** los perfiles son datos biométricos. `datos/perfiles_voz.json`
no se versiona, `--enroll` lo advierte en pantalla antes de preguntar nombres
y `python perfiles_voz.py --olvidar "Nombre"` borra el de una persona (la
utilidad `--olvidar` que la sección 6 recomendaba, de momento solo para el
perfil de voz; anonimizar sus filas del histórico sigue pendiente).

---

## Fase 4 — `ask_teams.py`: preguntar sobre el pasado — **implementada**

**Resuelve:** el caso de uso principal de gestión. **Esfuerzo:** medio.

```powershell
python ask_teams.py "¿qué dijo Francisco sobre OLS este mes?"
python ask_teams.py "¿qué lleva más tiempo bloqueado?" --desde 2026-08-01
```

Flujo en tres pasos:

1. **Interpretación**: una primera llamada barata al LLM convierte la pregunta
   en filtros estructurados (persona, rango de fechas, términos de búsqueda, y
   si la pregunta es *puntual* o *agregada*).
2. **Recuperación**, según el tipo:
   - *Puntual* ("qué dijo X sobre Y"): FTS5 sobre `segments`, filtrado por
     persona y fecha, quedándose con los mejores resultados y sus segmentos
     vecinos como contexto.
   - *Agregada* ("qué lleva bloqueado", "en qué se ha ido el mes"): consulta
     SQL directa sobre `updates`/`actions`/`risks`, sin búsqueda textual.
3. **Respuesta**: el LLM responde **citando reunión y fecha** en cada
   afirmación, con instrucción explícita de decir "no consta" antes que
   inventar (misma filosofía que el prompt actual, que ya insiste en esto).

**Nota sobre FTS5:** no hace *stemming* en español ("bloqueado" no encuentra
"bloquear"). Se compensa haciendo que el paso 1 genere variantes de los
términos y usando el operador `OR` de FTS5. Es suficiente para este volumen y
evita meter una base vectorial. Si a futuro se queda corto, el paso natural es
añadir embeddings vía el propio LiteLLM y una columna BLOB — el esquema no
tendría que cambiar.

---

## Fase 5 — `report_teams.py`: informes agregados — **implementada (2026-09-10)**

**Esfuerzo:** bajo una vez existen las fases 1 y 2 (es casi todo SQL).

```powershell
python report_teams.py --semanal
python report_teams.py --desde 2026-08-01 --persona "Pablo Gil"
```

Contenido:

- Acciones abiertas ordenadas por antigüedad, con dailys de arrastre.
- Acciones cerradas en el periodo.
- Bloqueos recurrentes (los que aparecen en varias reuniones).
- Reparto de temas por persona (dónde se ha ido el esfuerzo).
- Personas sin actualización reciente o con señales de sobrecarga.

Los números salen de SQL (deterministas, no alucinables) y el LLM solo escribe
la narrativa alrededor. Salida en Markdown.

### Lo que cambió al implementarla (2026-09-10)

1. **Los cinco contenidos están, pero tres de ellos solo se podían dar
   diciendo lo que no significan.** El plan los listaba como si el dato
   existiera y en dos casos no existe del todo:
   - *Acciones cerradas en el periodo*: `cerrada_en` es la fecha en que se
     procesó la reunión que las cerró, no la del cierre real (D4). Se da
     igualmente, con la advertencia al pie de la lista.
   - *Bloqueos recurrentes*: `updates.bloqueos` es texto libre sin identidad
     (D8). `memoria.bloqueos_recurrentes` normaliza el texto (acentos,
     mayúsculas y puntuación fuera) y cuenta en cuántas reuniones distintas
     aparece esa misma frase. **No es detección de temas**: uno contado con
     otras palabras no se detecta, y el informe lo dice.
   - *Personas sin actualización reciente*: sin roster (D7) no se puede
     distinguir "no ha hablado" de "ya no está". Solo se ven las personas que
     la base ya conoce, y también se dice.
   La quinta —"señales de sobrecarga"— no se ha aproximado: no hay ningún dato
   que la soporte y un número inventado en un informe es peor que su ausencia.
2. **El pendiente de la Fase 2 queda cerrado aquí**, que es donde el plan
   anotó que encajaba. `memoria.acciones_sin_mencion` deduce cuántas reuniones
   se han celebrado sin nombrar cada acción abierta contando las posteriores a
   su última mención, sin necesidad de guardar nada nuevo.
3. **La narrativa nunca bloquea el informe.** Mismo criterio que el intérprete
   de `ask_teams.py`: si el LLM no responde, salen las tablas y una nota
   diciendo por qué falta el análisis. `--sin-llm` ni lo intenta y `--json`
   vuelca solo las cifras.
4. **Los recuentos de cabecera se recalculan con el filtro de `--persona`** en
   vez de tomarse de `metricas()`, que no lo conoce. Y como el filtro no
   alcanza a las reuniones ni a los riesgos (no tienen responsable), la
   cabecera del informe declara hasta dónde llega.
5. **`--referencia`**, que el plan no preveía: `--semanal`/`--mensual` se
   calculan sobre hoy, y sin esto no había forma de sacar el informe de la
   semana pasada.

Cubierto por `tests/test_report.py` (42 pruebas: las cuatro consultas nuevas
de `memoria.py`, los periodos, el alcance mezclado de las cifras, el filtro de
persona, la degradación sin LLM y que cada advertencia aparezca cuando su
lista tiene filas). Verificado además de extremo a extremo contra un servidor
que imita `/chat/completions`.

---

## Fase 6 (transversal) — Map-reduce para contexto largo

**Resuelve:** P6.

Una daily de una hora con diarización ronda los 10-40k tokens; varias
reuniones juntas no caben en la ventana del modelo local. Se necesita:

- Estimación de tamaño en Python antes de llamar (aproximación por caracteres,
  unos 3,5 caracteres por token en español; no se añade un tokenizador como
  dependencia).
- Si excede el umbral (`--max-tokens`, configurable según el modelo), trocear
  la transcripción por tiempo (unos 20 min con 1 min de solape, cortando en
  cambio de turno para no partir intervenciones), resumir cada trozo y
  combinar en un paso de reducción.
- El troceo afecta a `summarize_teams.py` y a la síntesis de `ask_teams.py`.

---

## Fase 7 — Corrección humana de acciones (esquema 3) — **implementada (motor, 2026-09-16)**

**Resuelve:** el LLM crea acciones duplicadas, mal redactadas, asignadas a
quien no es o que directamente no son tareas, y hoy no hay forma de
arreglarlo sin SQL a mano. **Esfuerzo:** alto. **Riesgo:** alto: toca
`crear_reunion`, que es el corazón de la idempotencia del reproceso.

Es la mitad de motor de la fase **I6a** de
[`PLAN_INTERFAZ.md`](PLAN_INTERFAZ.md) (panel de edición al pulsar una acción
del timeline). Va aquí y no allí por el mismo principio de siempre: si la
lógica vive en la API, procesar por SSH seguiría pisando las correcciones.

### Qué se ve en los datos reales

Las dos dailys de la base (7 y 10 de septiembre) ya tienen los tres casos:

- **Emparejadas o duplicadas**: #12 «Seguir estabilizando la versión de ULS»
  y #14 «Seguir con ULS; avanzar en banda base cuando haya hueco»; #11
  «Continuar trabajo en banda base» solapa con la segunda mitad de #14.
- **Dependientes**: #25 «Hablar con Diego tras la entrega de GCS4-1» no
  puede avanzar hasta #16 «Cerrar GCS4-1».
- **Mal asignadas**: responsables `equipo` y `Pedro / José Javier`, que
  `obtener_o_crear_persona` ha dado de alta como si fueran personas.

### Decisiones tomadas (2026-09-16)

| Cuestión | Decisión | Consecuencia |
|---|---|---|
| Qué es «eliminar» | **Descartar, reversible.** Nunca `DELETE` | La acción sigue en la base con `descartada_en` y un motivo; no se ve, no cuenta y no se le ofrece al LLM. Si se reprocesa la reunión y el modelo la vuelve a generar, **sigue descartada** |
| Relaciones | **Duplicada (fusión)** y **depende de**. No hay «relacionada» sin semántica | La fusión cambia lo que se cuenta; la dependencia solo se muestra y se da como contexto al LLM |
| Campos corregibles | **Descripción, responsable y estado.** Crear acciones a mano queda fuera | Sin acciones «huérfanas» de reunión: toda acción sigue naciendo en una transcripción |
| Supervivencia al reproceso | **Reconciliar y conservar**, en lugar de la tabla `overrides` reaplicada que proponía el plan de interfaz | `crear_reunion` deja de borrar las acciones: las empareja con lo que devuelve el modelo y conserva `id` y `uid`. Lo que no encuentra pareja y tiene correcciones se marca *pendiente de revisión*, no se pierde |
| Dependencias y LLM | **Sí, como contexto** en la lista de arrastres | `bloqueada por [16]` junto a la acción; la validación de `normalizar()` no cambia |
| Sugerencias automáticas de duplicados | **Sí, en una fase posterior** (Fase 8) | Esta fase es solo manual |

**Por qué se abandona `overrides`.** Reaplicar correcciones sobre filas
recién insertadas obliga a encontrar «la misma acción» con una clave, y la
única clave posible es el texto que el modelo acaba de reescribir. Con
`overrides` la corrección se busca **después** de perder la fila; con la
reconciliación se busca **antes** de decidir si se borra, con la fila
original delante —su id, sus vínculos, sus correcciones— y la redacción que
el modelo le dio la primera vez. Además, una fusión o una dependencia no son
«un campo con otro valor»: son vínculos entre dos filas que la cascada del
borrado destruiría antes de que nadie pudiera reaplicarlos.

### 7.1 Esquema 3

Migración 2 → 3 en `_migrar`, con las mismas reglas que la 1 → 2 (`ALTER
TABLE` antes de `_ESQUEMA`, sobre una copia de una base real en los tests):

```sql
-- actions: columnas nuevas
uid               TEXT     -- identidad estable, se asigna una vez y no se recalcula
descripcion_llm   TEXT     -- lo que dijo el modelo; `descripcion` es lo vigente
descartada_en     TEXT
motivo_descarte   TEXT
absorbida_por     INTEGER REFERENCES actions(id)   -- fusión: esta es la duplicada
revisar           INTEGER NOT NULL DEFAULT 0      -- el reproceso no la encontró

CREATE UNIQUE INDEX idx_actions_uid ON actions(uid);

-- D10, por fin: qué reuniones tocaron cada acción
CREATE TABLE action_mentions (
  action_id   INTEGER NOT NULL REFERENCES actions(id) ON DELETE CASCADE,
  meeting_id  INTEGER NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
  estado      TEXT NOT NULL,
  comentario  TEXT,
  PRIMARY KEY (action_id, meeting_id)
);

CREATE TABLE action_dependencias (
  action_id       INTEGER NOT NULL REFERENCES actions(id) ON DELETE CASCADE,
  depende_de_id   INTEGER NOT NULL REFERENCES actions(id) ON DELETE CASCADE,
  creado_en       TEXT NOT NULL,
  PRIMARY KEY (action_id, depende_de_id),
  CHECK (action_id != depende_de_id)
);

-- Historial de lo que ha hecho un humano. No se reaplica: documenta y protege.
CREATE TABLE correcciones (
  id              INTEGER PRIMARY KEY,
  action_id       INTEGER NOT NULL REFERENCES actions(id) ON DELETE CASCADE,
  campo           TEXT NOT NULL,   -- descripcion | persona | estado | descarte | fusion | dependencia
  valor_anterior  TEXT,
  valor_nuevo     TEXT,
  origen          TEXT NOT NULL,   -- web | cli
  creado_en       TEXT NOT NULL,
  deshecha_en     TEXT
);

CREATE VIEW acciones_vigentes AS
  SELECT * FROM actions WHERE descartada_en IS NULL AND absorbida_por IS NULL;
```

Notas:

- **D10 entra en esta fase porque la fusión lo necesita.** Sumar los
  contadores `menciones` de dos duplicadas cuenta dos veces cada reunión que
  las nombró a las dos, y deshacer la fusión sin saber qué menciones eran de
  quién es imposible. Con `action_mentions`, `menciones` y
  `meeting_id_ultima` pasan a ser **derivables** (se mantienen como caché
  por compatibilidad), la fusión es la unión de dos conjuntos y el carril del
  timeline puede dibujar por fin las marcas intermedias. Resuelve también D4:
  la fecha del cierre es la de la reunión de la mención que lo cerró.
- **`descripcion_llm` es la clave del emparejamiento**, no `descripcion`:
  una descripción corregida a mano nunca se parecerá a lo que el modelo
  vuelva a generar, y la original sí.
- **La migración rellena** `uid` (`<uid reunión>-a<n>`), `descripcion_llm`
  (copia de `descripcion`) y `action_mentions` a partir de lo único que hay:
  el origen y, si `meeting_id_ultima` es distinta, esa. Las menciones
  intermedias de las acciones con más de dos se pierden y **se dice** en la
  salida de `--migrar`; con la base actual no afecta a ninguna.
- **`acciones_vigentes` es la única entrada de lectura.** Hoy hay 14
  consultas con `FROM actions` en `memoria.py` (tablero, métricas, carriles,
  informes, arrastres…). Todas pasan a la vista salvo las que escriben y la
  ficha de detalle. Un test recorre `memoria.py` y falla si aparece un
  `FROM actions` nuevo fuera de una lista blanca: una descartada que se
  cuela en `metricas()` y no en el tablero es exactamente la contradicción
  que esta herramienta existe para evitar.

### 7.2 Operaciones en `memoria.py`

Todas dentro de una transacción, registran su fila en `correcciones` y
devuelven la acción resultante. La API solo las llama.

| Función | Qué hace | Reglas |
|---|---|---|
| `corregir_accion(uid, descripcion=, persona=, estado=, origen=)` | Cambia los campos indicados | `estado` validado contra `ESTADOS_ACCION`; al cerrar pone `cerrada_en`, al reabrir lo borra. `persona` pasa por `obtener_o_crear_persona` |
| `descartar_accion(uid, motivo)` / `restaurar_accion(uid)` | Marca o desmarca `descartada_en` | Descartar una acción de la que dependen otras borra esas dependencias (y queda en `correcciones`, para poder restaurarlas) |
| `fusionar_acciones(principal_uid, duplicada_uid)` | `absorbida_por` + unión de menciones | La principal toma el origen más antiguo; su estado no cambia. No se fusiona en cadena: si la duplicada ya absorbía otras, pasan a la principal. Las dependencias de la duplicada se trasladan |
| `separar_acciones(duplicada_uid)` | Deshace la fusión | Cada una recupera sus menciones: por eso existe `action_mentions` |
| `anadir_dependencia(uid, depende_de_uid)` / `quitar_dependencia` | Vínculo dirigido | **Rechaza ciclos** (recorrido en Python sobre la tabla), consigo misma y con descartadas o absorbidas |
| `detalle_accion(uid)` | Ficha completa | Menciones con fecha y comentario, dependencias en los dos sentidos, absorbidas, historial de `correcciones` |

**Cerrar una acción con dependencias abiertas se permite y se avisa.** La
realidad manda sobre el grafo: si alguien dice en la daily que ya está, es
que el vínculo era erróneo o ya no importa. La función devuelve el aviso; no
bloquea.

### 7.3 Reconciliación al reprocesar

Es lo que sustituye al borrado en cascada de las acciones en `crear_reunion`
y el punto más delicado de la fase.

1. **La fila de `meetings` ya no se borra**: se actualiza en su sitio y se
   borran a mano sus hijos regenerables (segmentos, updates, riesgos,
   hablantes, y las menciones que esa reunión hizo a acciones ajenas). Hoy el
   `id` se conserva reinsertándolo; con esto se conserva sin trucos, y la
   cascada deja de llevarse por delante acciones con vínculos.
2. **`_deshacer_arrastres` pasa a trabajar sobre `action_mentions`**: quitar
   las menciones de esta reunión y recalcular estado, `menciones` y
   `meeting_id_ultima` desde las que quedan. Desaparece el «las cerradas
   vuelven a `abierta`» que hoy se asume porque el estado previo no se
   guardaba.
3. **Emparejar las acciones nacidas en la reunión** con las que devuelve el
   modelo, en este orden y de forma exclusiva (una fila solo empareja una
   vez, como los perfiles de voz):
   1. `descripcion_llm` normalizada idéntica (misma normalización que
      `_huella_bloqueo`).
   2. Similitud de `difflib.SequenceMatcher` sobre el texto normalizado por
      encima de `UMBRAL_RECONCILIACION`, con el mismo responsable como
      desempate. Stdlib, sin embeddings: tiene que funcionar donde no está
      bge-m3.
4. **Con pareja**: se conserva la fila (`id`, `uid`, vínculos). Los campos
   **con corrección vigente** mantienen el valor humano; el resto toma el
   del modelo. `descripcion_llm` se actualiza a la nueva redacción. Una
   descartada o absorbida sigue estándolo.
5. **Sin pareja y protegida** (tiene correcciones, está descartada, absorbe
   o es absorbida, o participa en una dependencia): se conserva con
   `revisar = 1`. La interfaz la enseña como «el modelo ya no la detecta en
   esta reunión» con dos botones: descartar o mantener.
6. **Sin pareja y sin proteger**: se borra, como hoy.
7. Las acciones del modelo sin pareja se insertan como nuevas.

**El estado corregido a mano frente a los arrastres.** Una corrección de
estado es cierta *a partir* del día en que se hizo. Regla: un arrastre de una
reunión con fecha **anterior o igual** al día de la corrección no la pisa
(reprocesar la daily del lunes no deshace lo que alguien arregló el
miércoles); uno de una reunión **posterior** sí la actualiza, porque es
información nueva. Se registra en `action_mentions` igualmente.

**El umbral es una medición, no una preferencia**, igual que el de voz:
`summarize_teams.py --dry-run-reconciliacion` reprocesa sin escribir y
enseña cada pareja con su similitud. Se calibra reprocesando las dailys
reales con el mismo modelo y con otro (`qwen3.8-27b`), que es el caso en que
más cambia la redacción.

### 7.4 Arrastres

- `acciones_abiertas` lee de `acciones_vigentes`: las descartadas y las
  absorbidas no se ofrecen. Si el modelo devuelve igualmente su id, la
  validación actual ya lo descarta por no estar en la lista.
- Cada acción ofrecida lleva sus dependencias abiertas:
  `[25] Pedro — Hablar con Diego tras la entrega (abierta, 1 mención; bloqueada por [16])`.
  `ARRASTRES_PROMPT` explica en una línea qué significa; no se pide al modelo
  que cree ni quite dependencias.
- La descripción ofrecida es la **corregida**, no la del modelo: es la que
  mejor describe la tarea y la que el equipo reconocerá.

### 7.5 Tests

Contra una base temporal, como el resto:

- Cada operación de 7.2 y su deshacer deja la base igual que antes
  (comparando filas, como la idempotencia de la Fase 2).
- **Reprocesar una reunión con una acción corregida, otra descartada, dos
  fusionadas y una dependencia conserva las cinco cosas.** Es el criterio de
  aceptación de la fase.
- Reprocesar con una respuesta del modelo que ya no contiene una acción
  protegida la deja con `revisar = 1`; una sin proteger desaparece.
- Una corrección de estado sobrevive al reproceso de una reunión anterior y
  cede ante una posterior.
- Ciclos de dependencias rechazados; fusionar una acción ya absorbida
  rechazado.
- Ninguna consulta de lectura cuenta descartadas ni absorbidas (el test de
  `FROM actions` más un caso por función pública).
- Migración 2 → 3 sobre una copia de la base real.

### 7.6 Fuera de esta fase

- **Responsables compuestos** (`Pedro / José Javier`, `equipo`): son un
  problema de `personas`, no de acciones. Esta fase permite reasignar, pero
  no impide que el modelo los vuelva a crear. Va con la gestión de personas y
  alias (I6b del plan de interfaz).
- Crear acciones a mano.
- Riesgos (D3): siguen sin estado y sin corrección.

### Resultado (2026-09-16)

Implementada la mitad de motor: esquema 3 y su migración, las operaciones de
7.2, la reconciliación de 7.3 y los arrastres de 7.4, todo en `memoria.py`,
más `--dry-run-reconciliacion` en `summarize_teams.py`. Los endpoints y el
panel de edición siguen siendo I6a. Cubierto por `tests/test_correcciones.py`
(35 pruebas; 328 en total), incluido el criterio de aceptación y la migración
de una copia de la base real cuando existe `datos/meetings.db`. Sobre esa
copia: 28 acciones, 42 menciones reconstruidas (14 con su comentario sacado de
`datos_json`), ninguna mención intermedia perdida y ningún estado cambiado al
recalcular desde las menciones.

Lo que cambió al implementarla:

1. **D4 queda resuelta como decía el plan**: `cerrada_en` es la fecha de la
   reunión en que pasó a cerrada (o el día de la corrección manual). Las
   advertencias de `report_teams.py` y del panel de métricas se han
   reescrito en consecuencia.
2. **La principal no toma el `meeting_id_origen` de la duplicada.** Ese campo
   es el ancla con la que la reconciliación encuentra la acción al reprocesar
   su reunión de nacimiento; cambiarlo haría que el modelo la regenerara como
   nueva. La fecha más antigua se ve en las menciones de `detalle_accion`.
3. **Las menciones no se mueven al fusionar.** La principal cuenta la unión de
   las suyas y las de sus absorbidas, y el estado sale solo de las propias
   (así «su estado no cambia» se cumple también cuando la duplicada tiene una
   mención posterior). Separar no tiene que devolver nada.
4. **El estado se deriva por fecha de reunión, no por orden de proceso**:
   reprocesar una daily antigua ya no pisa lo que dijo otra posterior.
5. **La reconciliación solo actúa sobre lo que `crear_reunion` marcó** en una
   tabla temporal de la conexión. Insertar acciones en una reunión nueva, o
   dos veces seguidas en la misma pasada, no borra nada.
6. **Las acciones nuevas se insertan antes de borrar las huérfanas**: si no,
   la nueva heredaba el `uid` de la borrada (`<reunión>-a<n>` numera desde el
   mayor existente) y un enlace guardado cambiaba de destino. Queda un caso
   residual: si una pasada solo borra la última y otra posterior inserta, el
   número se reutiliza.
7. **`deshacer_correccion`**, que el plan no listaba: sin él, corregir un
   campo no tenía vuelta atrás y la acción quedaba protegida para siempre.
8. **`UMBRAL_RECONCILIACION = 0.75` es provisional.** Falta medirlo en el
   servidor con `--dry-run-reconciliacion` sobre las dailys reales, con el
   mismo modelo y con `qwen3.8-27b`.

Para desplegar: `python memoria.py --migrar` en el servidor antes de
actualizar la API (en solo lectura responde 503 con una base en esquema 2).

---

## Fase 8 — Sugerencias de duplicados — propuesta (2026-09-16)

**Resuelve:** que haya que descubrir a ojo las duplicadas entre cientos de
acciones. **Esfuerzo:** medio. **Riesgo:** bajo: nunca escribe nada sin un
humano. **Depende de:** Fase 7 y del índice semántico de la Fase 4.

- **Nunca se fusiona sola.** El sistema propone parejas; la decisión es de
  quien las mira, y pasa por `fusionar_acciones` como cualquier otra.
- **Embeddings con bge-m3**, el mismo modelo y endpoint del índice. Los
  vectores de las acciones son derivados y reconstruibles, así que viven en
  `datos/indice.db` (tabla nueva, `VERSION_TROCEADO` aparte), no en
  `meetings.db`. Se recalculan en el paso de indexado de `procesar_teams.py`
  y con `indexar_teams.py --acciones`.
- **Solo entre vigentes, y con sentido**: parejas de acciones abiertas, o una
  recién nacida frente a las abiertas. No se proponen dos acciones de la
  misma reunión con distinto responsable (el LLM ya las separó a propósito).
- **Los rechazos son una decisión humana y viven en `meetings.db`**
  (`sugerencias_rechazadas(action_a_uid, action_b_uid, creado_en)`): borrar
  el índice no puede hacer que vuelvan a proponerse parejas ya descartadas.
- **Umbral medido, no elegido**: `indexar_teams.py --acciones --dry-run`
  lista todas las parejas con su similitud. Se calibra con las fusiones que
  se hayan hecho a mano en la Fase 7, que son la verdad de referencia.
- **Sin `sqlite-vec` o sin modelo, no hay sugerencias y se dice**; el resto
  de la corrección funciona igual. Mismo criterio que el chat.
- `report_teams.py` puede listar las sugerencias pendientes como una sección
  más, con su advertencia de que son probables, no confirmadas.

---

## 4. Orden de implementación recomendado

| Orden | Fase | Esfuerzo | Riesgo | Desbloquea |
|---|---|---|---|---|
| ~~1~~ | ~~Fase 0 — Glosario~~ **hecha** | Bajo | Nulo | Calidad de todo lo demás |
| ~~2~~ | ~~Fase 1 — JSON + SQLite~~ **hecha** | Medio | Bajo | Fases 2, 4, 5 |
| ~~3~~ | ~~Fase 2 — Arrastres~~ **hecha** | Bajo | Bajo | El valor de gestión |
| ~~4~~ | ~~Fase 4 — `ask_teams.py`~~ **hecha** | Medio | Bajo | Consulta del histórico |
| ~~5~~ | ~~Fase 5 — `report_teams.py`~~ **hecha** | Bajo | Nulo | Informes |
| ~~6~~ | ~~Fase 3 — Perfiles de voz~~ **hecha** | Medio-alto | **Alto** | Calidad a largo plazo |
| ~~7~~ | ~~Fase 7 — Corrección humana de acciones (esquema 3, D10)~~ **hecha (motor)** | Alto | **Alto** | I6a de la interfaz; Fase 8 |
| 8 | Fase 8 — Sugerencias de duplicados | Medio | Bajo | I11 de la interfaz |
| 9 | Fase 6 — Map-reduce | Medio | Medio | Reuniones largas |

La Fase 3 va deliberadamente tarde pese a ser muy valiosa: es la única que
toca la parte frágil (pyannote, GPU, entorno offline) y conviene abordarla con
el resto ya estable y con datos reales acumulados para calibrar el umbral.

Las fases 1-2 y 4-5 son puramente aditivas: el pipeline actual sigue
funcionando igual si se ignoran los flags nuevos.

## 5. Compatibilidad hacia atrás

- Todos los scripts actuales mantienen su comportamiento por defecto salvo
  `summarize_teams.py`, que pasa a pedir JSON. Se añade `--sin-bd` para
  reproducir exactamente el comportamiento de hoy (una llamada, Markdown
  directo, sin tocar SQLite).
- Se añade un modo `--importar` que ingesta transcripciones y resúmenes ya
  existentes en `grabaciones/` para no empezar la BD vacía.
- **La BD vive en el servidor Linux con GPU** (decisión tomada). Implica que
  `summarize_teams.py`, `ask_teams.py` y `report_teams.py` se ejecutan allí, y
  que LiteLLM debe ser accesible desde el servidor (`--base-url`, ya que el
  valor por defecto `localhost:4000` asume que el proxy corre en la misma
  máquina). El portátil Windows solo graba y envía el `.wav`.
- `datos/glosario.md` tiene que existir **también en el servidor**: lo usan
  tanto la transcripción como el resumen. Al no versionarse, hay que copiarlo
  a mano igual que los modelos (ver `DEPLOY_OFFLINE.md`).
- La ruta de la BD será configurable con `--db` / variable de entorno, para no
  quedar atados a esta decisión.

## 6. Privacidad y aviso legal

El repo ya advierte que grabar requiere consentimiento de todos los
participantes. Este plan **amplía el alcance de esos datos** y conviene dejarlo
explícito en el `README.md`:

- Se pasa de ficheros sueltos a un **histórico persistente y consultable** de
  lo que dice cada persona identificada por nombre.
- Los **perfiles de voz (Fase 3) son datos biométricos** a efectos de RGPD. El
  consentimiento para grabar una reunión no cubre automáticamente crear un
  perfil de voz reutilizable de alguien.
- Recomendación: `datos/` en `.gitignore` (nunca versionar), definir una
  política de retención (por ejemplo, borrar audio a los 30 días y conservar
  solo resúmenes) y añadir a `memoria.py` una utilidad `--olvidar <persona>`
  que purgue su perfil de voz y anonimice sus filas.

Estos puntos son una recomendación, no un bloqueo: la decisión sobre qué se
guarda y cuánto tiempo es tuya.

## 7. Cuestiones abiertas

Las cuatro cuestiones iniciales están resueltas en la sección 0, salvo el
umbral de similitud de voz, que no es una decisión sino una medición: hay que
calibrarlo contra grabaciones reales al abordar la Fase 3.

Quedan pendientes de decidir, más adelante:

1. Política de retención de audio y transcripciones (sección 6).
2. Si `ask_teams.py` se usará por SSH contra el servidor o conviene un modo
   cliente/servidor mínimo.
3. Si la sección "Dudosos" del glosario debe alimentarse automáticamente:
   detectar términos que el LLM marca como no claros y proponerlos.

---

## Resultado de la Fase 4 (2026-09-09)

Ficheros nuevos: `llm.py` (cliente LiteLLM, con streaming y embeddings),
`rag.py` (índice semántico), `indexar_teams.py` (su CLI) y `ask_teams.py`. En
`memoria.py`, dos consultas transversales (`updates_recientes`,
`riesgos_recientes`); en `summarize_teams.py`, `call_litellm` pasa a delegar
en `llm.chat` y se indexa la reunión al terminar; en `tests/`, 71 pruebas más
(193 en total).

Decisiones y hallazgos:

1. **Se adelantó la búsqueda vectorial**, que este plan dejaba como escape a
   futuro (sección Fase 4, nota sobre FTS5). El motivo es el que ya anticipaba
   la nota: FTS5 no reduce a la raíz, así que «bloqueado» no encuentra
   «bloquear», y la expansión de términos por el LLM lo tapa solo a medias.
   La recuperación es **híbrida** con Reciprocal Rank Fusion, y sigue sin
   entrar ninguna base vectorial: `sqlite-vec` es una extensión de SQLite.
2. **El índice vive en `datos/indice.db`, no en `meetings.db`.** Es un
   artefacto derivado y reconstruible; separarlo mantiene el histórico legible
   con la stdlib pura —el venv de Whisper y una API sin la extensión no se
   rompen— y no obliga a subir `ESQUEMA_VERSION` antes de que la fase I6 lo
   necesite para `overrides`.
3. **`nomic-embed-text-v1.5` se descartó por ser solo inglés.** Era el
   candidato inicial; Nomic lo dice explícitamente y con transcripciones en
   castellano la recuperación se degrada **sin dar ningún error**. El modelo
   por defecto es **bge-m3** (1024 dims, multilingüe, 8k de contexto), que
   además no necesita prefijos de tarea.
4. **Mezclar vectores de dos modelos es el fallo silencioso de esto**, y por
   eso el CLI se niega en seco y pide `--reconstruir`: las distancias se
   siguen calculando igual, solo que no significan nada.
5. **El intérprete es un lujo, no un requisito.** Si el LLM no responde o
   devuelve JSON roto, se busca con la pregunta cruda, que funciona
   sorprendentemente bien: las palabras que alguien escribe son casi siempre
   las que están en la transcripción. Nada sin validar entra en una consulta,
   con el mismo criterio de `normalizar()`.
6. **La rama agregada deduplica.** Se vio con datos: el mismo bloqueo repetido
   en cinco dailys son cinco copias del mismo texto que desplazan del contexto
   a las fuentes que sí dicen algo distinto. Pasó de 29 fuentes a 14 en la
   misma pregunta.
7. **`summarize_teams.py` indexa al terminar**, y si falla solo avisa por
   stderr: el `.md` y la base ya están guardados, que es lo que importa.
   `--sin-indice` lo desactiva.

Verificado:

- **193 tests en verde**, y la suite sigue pasando sin `sqlite-vec` ni
  FastAPI: esas pruebas se saltan solas, como en el servidor de
  transcripción.
- Contra la base real (1 reunión, 145 segmentos): 9 fragmentos indexados, un
  segundo pase sin gastar ni una llamada, y `imputacion` recuperando su
  fragmento con el hablante y la línea correctos.
- Contra una base de demostración de 6 reuniones y 240 segmentos, con un
  LiteLLM simulado que hace chat, streaming y embeddings: la pregunta
  agregada, la puntual, las citas resolviendo a segmentos que existen, y la
  degradación a búsqueda literal al retirar el índice.
- **Contra los modelos reales del servidor** (Ubuntu + L4, vía SSH): índice
  reconstruido con bge-m3 de verdad —9 fragmentos, 1024 dimensiones, 2
  segundos—, la pregunta puntual «¿qué dijo Francisco sobre ULS?» respondida
  correctamente y citando el segmento 80, y la agregada respondiendo «no
  consta una duración concreta», que es lo honesto con una sola reunión en la
  base. Las 194 pruebas también pasan allí (62 se saltan por no haber FastAPI
  en el venv de Whisper).

Dos cosas que solo se vieron con el modelo real:

8. **No hay proxy LiteLLM en ese servidor**: son dos `llama-server` sueltos
   (8000 el chat `qwen3.8-27b`, 8085 `bge-m3`). El código asumía una sola URL
   para chat y embeddings, así que se añadió `TEAMS_EMBED_BASE_URL`; sin ella
   habría que elegir cuál de los dos modelos funciona. Y la API key dejó de
   ser obligatoria: un `llama-server` directo no pide ninguna.
9. **El intérprete no era «una llamada barata».** `qwen3.8-27b` es un modelo
   de razonamiento y gastaba ~30 s pensando antes de escribir el JSON de
   filtros. Pidiéndole `reasoning_effort: "none"` —con reintento sin el campo
   si el backend no lo conoce— la pregunta completa bajó de **1m42s a 28s**.
