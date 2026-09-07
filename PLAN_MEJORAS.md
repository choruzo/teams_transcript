# Plan de mejoras: de "transcriptor" a "memoria del equipo"

Estado: propuesta, sin implementar. Redactado el 2026-09-07.

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

## Fase 0 — Glosario del proyecto

**Resuelve:** P5. **Esfuerzo:** bajo. **Riesgo:** ninguno.

Fichero `datos/glosario.md` mantenido a mano, con tres secciones: personas del
equipo, productos/componentes (OLS, WLS, VTS, banda base, GACF, VCD, GTR,
Coverity, SBR, XSD...) y siglas internas.

Se usa en dos puntos:

1. **Whisper** (`transcribe_teams.py`): nuevo flag `--glosario RUTA`, que
   alimenta el parámetro `initial_prompt` del modelo. Mejora notablemente el
   reconocimiento de siglas y nombres propios.
   - *Limitación importante*: `initial_prompt` de Whisper está acotado a unos
     224 tokens (la mitad de la ventana del decoder). No cabe un glosario
     largo, así que el fichero tendrá una sección corta marcada como
     "prioritaria" para Whisper, y el resto se usará solo en el LLM.
2. **LLM** (`summarize_teams.py`): el glosario completo se inyecta en el
   system prompt para que el modelo corrija en el resumen los términos que
   Whisper haya destrozado, en vez de arrastrar "cuisine" o "el son".

**Criterio de aceptación:** reprocesar `20260907_090437_mixed.wav` con
glosario y comprobar que "Goverity" pasa a "Coverity" y que las siglas del
proyecto aparecen bien escritas en el resumen.

---

## Fase 1 — Salida estructurada y almacén SQLite

**Resuelve:** P3, y es la base de todas las fases siguientes.
**Esfuerzo:** medio. **Riesgo:** bajo (no toca la parte frágil del pipeline).

### 1.1 Esquema de `datos/meetings.db`

```sql
CREATE TABLE meetings (
  id              INTEGER PRIMARY KEY,
  fecha           TEXT NOT NULL,        -- ISO-8601, de la fecha del audio
  titulo          TEXT,                 -- "Daily", editable
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

### 1.2 El LLM devuelve JSON, no Markdown

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

---

## Fase 2 — Seguimiento de acciones entre reuniones

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

---

## Fase 3 — Perfiles de voz persistentes

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

---

## Fase 4 — `ask_teams.py`: preguntar sobre el pasado

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

## Fase 5 — `report_teams.py`: informes agregados

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

## 4. Orden de implementación recomendado

| Orden | Fase | Esfuerzo | Riesgo | Desbloquea |
|---|---|---|---|---|
| 1 | Fase 0 — Glosario | Bajo | Nulo | Calidad de todo lo demás |
| 2 | Fase 1 — JSON + SQLite | Medio | Bajo | Fases 2, 4, 5 |
| 3 | Fase 2 — Arrastres | Bajo | Bajo | El valor de gestión |
| 4 | Fase 4 — `ask_teams.py` | Medio | Bajo | Consulta del histórico |
| 5 | Fase 5 — `report_teams.py` | Bajo | Nulo | Informes |
| 6 | Fase 3 — Perfiles de voz | Medio-alto | **Alto** | Calidad a largo plazo |
| 7 | Fase 6 — Map-reduce | Medio | Medio | Reuniones largas |

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
- El flujo de dos máquinas se mantiene: `datos/meetings.db` vive donde se
  ejecuta el resumen. Si transcripción y resumen ocurren en máquinas
  distintas, se copia el `.txt` como hasta ahora; la BD no viaja.

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

1. ¿Se procesan solo dailys, o también otro tipo de reuniones? Afecta al
   prompt (hoy está muy especializado en dailys) y sugeriría un campo `tipo`
   en `meetings` con plantillas de prompt por tipo.
2. ¿La BD debe vivir en el PC Windows o en el servidor Linux? Cambia dónde se
   ejecuta `summarize_teams.py`.
3. ¿Interesa exportar las acciones abiertas a Planner/Jira, o el Markdown es
   suficiente? En la transcripción se menciona "subir documentos al planner".
4. Umbral de similitud de voz: hay que calibrarlo con grabaciones reales.
