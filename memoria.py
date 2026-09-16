"""
memoria.py

Capa de acceso al almacen SQLite del historico de reuniones
(`datos/meetings.db`). Modulo compartido: ningun otro script escribe SQL
suelto, todos pasan por aqui.

Solo stdlib (`sqlite3`), como el resto del proyecto. La busqueda de texto
completo usa FTS5, incluido en el sqlite3 de CPython.

Uso tipico:

    import memoria

    conn = memoria.conectar()                 # crea el fichero y el esquema
    meeting_id = memoria.crear_reunion(conn, fecha="2026-09-07", ...)
    memoria.insertar_segmentos(conn, meeting_id, segmentos)
    conn.commit()

La ruta de la base se resuelve en este orden: argumento explicito >
variable de entorno TEAMS_DB > `datos/meetings.db` junto al repo.
"""

import hashlib
import json
import os
import re
import sqlite3
import unicodedata
from datetime import date
from pathlib import Path

ESQUEMA_VERSION = 3

RUTA_POR_DEFECTO = Path(__file__).resolve().parent / "datos" / "meetings.db"
VARIABLE_ENTORNO = "TEAMS_DB"

# Las rutas de audio y transcripcion se guardan absolutas y tal como las vio la
# maquina que proceso la reunion. Quien lea la BD desde otro sitio (la API en
# un contenedor, con `grabaciones/` montado en otro punto) traduce el prefijo
# con estas dos variables en vez de reescribir la base.
VARIABLE_RAIZ_ORIGEN = "TEAMS_RAIZ_ORIGEN"
VARIABLE_RAIZ_LOCAL = "TEAMS_RAIZ_LOCAL"

TIPOS_REUNION = ("daily", "workshop", "retro", "planning")

ESTADOS_ACCION = (
    "abierta",
    "en_progreso",
    "completada",
    "bloqueada",
    "abandonada",
)

# Una accion deja de estar viva cuando llega a uno de estos dos estados. Se
# declara aparte porque la distincion "sigue en el aire / ya no" aparece en
# media docena de consultas y en las metricas de la interfaz.
ESTADOS_CERRADOS = ("completada", "abandonada")

# Menciones a partir de las cuales una accion abierta se considera estancada.
# Vivia en summarize_teams.py, pero la interfaz necesita el mismo numero para
# no contradecir a la seccion "Arrastres" del .md: el umbral es un hecho del
# dominio, no del generador de resumenes.
UMBRAL_ESTANCAMIENTO = 3

# El LLM razona mejor con etiquetas que con numeros; la BD guarda un valor
# comparable. Este es el unico punto donde se traduce entre ambos.
CONFIANZA_A_NUMERO = {"alta": 0.9, "media": 0.6, "baja": 0.3}

# Similitud minima (`difflib.SequenceMatcher` sobre el texto normalizado) para
# que, al reprocesar una reunion, una accion que devuelve el modelo se considere
# la misma que una ya guardada (Fase 7). **Es provisional**: igual que el umbral
# de voz, es una medicion y no una preferencia, y se calibra con
# `summarize_teams.py --dry-run-reconciliacion` reprocesando dailys reales con
# el mismo modelo y con otro.
UMBRAL_RECONCILIACION = 0.75

# Quien hizo una correccion. Solo documenta: las dos valen lo mismo.
ORIGENES_CORRECCION = ("web", "cli")

_ESQUEMA = """
CREATE TABLE IF NOT EXISTS meetings (
  id              INTEGER PRIMARY KEY,
  uid             TEXT,
  fecha           TEXT NOT NULL,
  titulo          TEXT,
  tipo            TEXT NOT NULL DEFAULT 'daily',
  audio_path      TEXT,
  transcript_path TEXT,
  duracion_seg    REAL,
  modelo_whisper  TEXT,
  modelo_llm      TEXT,
  resumen         TEXT,
  datos_json      TEXT,
  creado_en       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS personas (
  id       INTEGER PRIMARY KEY,
  nombre   TEXT NOT NULL UNIQUE,
  alias    TEXT,
  activo   INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS meeting_speakers (
  meeting_id  INTEGER NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
  etiqueta    TEXT NOT NULL,
  persona_id  INTEGER REFERENCES personas(id),
  confianza   REAL,
  metodo      TEXT,
  PRIMARY KEY (meeting_id, etiqueta)
);

CREATE TABLE IF NOT EXISTS updates (
  id             INTEGER PRIMARY KEY,
  meeting_id     INTEGER NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
  persona_id     INTEGER REFERENCES personas(id),
  trabajo        TEXT,
  bloqueos       TEXT,
  proximos_pasos TEXT
);

CREATE TABLE IF NOT EXISTS actions (
  id                  INTEGER PRIMARY KEY,
  descripcion         TEXT NOT NULL,
  persona_id          INTEGER REFERENCES personas(id),
  meeting_id_origen   INTEGER NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
  meeting_id_ultima   INTEGER REFERENCES meetings(id),
  estado              TEXT NOT NULL,
  menciones           INTEGER NOT NULL DEFAULT 1,
  cerrada_en          TEXT,
  uid                 TEXT,
  descripcion_llm     TEXT,
  descartada_en       TEXT,
  motivo_descarte     TEXT,
  absorbida_por       INTEGER REFERENCES actions(id),
  revisar             INTEGER NOT NULL DEFAULT 0
);

-- Esquema 3 (Fase 7). `menciones`, `meeting_id_ultima`, `estado` y
-- `cerrada_en` de `actions` pasan a ser una cache de lo que dicen estas
-- menciones (mas las correcciones de estado); `_recalcular_accion` la rehace.
CREATE TABLE IF NOT EXISTS action_mentions (
  action_id   INTEGER NOT NULL REFERENCES actions(id) ON DELETE CASCADE,
  meeting_id  INTEGER NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
  estado      TEXT NOT NULL,
  comentario  TEXT,
  PRIMARY KEY (action_id, meeting_id)
);

CREATE TABLE IF NOT EXISTS action_dependencias (
  action_id       INTEGER NOT NULL REFERENCES actions(id) ON DELETE CASCADE,
  depende_de_id   INTEGER NOT NULL REFERENCES actions(id) ON DELETE CASCADE,
  creado_en       TEXT NOT NULL,
  PRIMARY KEY (action_id, depende_de_id),
  CHECK (action_id != depende_de_id)
);

-- Historial de lo que ha hecho un humano. No se reaplica: documenta y protege.
CREATE TABLE IF NOT EXISTS correcciones (
  id              INTEGER PRIMARY KEY,
  action_id       INTEGER NOT NULL REFERENCES actions(id) ON DELETE CASCADE,
  campo           TEXT NOT NULL,
  valor_anterior  TEXT,
  valor_nuevo     TEXT,
  origen          TEXT NOT NULL,
  creado_en       TEXT NOT NULL,
  deshecha_en     TEXT
);

-- La unica entrada de lectura de las acciones: lo descartado y lo fusionado
-- no se ve, no cuenta y no se le ofrece al LLM.
CREATE VIEW IF NOT EXISTS acciones_vigentes AS
  SELECT * FROM actions WHERE descartada_en IS NULL AND absorbida_por IS NULL;

CREATE TABLE IF NOT EXISTS risks (
  id          INTEGER PRIMARY KEY,
  meeting_id  INTEGER NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
  descripcion TEXT NOT NULL,
  area        TEXT,
  severidad   TEXT
);

CREATE TABLE IF NOT EXISTS segments (
  id          INTEGER PRIMARY KEY,
  meeting_id  INTEGER NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
  idx         INTEGER NOT NULL,
  inicio      REAL,
  fin         REAL,
  etiqueta    TEXT,
  persona_id  INTEGER REFERENCES personas(id),
  texto       TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_meetings_uid ON meetings(uid);
CREATE INDEX IF NOT EXISTS idx_segments_meeting ON segments(meeting_id, idx);
CREATE INDEX IF NOT EXISTS idx_actions_estado ON actions(estado);
CREATE INDEX IF NOT EXISTS idx_meetings_fecha ON meetings(fecha);
CREATE UNIQUE INDEX IF NOT EXISTS idx_actions_uid ON actions(uid);
CREATE INDEX IF NOT EXISTS idx_actions_origen ON actions(meeting_id_origen);
CREATE INDEX IF NOT EXISTS idx_mentions_meeting ON action_mentions(meeting_id);
CREATE INDEX IF NOT EXISTS idx_correcciones_accion ON correcciones(action_id);

CREATE VIRTUAL TABLE IF NOT EXISTS segments_fts USING fts5(
  texto,
  content='segments', content_rowid='id',
  tokenize="unicode61 remove_diacritics 2"
);

-- La tabla FTS es de 'contenido externo': hay que sincronizarla a mano.
CREATE TRIGGER IF NOT EXISTS segments_ai AFTER INSERT ON segments BEGIN
  INSERT INTO segments_fts(rowid, texto) VALUES (new.id, new.texto);
END;

CREATE TRIGGER IF NOT EXISTS segments_ad AFTER DELETE ON segments BEGIN
  INSERT INTO segments_fts(segments_fts, rowid, texto)
  VALUES ('delete', old.id, old.texto);
END;

CREATE TRIGGER IF NOT EXISTS segments_au AFTER UPDATE ON segments BEGIN
  INSERT INTO segments_fts(segments_fts, rowid, texto)
  VALUES ('delete', old.id, old.texto);
  INSERT INTO segments_fts(rowid, texto) VALUES (new.id, new.texto);
END;
"""


def ruta_bd(explicita: Path | str | None = None) -> Path:
    if explicita:
        return Path(explicita)
    del_entorno = os.environ.get(VARIABLE_ENTORNO)
    if del_entorno:
        return Path(del_entorno)
    return RUTA_POR_DEFECTO


def sin_acentos(texto: str | None) -> str:
    """`Sesion` y `sesión` deben encontrarse igual.

    Es la version en Python de lo que el tokenizador FTS5 hace con
    `remove_diacritics 2` sobre los segmentos: se registra como funcion SQL en
    `conectar` para que el filtro de texto del tablero de acciones (I3) trate
    los acentos igual que la busqueda de I4. Sin esto, buscar "accion" no
    encontraria "acción", que es justo lo que se teclea con prisa.
    """
    if not texto:
        return ""
    descompuesto = unicodedata.normalize("NFD", texto.lower())
    return "".join(c for c in descompuesto if unicodedata.category(c) != "Mn")


def _preparar(conn: sqlite3.Connection) -> sqlite3.Connection:
    """Lo comun a las dos formas de abrir: `row_factory` y funciones propias.

    `create_function` vale tambien en una conexion `query_only`: define como se
    interpreta una consulta, no escribe nada.
    """
    conn.row_factory = sqlite3.Row
    conn.create_function("sin_acentos", 1, sin_acentos, deterministic=True)
    return conn


def conectar(
    ruta: Path | str | None = None,
    solo_lectura: bool = False,
    entre_hilos: bool = False,
) -> sqlite3.Connection:
    """Abre (creando si hace falta) la BD y garantiza el esquema.

    Con `solo_lectura` la conexion rechaza cualquier escritura y **no** toca el
    esquema: es la que debe usar todo lo que solo consulta (la API web), para
    que un error de programacion no pueda corromper el historico.

    Con `entre_hilos` se levanta la comprobacion de hilo de sqlite3. Hace falta
    en la API: FastAPI ejecuta las rutas sincronas en un pool y puede abrir la
    conexion en un hilo y cerrarla en otro, lo que aborta con "SQLite objects
    created in a thread can only be used in that same thread" **solo cuando
    llegan varias peticiones a la vez**. La conexion se crea y se destruye
    dentro de una peticion y no se comparte con ninguna otra, asi que la
    comprobacion no protege de nada aqui. Quien reutilice una conexion entre
    peticiones tendra que serializar el acceso por su cuenta.
    """
    path = ruta_bd(ruta)
    if solo_lectura:
        if not path.exists():
            raise FileNotFoundError(f"No existe la base de datos: {path}")
        conn = _preparar(sqlite3.connect(path, check_same_thread=not entre_hilos))
        # Se usa `query_only` y no la URI `mode=ro` a proposito: con la base en
        # modo WAL, una conexion abierta como `mode=ro` no puede crear el
        # fichero `-shm` que SQLite necesita para leer, y falla justo cuando
        # nadie mas esta escribiendo (el caso normal). `query_only` da la misma
        # garantia -- cualquier INSERT/UPDATE/DELETE lanza excepcion -- sin ese
        # problema.
        conn.execute("PRAGMA query_only = ON")
        return conn

    path.parent.mkdir(parents=True, exist_ok=True)
    conn = _preparar(sqlite3.connect(path, check_same_thread=not entre_hilos))
    conn.execute("PRAGMA foreign_keys = ON")
    # WAL permite que la API lea mientras el pipeline escribe. Es una propiedad
    # persistente de la base: basta con fijarlo una vez, pero es idempotente.
    # (No vale si `datos/` acabara en un sistema de ficheros en red.)
    conn.execute("PRAGMA journal_mode = WAL")
    try:
        _inicializar(conn)
    except Exception:
        # Sin esto, un fallo de migracion deja el fichero abierto: en Windows
        # nadie puede borrarlo ni moverlo despues.
        conn.close()
        raise
    return conn


def _columnas(conn: sqlite3.Connection, tabla: str) -> set[str]:
    return {fila[1] for fila in conn.execute(f"PRAGMA table_info({tabla})")}


def _inicializar(conn: sqlite3.Connection) -> None:
    """Crea el esquema si falta y migra el existente. Idempotente.

    `PRAGMA user_version` marca la version del esquema. Las bases nuevas se
    crean ya en la ultima con `_ESQUEMA`; las anteriores pasan por `_migrar`,
    porque `CREATE TABLE IF NOT EXISTS` no anade columnas a una tabla que ya
    existe.
    """
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version > ESQUEMA_VERSION:
        raise RuntimeError(
            f"La base de datos usa el esquema version {version}, superior al "
            f"que entiende este codigo ({ESQUEMA_VERSION}). Actualiza el repo."
        )
    # Migrar antes de `_ESQUEMA`, no despues: el script crea el indice UNIQUE
    # sobre `meetings.uid`, y en una base anterior esa columna todavia no
    # existe.
    if version < ESQUEMA_VERSION:
        _migrar(conn, version)
    conn.executescript(_ESQUEMA)
    if version < ESQUEMA_VERSION:
        conn.execute(f"PRAGMA user_version = {ESQUEMA_VERSION}")
    conn.commit()


def _migrar(conn: sqlite3.Connection, desde: int) -> None:
    """Lleva una base existente hasta `ESQUEMA_VERSION`.

    `desde` es 0 en una base recien creada (donde `_ESQUEMA` ya lo ha dejado
    todo hecho) y la version anterior en una que venia de antes.
    """
    if desde and desde < 2:
        # v1 -> v2: `meetings.uid`, identidad estable de una reunion.
        if "uid" not in _columnas(conn, "meetings"):
            conn.execute("ALTER TABLE meetings ADD COLUMN uid TEXT")
        for fila in conn.execute(
            "SELECT id, transcript_path FROM meetings WHERE uid IS NULL"
        ).fetchall():
            conn.execute(
                "UPDATE meetings SET uid = ? WHERE id = ?",
                (_uid_disponible(conn, fila["transcript_path"]), fila["id"]),
            )
        # El indice UNIQUE lo crea `_ESQUEMA`, justo despues de esto.

    if desde and desde < 3:
        _migrar_a_3(conn)


# Columnas que el esquema 3 anade a `actions`, con su definicion para ALTER.
_COLUMNAS_ACCIONES_V3 = (
    ("uid", "TEXT"),
    ("descripcion_llm", "TEXT"),
    ("descartada_en", "TEXT"),
    ("motivo_descarte", "TEXT"),
    ("absorbida_por", "INTEGER REFERENCES actions(id)"),
    ("revisar", "INTEGER NOT NULL DEFAULT 0"),
)


def _existe_tabla(conn: sqlite3.Connection, nombre: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (nombre,)
        ).fetchone()
        is not None
    )


def _migrar_a_3(conn: sqlite3.Connection) -> None:
    """v2 -> v3: identidad de las acciones, menciones, dependencias y correcciones.

    Las menciones se rellenan con lo unico que habia: la reunion de origen y,
    si es distinta, la ultima. Las intermedias de una accion con mas de dos se
    pierden (el contador `menciones` se conserva tal cual hasta que algo la
    recalcule); `python memoria.py --migrar` dice cuantas son antes de migrar.
    """
    if _existe_tabla(conn, "actions"):
        actuales = _columnas(conn, "actions")
        for nombre, definicion in _COLUMNAS_ACCIONES_V3:
            if nombre not in actuales:
                conn.execute(f"ALTER TABLE actions ADD COLUMN {nombre} {definicion}")
    # Las tablas nuevas tienen que existir antes de rellenarlas. `_ESQUEMA` es
    # idempotente y ya se puede ejecutar: todas las columnas que indexa existen.
    conn.executescript(_ESQUEMA)

    for fila in conn.execute(
        "SELECT id, meeting_id_origen FROM actions WHERE uid IS NULL "
        "ORDER BY meeting_id_origen, id"
    ).fetchall():
        conn.execute(
            "UPDATE actions SET uid = ? WHERE id = ?",
            (_uid_accion(conn, fila["meeting_id_origen"]), fila["id"]),
        )
    conn.execute(
        "UPDATE actions SET descripcion_llm = descripcion WHERE descripcion_llm IS NULL"
    )
    # Mencion de origen. Si la accion se toco despues, su estado de aquel dia no
    # se guardo: 'abierta' es lo que devolvia `_deshacer_arrastres` en v2.
    conn.execute(
        """
        INSERT OR IGNORE INTO action_mentions (action_id, meeting_id, estado)
        SELECT id, meeting_id_origen,
               CASE WHEN meeting_id_ultima IS NULL
                         OR meeting_id_ultima = meeting_id_origen
                    THEN estado ELSE 'abierta' END
          FROM actions
        """
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO action_mentions (action_id, meeting_id, estado)
        SELECT id, meeting_id_ultima, estado
          FROM actions
         WHERE meeting_id_ultima IS NOT NULL
           AND meeting_id_ultima != meeting_id_origen
        """
    )
    # El comentario de cada arrastre solo vivia en el JSON del modelo.
    for reunion in conn.execute(
        "SELECT id, datos_json FROM meetings WHERE datos_json IS NOT NULL"
    ).fetchall():
        try:
            arrastres = json.loads(reunion["datos_json"]).get("arrastres") or []
        except (json.JSONDecodeError, TypeError, AttributeError):
            continue
        for arrastre in arrastres:
            if not isinstance(arrastre, dict) or not arrastre.get("comentario"):
                continue
            conn.execute(
                "UPDATE action_mentions SET comentario = ? "
                "WHERE action_id = ? AND meeting_id = ?",
                (str(arrastre["comentario"]), arrastre.get("action_id"), reunion["id"]),
            )


def menciones_perdidas_en_migracion(conn: sqlite3.Connection) -> int:
    """Acciones de una base v2 cuyas menciones intermedias no se pueden recuperar."""
    if not _existe_tabla(conn, "actions"):
        return 0
    return conn.execute(
        """
        SELECT count(*) FROM actions
         WHERE menciones > CASE WHEN meeting_id_ultima IS NULL
                                     OR meeting_id_ultima = meeting_id_origen
                                THEN 1 ELSE 2 END
        """
    ).fetchone()[0]


# --------------------------------------------------------------------------
# Rutas
# --------------------------------------------------------------------------


def ruta_local(ruta: str | None) -> Path | None:
    """Traduce una ruta guardada en la BD a una valida en esta maquina.

    `meetings.audio_path` y `transcript_path` son rutas absolutas de la maquina
    que proceso la reunion. Quien lea la base desde otro sitio -- tipicamente la
    API en un contenedor, con `grabaciones/` montado en otro punto -- define
    TEAMS_RAIZ_ORIGEN y TEAMS_RAIZ_LOCAL y esta funcion sustituye el prefijo.
    Sin esas variables devuelve la ruta tal cual.
    """
    if not ruta:
        return None
    origen = os.environ.get(VARIABLE_RAIZ_ORIGEN)
    local = os.environ.get(VARIABLE_RAIZ_LOCAL)
    if not origen or not local:
        return Path(ruta)
    # Comparacion con separadores normalizados: la ruta pudo escribirse en
    # Windows y leerse en Linux.
    plana = ruta.replace("\\", "/")
    prefijo = origen.replace("\\", "/").rstrip("/")
    if plana.lower().startswith(prefijo.lower()):
        resto = plana[len(prefijo) :].lstrip("/")
        return Path(local) / resto if resto else Path(local)
    return Path(ruta)


_UID_INVALIDO = re.compile(r"[^a-z0-9_-]+")


def uid_transcripcion(transcript_path: Path | str | None) -> str | None:
    """Identidad estable de una reunion, derivada del nombre de su transcripcion.

    Se usa en las URLs de la interfaz y como ancla de las correcciones
    manuales, asi que tiene que sobrevivir a un reprocesado. Desde el esquema
    3 el `id` tambien sobrevive (la fila se actualiza en su sitio), pero no
    viaja entre bases: la URL sigue yendo por `uid`.

    Del nombre y no de la ruta completa a proposito: mover `grabaciones/` de
    sitio, o procesar la misma transcripcion en otra maquina, no deberia
    cambiar la identidad de la reunion.
    """
    if not transcript_path:
        return None
    base = _UID_INVALIDO.sub("-", Path(transcript_path).stem.lower()).strip("-")
    return base[:60] if base else "reunion"


def _uid_disponible(conn: sqlite3.Connection, transcript_path: str | None) -> str | None:
    """`uid_transcripcion` evitando chocar con una reunion ya registrada.

    Dos transcripciones distintas pueden tener el mismo nombre en carpetas
    distintas; el indice UNIQUE rechazaria la segunda. En ese caso se desempata
    con un resumen corto de la ruta completa, que si es unica.
    """
    base = uid_transcripcion(transcript_path)
    if base is None:
        return None
    fila = conn.execute("SELECT id FROM meetings WHERE uid = ?", (base,)).fetchone()
    if not fila:
        return base
    sufijo = hashlib.sha1(str(transcript_path).encode("utf-8")).hexdigest()[:6]
    return f"{base[:53]}-{sufijo}"


# --------------------------------------------------------------------------
# Personas
# --------------------------------------------------------------------------


def obtener_o_crear_persona(conn: sqlite3.Connection, nombre: str | None) -> int | None:
    """Devuelve el id de la persona, creandola si no existe.

    El emparejamiento es por nombre (sin distinguir mayusculas) y por la lista
    de alias. Un nombre vacio o generico (SPEAKER_XX, 'no identificado')
    devuelve None: preferimos una fila sin persona a inventarnos una.
    """
    if not nombre:
        return None
    nombre = " ".join(str(nombre).split())
    if not nombre:
        return None
    bajo = nombre.lower()
    if bajo.startswith("speaker_") or bajo in {
        "no identificado",
        "desconocido",
        "n/a",
        "-",
    }:
        return None

    fila = conn.execute(
        "SELECT id FROM personas WHERE lower(nombre) = ?", (bajo,)
    ).fetchone()
    if fila:
        return fila["id"]

    for otra in conn.execute("SELECT id, alias FROM personas WHERE alias IS NOT NULL"):
        try:
            alias = json.loads(otra["alias"]) or []
        except (json.JSONDecodeError, TypeError):
            continue
        if any(str(a).lower() == bajo for a in alias):
            return otra["id"]

    cur = conn.execute("INSERT INTO personas (nombre) VALUES (?)", (nombre,))
    return cur.lastrowid


def anadir_alias(conn: sqlite3.Connection, nombre: str, alias: str) -> None:
    """Registra un alias adicional para una persona (p. ej. 'Javi' -> 'Javier M.')."""
    persona_id = obtener_o_crear_persona(conn, nombre)
    if persona_id is None:
        return
    fila = conn.execute(
        "SELECT alias FROM personas WHERE id = ?", (persona_id,)
    ).fetchone()
    try:
        actuales = json.loads(fila["alias"]) if fila["alias"] else []
    except (json.JSONDecodeError, TypeError):
        actuales = []
    if alias not in actuales:
        actuales.append(alias)
    conn.execute(
        "UPDATE personas SET alias = ? WHERE id = ?",
        (json.dumps(actuales, ensure_ascii=False), persona_id),
    )


# --------------------------------------------------------------------------
# Reuniones
# --------------------------------------------------------------------------


def crear_reunion(
    conn: sqlite3.Connection,
    *,
    fecha: str,
    titulo: str | None = None,
    tipo: str = "daily",
    audio_path: str | None = None,
    transcript_path: str | None = None,
    duracion_seg: float | None = None,
    modelo_whisper: str | None = None,
    modelo_llm: str | None = None,
    resumen: str | None = None,
    datos_json: dict | None = None,
    reemplazar: bool = True,
) -> int:
    """Registra la reunion y devuelve su id.

    Con `reemplazar` (por defecto), volver a procesar la misma transcripcion
    **actualiza la fila existente** en vez de duplicarla: el pipeline se
    ejecuta a mano y se repite a menudo mientras se afinan prompts. Se borran
    sus hijos regenerables (segmentos, updates, riesgos, hablantes y las
    menciones que hizo a acciones ajenas), pero **no sus acciones**: esas las
    reconcilia `insertar_acciones` con lo que devuelva el modelo esta vez, para
    que las correcciones humanas, las fusiones y las dependencias sobrevivan
    (Fase 7). Hasta la v2 la fila se borraba y la cascada se las llevaba.

    La identidad de la reunion (`id` y `uid`) se conserva sin trucos: la fila
    es la misma.
    """
    if tipo not in TIPOS_REUNION:
        raise ValueError(f"Tipo de reunion desconocido: {tipo!r}")

    valores = (
        fecha,
        titulo,
        tipo,
        audio_path,
        transcript_path,
        duracion_seg,
        modelo_whisper,
        modelo_llm,
        resumen,
        json.dumps(datos_json, ensure_ascii=False) if datos_json else None,
    )

    if reemplazar and transcript_path:
        antiguas = conn.execute(
            "SELECT id, uid FROM meetings WHERE transcript_path = ? ORDER BY id",
            (transcript_path,),
        ).fetchall()
        if antiguas:
            conservada = antiguas[-1]
            uid = next((a["uid"] for a in antiguas if a["uid"]), None)
            # Duplicados de antes del indice UNIQUE: se borran como siempre.
            for sobrante in antiguas[:-1]:
                _deshacer_arrastres(conn, sobrante["id"])
                conn.execute("DELETE FROM meetings WHERE id = ?", (sobrante["id"],))
            meeting_id = conservada["id"]
            _deshacer_arrastres(conn, meeting_id)
            for tabla in ("segments", "updates", "risks", "meeting_speakers"):
                conn.execute(f"DELETE FROM {tabla} WHERE meeting_id = ?", (meeting_id,))
            conn.execute(
                """
                UPDATE meetings
                   SET fecha = ?, titulo = ?, tipo = ?, audio_path = ?,
                       transcript_path = ?, duracion_seg = ?, modelo_whisper = ?,
                       modelo_llm = ?, resumen = ?, datos_json = ?,
                       creado_en = datetime('now'), uid = ?
                 WHERE id = ?
                """,
                (*valores, uid or _uid_disponible(conn, transcript_path), meeting_id),
            )
            _marcar_para_reconciliar(conn, meeting_id)
            return meeting_id

    cur = conn.execute(
        """
        INSERT INTO meetings (
            uid, fecha, titulo, tipo, audio_path, transcript_path,
            duracion_seg, modelo_whisper, modelo_llm, resumen, datos_json,
            creado_en
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
        """,
        (_uid_disponible(conn, transcript_path), *valores),
    )
    return cur.lastrowid


def _deshacer_arrastres(conn: sqlite3.Connection, meeting_id: int) -> None:
    """Revierte el efecto de una reunion sobre acciones de reuniones anteriores.

    Se quitan las menciones que esta reunion hizo a acciones **ajenas** y se
    recalcula cada una desde las que le quedan. Desde el esquema 3 eso
    devuelve el estado real que tenian, no el "vuelven a abierta" que se
    asumia cuando el estado previo no se guardaba.
    """
    ajenas = [
        fila["action_id"]
        for fila in conn.execute(
            """
            SELECT am.action_id
              FROM action_mentions am
              JOIN actions a ON a.id = am.action_id
             WHERE am.meeting_id = ? AND a.meeting_id_origen != ?
            """,
            (meeting_id, meeting_id),
        ).fetchall()
    ]
    conn.execute(
        f"DELETE FROM action_mentions WHERE meeting_id = ? "
        f"AND action_id IN ({_placeholders(ajenas)})",
        (meeting_id, *ajenas),
    )
    for action_id in ajenas:
        _recalcular_accion(conn, action_id)
    # Una accion sin menciones (no deberia quedar ninguna tras migrar) no puede
    # seguir apuntando a esta reunion: el borrado de la fila fallaria.
    conn.execute(
        """
        UPDATE actions SET meeting_id_ultima = meeting_id_origen
         WHERE meeting_id_ultima = ? AND meeting_id_origen != ?
        """,
        (meeting_id, meeting_id),
    )


def registrar_hablantes(
    conn: sqlite3.Connection,
    meeting_id: int,
    hablantes: list[dict],
    metodo: str = "llm",
) -> dict[str, int]:
    """Guarda el mapeo etiqueta -> persona de esta reunion.

    Devuelve {etiqueta: persona_id} de los que si se han podido resolver, para
    poder etiquetar los segmentos con la persona.
    """
    mapa: dict[str, int] = {}
    for hablante in hablantes:
        etiqueta = str(hablante.get("etiqueta") or "").strip()
        if not etiqueta:
            continue
        persona_id = obtener_o_crear_persona(conn, hablante.get("nombre"))
        confianza = hablante.get("confianza")
        if isinstance(confianza, str):
            confianza = CONFIANZA_A_NUMERO.get(confianza.strip().lower())
        conn.execute(
            """
            INSERT OR REPLACE INTO meeting_speakers
                (meeting_id, etiqueta, persona_id, confianza, metodo)
            VALUES (?, ?, ?, ?, ?)
            """,
            (meeting_id, etiqueta, persona_id, confianza, metodo),
        )
        if persona_id is not None:
            mapa[etiqueta] = persona_id
    return mapa


def insertar_segmentos(
    conn: sqlite3.Connection,
    meeting_id: int,
    segmentos: list[dict],
    mapa_hablantes: dict[str, int] | None = None,
) -> int:
    """Inserta los segmentos de la transcripcion. Devuelve cuantos."""
    mapa = mapa_hablantes or {}
    filas = []
    for idx, seg in enumerate(segmentos):
        etiqueta = seg.get("etiqueta")
        filas.append(
            (
                meeting_id,
                idx,
                seg.get("inicio"),
                seg.get("fin"),
                etiqueta,
                mapa.get(etiqueta),
                seg["texto"],
            )
        )
    conn.executemany(
        """
        INSERT INTO segments (meeting_id, idx, inicio, fin, etiqueta, persona_id, texto)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        filas,
    )
    return len(filas)


def insertar_updates(
    conn: sqlite3.Connection, meeting_id: int, updates: list[dict]
) -> int:
    filas = []
    for upd in updates:
        persona_id = obtener_o_crear_persona(conn, upd.get("persona"))
        filas.append(
            (
                meeting_id,
                persona_id,
                upd.get("trabajo"),
                upd.get("bloqueos"),
                upd.get("proximos_pasos"),
            )
        )
    conn.executemany(
        """
        INSERT INTO updates (meeting_id, persona_id, trabajo, bloqueos, proximos_pasos)
        VALUES (?, ?, ?, ?, ?)
        """,
        filas,
    )
    return len(filas)


def insertar_riesgos(
    conn: sqlite3.Connection, meeting_id: int, riesgos: list[dict]
) -> int:
    filas = [
        (
            meeting_id,
            riesgo["descripcion"],
            riesgo.get("area"),
            riesgo.get("severidad"),
        )
        for riesgo in riesgos
        if riesgo.get("descripcion")
    ]
    conn.executemany(
        "INSERT INTO risks (meeting_id, descripcion, area, severidad) VALUES (?, ?, ?, ?)",
        filas,
    )
    return len(filas)


def insertar_acciones(
    conn: sqlite3.Connection, meeting_id: int, acciones: list[dict]
) -> int:
    """Da de alta las acciones nacidas en esta reunion, reconciliando si se reprocesa.

    Devuelve cuantas acciones de la reunion quedan registradas a partir de esta
    respuesta del modelo (conservadas + nuevas). El detalle lo da
    `reconciliar_acciones`.
    """
    resultado = reconciliar_acciones(conn, meeting_id, acciones)
    return resultado["conservadas"] + resultado["nuevas"]


# --------------------------------------------------------------------------
# Identidad, menciones y estado derivado de las acciones (Fase 7)
# --------------------------------------------------------------------------


def _uid_accion(conn: sqlite3.Connection, meeting_id: int) -> str:
    """`<uid reunion>-a<n>`: identidad estable, se asigna una vez.

    `n` es el siguiente al mayor ya usado con ese prefijo, no el numero de
    acciones: tras borrar una, el hueco no se rellena con otra distinta.
    """
    fila = conn.execute("SELECT uid FROM meetings WHERE id = ?", (meeting_id,)).fetchone()
    prefijo = f"{(fila['uid'] if fila and fila['uid'] else f'm{meeting_id}')}-a"
    mayor = 0
    for otra in conn.execute(
        "SELECT uid FROM actions WHERE substr(uid, 1, ?) = ?", (len(prefijo), prefijo)
    ):
        resto = otra["uid"][len(prefijo) :]
        if resto.isdigit():
            mayor = max(mayor, int(resto))
    return f"{prefijo}{mayor + 1}"


def _normalizar_accion(accion: dict) -> dict | None:
    descripcion = " ".join(str(accion.get("descripcion") or "").split())
    if not descripcion:
        return None
    estado = str(accion.get("estado") or "abierta").strip().lower()
    if estado not in ESTADOS_ACCION:
        estado = "abierta"
    return {
        "descripcion": descripcion,
        "estado": estado,
        "persona": str(accion.get("persona") or "").strip(),
    }


def _insertar_accion(conn: sqlite3.Connection, meeting_id: int, accion: dict) -> int:
    cur = conn.execute(
        """
        INSERT INTO actions (
            uid, descripcion, descripcion_llm, persona_id, meeting_id_origen,
            meeting_id_ultima, estado, menciones
        ) VALUES (?, ?, ?, ?, ?, ?, ?, 1)
        """,
        (
            _uid_accion(conn, meeting_id),
            accion["descripcion"],
            accion["descripcion"],
            obtener_o_crear_persona(conn, accion["persona"]),
            meeting_id,
            meeting_id,
            accion["estado"],
        ),
    )
    _anotar_mencion(conn, cur.lastrowid, meeting_id, accion["estado"])
    _recalcular_accion(conn, cur.lastrowid)
    return cur.lastrowid


def _anotar_mencion(
    conn: sqlite3.Connection,
    action_id: int,
    meeting_id: int,
    estado: str,
    comentario: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO action_mentions (action_id, meeting_id, estado, comentario)
        VALUES (?, ?, ?, ?)
        ON CONFLICT (action_id, meeting_id)
        DO UPDATE SET estado = excluded.estado, comentario = excluded.comentario
        """,
        (action_id, meeting_id, estado, comentario or None),
    )


def _correccion_vigente(
    conn: sqlite3.Connection, action_id: int, campo: str
) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT * FROM correcciones
         WHERE action_id = ? AND campo = ? AND deshecha_en IS NULL
         ORDER BY creado_en DESC, id DESC
         LIMIT 1
        """,
        (action_id, campo),
    ).fetchone()


def _derivar_accion(
    conn: sqlite3.Connection, action_id: int, excluir_meeting_id: int | None = None
) -> dict | None:
    """Estado, menciones, ultima reunion y fecha de cierre, calculados.

    - `menciones` y `meeting_id_ultima` salen de la **union** de las menciones
      de la accion y de las que ha absorbido: la fusion es la union de dos
      conjuntos, y una reunion que nombro a las dos cuenta una vez.
    - `estado` sale de las menciones **propias**: fusionar no cambia el estado
      de la principal.
    - `cerrada_en` es la fecha de la reunion donde paso a cerrada (D4).
    - Una correccion humana de estado manda sobre las menciones de reuniones
      con fecha anterior o igual al dia en que se hizo, y cede ante las
      posteriores, que son informacion nueva.

    Con `excluir_meeting_id` se calcula como si esa reunion no la hubiera
    mencionado: es lo que `acciones_abiertas` necesita al reprocesar.
    Devuelve None si la accion no tiene ninguna mencion.
    """
    fila = conn.execute(
        "SELECT estado, cerrada_en FROM actions WHERE id = ?", (action_id,)
    ).fetchone()
    if fila is None:
        return None
    absorbidas = [
        f["id"]
        for f in conn.execute("SELECT id FROM actions WHERE absorbida_por = ?", (action_id,))
    ]
    ids = [action_id, *absorbidas]
    menciones = conn.execute(
        f"""
        SELECT am.action_id, am.meeting_id, am.estado, m.fecha
          FROM action_mentions am
          JOIN meetings m ON m.id = am.meeting_id
         WHERE am.action_id IN ({_placeholders(ids)})
           AND am.meeting_id IS NOT ?
         ORDER BY m.fecha, m.id, am.action_id != ?
        """,
        (*ids, excluir_meeting_id, action_id),
    ).fetchall()
    if not menciones:
        return None

    estado, cerrada_en, ultima_propia = fila["estado"], fila["cerrada_en"], None
    propias = [m for m in menciones if m["action_id"] == action_id]
    if propias:
        estado, cerrada_en = None, None
        for mencion in propias:
            if mencion["estado"] in ESTADOS_CERRADOS:
                if estado not in ESTADOS_CERRADOS:
                    cerrada_en = mencion["fecha"]
            else:
                cerrada_en = None
            estado = mencion["estado"]
        ultima_propia = propias[-1]["fecha"]

    correccion = _correccion_vigente(conn, action_id, "estado")
    if correccion and (ultima_propia is None or correccion["creado_en"][:10] >= ultima_propia):
        nuevo = correccion["valor_nuevo"]
        if nuevo in ESTADOS_CERRADOS:
            if estado not in ESTADOS_CERRADOS or not cerrada_en:
                cerrada_en = correccion["creado_en"][:10]
        else:
            cerrada_en = None
        estado = nuevo

    return {
        "estado": estado,
        "cerrada_en": cerrada_en,
        "menciones": len({m["meeting_id"] for m in menciones}),
        "meeting_id_ultima": menciones[-1]["meeting_id"],
    }


def _recalcular_accion(conn: sqlite3.Connection, action_id: int) -> None:
    """Rehace la cache de `actions` desde `action_mentions` y las correcciones.

    Si la accion esta absorbida, recalcula tambien la principal, cuya union de
    menciones acaba de cambiar.
    """
    derivado = _derivar_accion(conn, action_id)
    if derivado is not None:
        conn.execute(
            """
            UPDATE actions
               SET estado = ?, cerrada_en = ?, menciones = ?, meeting_id_ultima = ?
             WHERE id = ?
            """,
            (
                derivado["estado"],
                derivado["cerrada_en"],
                derivado["menciones"],
                derivado["meeting_id_ultima"],
                action_id,
            ),
        )
    fila = conn.execute(
        "SELECT absorbida_por FROM actions WHERE id = ?", (action_id,)
    ).fetchone()
    if fila and fila["absorbida_por"]:
        _recalcular_accion(conn, fila["absorbida_por"])


# --------------------------------------------------------------------------
# Reconciliacion al reprocesar (Fase 7, seccion 7.3)
# --------------------------------------------------------------------------

_TABLA_PENDIENTES = "temp.reconciliacion_pendiente"


def _marcar_para_reconciliar(conn: sqlite3.Connection, meeting_id: int) -> None:
    """Apunta las acciones que la pasada anterior dejo en esta reunion.

    Va en una tabla temporal de la conexion, no en la base: es un estado de
    esta pasada, y `insertar_acciones` lo consume. Si nadie lo consume (se
    reprocesa sin volver a insertar acciones) las acciones se quedan como
    estaban, que es lo menos destructivo.
    """
    conn.execute(
        "CREATE TEMP TABLE IF NOT EXISTS reconciliacion_pendiente ("
        " meeting_id INTEGER NOT NULL, action_id INTEGER NOT NULL,"
        " PRIMARY KEY (meeting_id, action_id))"
    )
    conn.execute(f"DELETE FROM {_TABLA_PENDIENTES} WHERE meeting_id = ?", (meeting_id,))
    conn.execute(
        f"INSERT INTO {_TABLA_PENDIENTES} (meeting_id, action_id) "
        "SELECT ?, id FROM actions WHERE meeting_id_origen = ?",
        (meeting_id, meeting_id),
    )


def _acciones_a_reconciliar(
    conn: sqlite3.Connection, meeting_id: int, pendientes: bool
) -> list[dict]:
    """Las acciones nacidas en la reunion, con lo que hace falta para emparejar.

    Con `pendientes` solo las que marco `crear_reunion` en esta conexion (las
    que ya se han reconciliado o insertado en esta pasada no vuelven a entrar).
    """
    filtro = "a.meeting_id_origen = ?"
    if pendientes:
        existe = conn.execute(
            "SELECT 1 FROM temp.sqlite_master WHERE name = 'reconciliacion_pendiente'"
        ).fetchone()
        if not existe:
            return []
        filtro += (
            f" AND a.id IN (SELECT action_id FROM {_TABLA_PENDIENTES}"
            " WHERE meeting_id = a.meeting_id_origen)"
        )
    return [
        dict(fila)
        for fila in conn.execute(
            f"""
            SELECT a.id, a.uid, a.descripcion, a.descripcion_llm, a.estado,
                   a.descartada_en, a.absorbida_por, p.nombre AS persona
              FROM actions a
              LEFT JOIN personas p ON p.id = a.persona_id
             WHERE {filtro}
             ORDER BY a.id
            """,
            (meeting_id,),
        ).fetchall()
    ]


def _huella_texto(texto: str | None) -> str:
    """Texto sin acentos, sin mayusculas y sin puntuacion, para comparar."""
    limpio = sin_acentos(texto or "").lower()
    limpio = re.sub(r"[^a-z0-9 ]+", " ", limpio)
    return " ".join(limpio.split())


def emparejar_acciones(
    existentes: list[dict],
    nuevas: list[dict],
    umbral: float = UMBRAL_RECONCILIACION,
) -> dict:
    """Empareja acciones guardadas con las que acaba de devolver el modelo.

    Pura: no toca la base, para poder probarla y ensenarla en seco. Exclusiva:
    cada fila empareja una vez como mucho, igual que los perfiles de voz.

    1. `descripcion_llm` normalizada identica a la nueva descripcion.
    2. Similitud de `SequenceMatcher` >= `umbral`, de mayor a menor y, a
       igualdad, primero las del mismo responsable.

    Se compara con `descripcion_llm` y no con `descripcion`: una descripcion
    corregida a mano nunca se parecera a lo que vuelva a generar el modelo.

    Devuelve `parejas` (indice existente, indice nuevo, similitud, metodo),
    `sin_pareja` (indices de existentes) y `nuevas` (indices de nuevas).
    """
    from difflib import SequenceMatcher

    huellas_e = [_huella_texto(e.get("descripcion_llm") or e.get("descripcion")) for e in existentes]
    huellas_n = [_huella_texto(n.get("descripcion")) for n in nuevas]
    libres_e = set(range(len(existentes)))
    libres_n = set(range(len(nuevas)))
    parejas = []

    for i in range(len(existentes)):
        for j in sorted(libres_n):
            if huellas_e[i] and huellas_e[i] == huellas_n[j]:
                parejas.append((i, j, 1.0, "identica"))
                libres_e.discard(i)
                libres_n.discard(j)
                break

    candidatas = []
    for i in libres_e:
        for j in libres_n:
            similitud = SequenceMatcher(None, huellas_e[i], huellas_n[j]).ratio()
            if similitud >= umbral:
                misma = sin_acentos(existentes[i].get("persona") or "") == sin_acentos(
                    nuevas[j].get("persona") or ""
                )
                candidatas.append((similitud, misma, i, j))
    candidatas.sort(key=lambda c: (-c[0], not c[1], c[2], c[3]))
    for similitud, _misma, i, j in candidatas:
        if i in libres_e and j in libres_n:
            parejas.append((i, j, round(similitud, 3), "similar"))
            libres_e.discard(i)
            libres_n.discard(j)

    return {
        "parejas": sorted(parejas),
        "sin_pareja": sorted(libres_e),
        "nuevas": sorted(libres_n),
    }


def accion_protegida(conn: sqlite3.Connection, action_id: int) -> bool:
    """Si un humano ha puesto algo en ella que un reproceso no debe borrar.

    Correcciones vigentes, descarte, fusion (en cualquier sentido) o
    dependencias (en cualquier sentido).
    """
    return bool(
        conn.execute(
            """
            SELECT EXISTS (SELECT 1 FROM correcciones
                            WHERE action_id = :id AND deshecha_en IS NULL)
                OR EXISTS (SELECT 1 FROM actions
                            WHERE id = :id
                              AND (descartada_en IS NOT NULL
                                   OR absorbida_por IS NOT NULL))
                OR EXISTS (SELECT 1 FROM actions WHERE absorbida_por = :id)
                OR EXISTS (SELECT 1 FROM action_dependencias
                            WHERE action_id = :id OR depende_de_id = :id)
            """,
            {"id": action_id},
        ).fetchone()[0]
    )


def reconciliar_acciones(
    conn: sqlite3.Connection, meeting_id: int, acciones: list[dict]
) -> dict:
    """Sustituye al borrado en cascada de las acciones al reprocesar.

    - **Con pareja**: se conserva la fila (`id`, `uid`, vinculos). Los campos
      con correccion vigente mantienen el valor humano; el resto toma el del
      modelo. `descripcion_llm` pasa a la nueva redaccion. Descartada o
      absorbida, sigue estandolo.
    - **Sin pareja y protegida**: se conserva con `revisar = 1`.
    - **Sin pareja y sin proteger**: se borra, como antes.
    - Lo que el modelo devuelve sin pareja se inserta como nuevo.

    En una reunion que se procesa por primera vez no hay nada que emparejar y
    todo entra como nuevo.
    """
    limpias = [a for a in (_normalizar_accion(x) for x in acciones) if a]
    existentes = _acciones_a_reconciliar(conn, meeting_id, pendientes=True)
    plan = emparejar_acciones(existentes, limpias)
    resultado = {"conservadas": 0, "revisar": 0, "borradas": 0, "nuevas": 0}

    for i, j, _similitud, _metodo in plan["parejas"]:
        vieja, nueva = existentes[i], limpias[j]
        descripcion = (
            vieja["descripcion"]
            if _correccion_vigente(conn, vieja["id"], "descripcion")
            else nueva["descripcion"]
        )
        persona_id = (
            conn.execute(
                "SELECT persona_id FROM actions WHERE id = ?", (vieja["id"],)
            ).fetchone()[0]
            if _correccion_vigente(conn, vieja["id"], "persona")
            else obtener_o_crear_persona(conn, nueva["persona"])
        )
        conn.execute(
            """
            UPDATE actions
               SET descripcion = ?, descripcion_llm = ?, persona_id = ?, revisar = 0
             WHERE id = ?
            """,
            (descripcion, nueva["descripcion"], persona_id, vieja["id"]),
        )
        _anotar_mencion(conn, vieja["id"], meeting_id, nueva["estado"])
        _recalcular_accion(conn, vieja["id"])
        resultado["conservadas"] += 1

    # Las nuevas antes de borrar nada: `_uid_accion` numera a partir del mayor
    # uid existente, y si la borrada era la ultima su uid se le daria a otra
    # accion distinta, con lo que un enlace guardado cambiaria de destino.
    for j in plan["nuevas"]:
        _insertar_accion(conn, meeting_id, limpias[j])
        resultado["nuevas"] += 1

    for i in plan["sin_pareja"]:
        vieja = existentes[i]
        if accion_protegida(conn, vieja["id"]):
            conn.execute("UPDATE actions SET revisar = 1 WHERE id = ?", (vieja["id"],))
            resultado["revisar"] += 1
        else:
            conn.execute("DELETE FROM actions WHERE id = ?", (vieja["id"],))
            resultado["borradas"] += 1

    if existentes:
        conn.execute(f"DELETE FROM {_TABLA_PENDIENTES} WHERE meeting_id = ?", (meeting_id,))
    return resultado


def simular_reconciliacion(
    conn: sqlite3.Connection,
    meeting_id: int | None,
    acciones: list[dict],
    umbral: float = UMBRAL_RECONCILIACION,
) -> dict:
    """Lo que haria `reconciliar_acciones` al reprocesar, sin escribir nada.

    Alimenta `summarize_teams.py --dry-run-reconciliacion`, que es como se
    calibra `UMBRAL_RECONCILIACION`. Para cada existente sin pareja se da
    ademas su mejor candidata, aunque no llegue al umbral: es justo el dato
    que hace falta para saber si el umbral esta demasiado alto.
    """
    from difflib import SequenceMatcher

    limpias = [a for a in (_normalizar_accion(x) for x in acciones) if a]
    existentes = (
        _acciones_a_reconciliar(conn, meeting_id, pendientes=False)
        if meeting_id is not None
        else []
    )
    plan = emparejar_acciones(existentes, limpias, umbral)
    parejas = [
        {
            "uid": existentes[i]["uid"],
            "guardada": existentes[i]["descripcion_llm"] or existentes[i]["descripcion"],
            "nueva": limpias[j]["descripcion"],
            "similitud": similitud,
            "metodo": metodo,
        }
        for i, j, similitud, metodo in plan["parejas"]
    ]
    sin_pareja = []
    for i in plan["sin_pareja"]:
        vieja = existentes[i]
        huella = _huella_texto(vieja["descripcion_llm"] or vieja["descripcion"])
        mejor = max(
            (
                (SequenceMatcher(None, huella, _huella_texto(n["descripcion"])).ratio(), n)
                for n in limpias
            ),
            key=lambda par: par[0],
            default=(0.0, None),
        )
        sin_pareja.append(
            {
                "uid": vieja["uid"],
                "guardada": vieja["descripcion_llm"] or vieja["descripcion"],
                "protegida": accion_protegida(conn, vieja["id"]),
                "mejor_candidata": mejor[1]["descripcion"] if mejor[1] else None,
                "similitud": round(mejor[0], 3),
            }
        )
    return {
        "umbral": umbral,
        "parejas": parejas,
        "sin_pareja": sin_pareja,
        "nuevas": [limpias[j]["descripcion"] for j in plan["nuevas"]],
    }


def _hoy(conn: sqlite3.Connection) -> str:
    return conn.execute("SELECT date('now')").fetchone()[0]


# --------------------------------------------------------------------------
# Acciones abiertas y arrastres (los explota la Fase 2)
# --------------------------------------------------------------------------


def id_reunion_por_transcripcion(
    conn: sqlite3.Connection, transcript_path: str
) -> int | None:
    """Id de la reunion ya registrada para esa transcripcion, si existe.

    Lo usa `summarize_teams.py` para saber que esta reprocesando y pedir las
    acciones abiertas *como estaban antes* de la pasada anterior.
    """
    fila = conn.execute(
        "SELECT id FROM meetings WHERE transcript_path = ? ORDER BY id DESC LIMIT 1",
        (transcript_path,),
    ).fetchone()
    return fila["id"] if fila else None


def reunion_por_uid(conn: sqlite3.Connection, uid: str) -> sqlite3.Row | None:
    """La reunion con ese `uid`, la identidad estable entre reprocesados.

    Es la consulta que resuelve una URL de la interfaz y una cita del chat.
    """
    return conn.execute("SELECT * FROM meetings WHERE uid = ?", (uid,)).fetchone()


# --------------------------------------------------------------------------
# Consultas de lectura (las explota la interfaz web)
# --------------------------------------------------------------------------


def _filtros_reuniones(
    desde: str | None, hasta: str | None, tipo: str | None, uid: str | None = None
) -> tuple[str, list]:
    condiciones, valores = [], []
    if uid:
        condiciones.append("m.uid = ?")
        valores.append(uid)
    if desde:
        condiciones.append("m.fecha >= ?")
        valores.append(desde)
    if hasta:
        condiciones.append("m.fecha <= ?")
        valores.append(hasta)
    if tipo:
        condiciones.append("m.tipo = ?")
        valores.append(tipo)
    return (" WHERE " + " AND ".join(condiciones) if condiciones else ""), valores


def listar_reuniones(
    conn: sqlite3.Connection,
    *,
    desde: str | None = None,
    hasta: str | None = None,
    tipo: str | None = None,
    uid: str | None = None,
    limite: int = 50,
    desplazamiento: int = 0,
) -> list[sqlite3.Row]:
    """Reuniones de mas reciente a mas antigua, con sus recuentos.

    Los recuentos van en subconsultas y no en JOIN + GROUP BY: con varios
    LEFT JOIN a la vez las filas se multiplican entre si y los totales salen
    inflados.
    """
    where, valores = _filtros_reuniones(desde, hasta, tipo, uid)
    return conn.execute(
        f"""
        SELECT m.id, m.uid, m.fecha, m.titulo, m.tipo, m.duracion_seg,
               m.resumen, m.audio_path, m.transcript_path, m.creado_en,
               (SELECT count(*) FROM segments s WHERE s.meeting_id = m.id)
                   AS n_segmentos,
               (SELECT count(*) FROM acciones_vigentes a WHERE a.meeting_id_origen = m.id)
                   AS n_acciones,
               (SELECT count(*) FROM risks r WHERE r.meeting_id = m.id)
                   AS n_riesgos,
               (SELECT count(*) FROM updates u WHERE u.meeting_id = m.id)
                   AS n_updates
          FROM meetings m
          {where}
         ORDER BY m.fecha DESC, m.id DESC
         LIMIT ? OFFSET ?
        """,
        (*valores, limite, desplazamiento),
    ).fetchall()


def contar_reuniones(
    conn: sqlite3.Connection,
    *,
    desde: str | None = None,
    hasta: str | None = None,
    tipo: str | None = None,
) -> int:
    where, valores = _filtros_reuniones(desde, hasta, tipo)
    return conn.execute(
        f"SELECT count(*) FROM meetings m {where}", valores
    ).fetchone()[0]


# --------------------------------------------------------------------------
# Vista de reunion (fase I2 de la interfaz)
# --------------------------------------------------------------------------

# Todo lo de aqui se lee de las **tablas**, no de `meetings.datos_json`. El
# JSON del LLM es lo que dijo el modelo aquella vez; las tablas son lo que el
# historico da por bueno hoy, y son lo que corregira la fase I6. Si la vista de
# reunion se pintara del JSON, una correccion humana no se veria en la reunion
# donde se corrigio. El JSON solo se usa para lo que no tiene tabla: las
# secciones propias de retro/planning/workshop.


def hablantes_de_reunion(conn: sqlite3.Connection, meeting_id: int) -> list[sqlite3.Row]:
    """Mapeo etiqueta -> persona de esa reunion, con su confianza.

    `persona` puede ser NULL: `obtener_o_crear_persona` no inventa gente para
    un `SPEAKER_01` sin nombre, y esa etiqueta se queda sin resolver.
    """
    return conn.execute(
        """
        SELECT ms.etiqueta, ms.confianza, ms.metodo, p.nombre AS persona
          FROM meeting_speakers ms
          LEFT JOIN personas p ON p.id = ms.persona_id
         WHERE ms.meeting_id = ?
         ORDER BY ms.etiqueta
        """,
        (meeting_id,),
    ).fetchall()


def updates_de_reunion(conn: sqlite3.Connection, meeting_id: int) -> list[sqlite3.Row]:
    """Lo que conto cada persona: la seccion "Por persona" del resumen."""
    return conn.execute(
        """
        SELECT COALESCE(p.nombre, 'no identificado') AS persona,
               u.trabajo, u.bloqueos, u.proximos_pasos
          FROM updates u
          LEFT JOIN personas p ON p.id = u.persona_id
         WHERE u.meeting_id = ?
         ORDER BY u.id
        """,
        (meeting_id,),
    ).fetchall()


def riesgos_de_reunion(conn: sqlite3.Connection, meeting_id: int) -> list[sqlite3.Row]:
    """Riesgos **mencionados** en esa reunion. No tienen estado ni continuidad (D3)."""
    return conn.execute(
        """
        SELECT descripcion, area, severidad
          FROM risks
         WHERE meeting_id = ?
         ORDER BY id
        """,
        (meeting_id,),
    ).fetchall()


# --------------------------------------------------------------------------
# Consultas transversales (Fase 4: `ask_teams.py`)
# --------------------------------------------------------------------------
# Las dos anteriores responden "que paso en esta reunion". Estas dos responden
# "que se ha contado ultimamente", que es lo que necesita una pregunta en
# lenguaje natural, y viven aqui por la misma regla de siempre: ni la API ni
# `ask_teams.py` escriben SQL.


def updates_recientes(
    conn: sqlite3.Connection,
    *,
    persona: str | None = None,
    desde: str | None = None,
    hasta: str | None = None,
    limite: int = 40,
) -> list[sqlite3.Row]:
    """Lo que ha contado la gente, con su reunion, mas reciente primero."""
    where = ["1=1"]
    valores: list = []
    if persona:
        where.append("sin_acentos(p.nombre) LIKE '%' || sin_acentos(?) || '%'")
        valores.append(persona)
    if desde:
        where.append("m.fecha >= ?")
        valores.append(desde)
    if hasta:
        where.append("m.fecha <= ?")
        valores.append(hasta)
    valores.append(limite)
    return conn.execute(
        f"""
        SELECT COALESCE(p.nombre, 'no identificado') AS persona,
               u.trabajo, u.bloqueos, u.proximos_pasos,
               m.uid, m.fecha, m.titulo, m.tipo
          FROM updates u
          LEFT JOIN personas p ON p.id = u.persona_id
          JOIN meetings m ON m.id = u.meeting_id
         WHERE {" AND ".join(where)}
         ORDER BY m.fecha DESC, u.id
         LIMIT ?
        """,
        valores,
    ).fetchall()


def riesgos_recientes(
    conn: sqlite3.Connection,
    *,
    desde: str | None = None,
    hasta: str | None = None,
    limite: int = 40,
) -> list[sqlite3.Row]:
    """Riesgos **mencionados** en el periodo, con su reunion (D3: sin estado)."""
    where = ["1=1"]
    valores: list = []
    if desde:
        where.append("m.fecha >= ?")
        valores.append(desde)
    if hasta:
        where.append("m.fecha <= ?")
        valores.append(hasta)
    valores.append(limite)
    return conn.execute(
        f"""
        SELECT r.descripcion, r.area, r.severidad,
               m.uid, m.fecha, m.titulo, m.tipo
          FROM risks r
          JOIN meetings m ON m.id = r.meeting_id
         WHERE {" AND ".join(where)}
         ORDER BY m.fecha DESC, r.id
         LIMIT ?
        """,
        valores,
    ).fetchall()


# Las dos consultas de acciones devuelven las mismas columnas a proposito: la
# interfaz pinta "nacidas aqui" y "arrastradas" con la misma tarjeta, y solo
# cambia el encabezado.
_SQL_ACCIONES_DE_REUNION = """
SELECT a.id, a.uid, a.descripcion, a.estado, a.menciones, a.cerrada_en,
       a.revisar, p.nombre AS persona,
       mo.uid AS origen_uid, mo.fecha AS origen_fecha,
       COALESCE(mu.uid, mo.uid) AS ultima_uid,
       COALESCE(mu.fecha, mo.fecha) AS ultima_fecha,
       (a.menciones >= ? AND a.estado NOT IN ({cerrados})) AS estancada
  FROM acciones_vigentes a
  LEFT JOIN personas p ON p.id = a.persona_id
  JOIN meetings mo ON mo.id = a.meeting_id_origen
  LEFT JOIN meetings mu ON mu.id = a.meeting_id_ultima
 WHERE {filtro}
 ORDER BY estancada DESC, a.menciones DESC, a.id
"""


def acciones_de_reunion(conn: sqlite3.Connection, meeting_id: int) -> list[sqlite3.Row]:
    """Las acciones que **nacieron** en esa reunion.

    Ojo al leerlas: `estado` y `menciones` son los de **hoy**, no los que
    tenian al acabar la reunion. Una accion creada en la del lunes y cerrada el
    jueves aparece aqui como completada, y eso es lo correcto -- pero la
    interfaz tiene que decirlo, porque el `.md` de aquel dia decia otra cosa.
    """
    sql = _SQL_ACCIONES_DE_REUNION.format(
        cerrados=_placeholders(ESTADOS_CERRADOS), filtro="a.meeting_id_origen = ?"
    )
    return conn.execute(
        sql, (UMBRAL_ESTANCAMIENTO, *ESTADOS_CERRADOS, meeting_id)
    ).fetchall()


def arrastres_de_reunion(conn: sqlite3.Connection, meeting_id: int) -> list[sqlite3.Row]:
    """Acciones **de reuniones anteriores** cuya ultima mencion es esta.

    Es la cara en la vista de reunion de la Fase 2 del motor. La condicion es
    `meeting_id_ultima`, asi que una accion que esta reunion menciono pero otra
    posterior volvio a tocar ya no sale aqui: la base no guarda el historial de
    menciones, solo la ultima (D10). Mientras se mire la reunion mas reciente
    -- que es el caso normal -- coincide con lo que dice su `.md`.
    """
    sql = _SQL_ACCIONES_DE_REUNION.format(
        cerrados=_placeholders(ESTADOS_CERRADOS),
        filtro="a.meeting_id_ultima = ? AND a.meeting_id_origen != ?",
    )
    return conn.execute(
        sql, (UMBRAL_ESTANCAMIENTO, *ESTADOS_CERRADOS, meeting_id, meeting_id)
    ).fetchall()


def contar_segmentos(conn: sqlite3.Connection, meeting_id: int) -> int:
    return conn.execute(
        "SELECT count(*) FROM segments WHERE meeting_id = ?", (meeting_id,)
    ).fetchone()[0]


def segmentos_de_reunion(
    conn: sqlite3.Connection,
    meeting_id: int,
    *,
    limite: int = 500,
    desplazamiento: int = 0,
) -> list[sqlite3.Row]:
    """La transcripcion, en orden y por paginas.

    Se pagina siempre: una hora de reunion son ~350 segmentos y una de tres,
    mil largos. `inicio`/`fin` pueden ser NULL si la reunion se proceso sin
    `.srt` (D9); el `persona_id` viene del mapeo de hablantes, y la `etiqueta`
    cruda se conserva para poder mostrar `SPEAKER_01` cuando nadie le puso
    nombre.
    """
    return conn.execute(
        """
        SELECT s.idx, s.inicio, s.fin, s.etiqueta, p.nombre AS persona, s.texto
          FROM segments s
          LEFT JOIN personas p ON p.id = s.persona_id
         WHERE s.meeting_id = ?
         ORDER BY s.idx
         LIMIT ? OFFSET ?
        """,
        (meeting_id, limite, desplazamiento),
    ).fetchall()


# --------------------------------------------------------------------------
# Tablero de acciones (fase I3 de la interfaz)
# --------------------------------------------------------------------------

# La misma proyeccion que `_SQL_ACCIONES_DE_REUNION`, mas el titulo de las dos
# reuniones y los dias sin tocar. El tablero no se mira desde una reunion, asi
# que una fecha suelta no dice a que reunion apunta el enlace, y la antiguedad
# es aqui una columna de primera clase: la pregunta que responde esta vista es
# "que lleva demasiado tiempo sin moverse".
_SQL_ACCIONES = """
SELECT a.id, a.uid, a.descripcion, a.estado, a.menciones, a.cerrada_en,
       a.revisar, p.nombre AS persona,
       mo.uid AS origen_uid, mo.fecha AS origen_fecha, mo.titulo AS origen_titulo,
       COALESCE(mu.uid, mo.uid) AS ultima_uid,
       COALESCE(mu.fecha, mo.fecha) AS ultima_fecha,
       COALESCE(mu.titulo, mo.titulo) AS ultima_titulo,
       CAST(julianday(?) - julianday(COALESCE(mu.fecha, mo.fecha)) AS INTEGER)
           AS dias_sin_tocar,
       (a.menciones >= ? AND a.estado NOT IN ({cerrados})) AS estancada
{desde_join}
{where}
 ORDER BY {orden}
 LIMIT ? OFFSET ?
"""

# El FROM va aparte porque los recuentos por estado y por persona lo comparten
# con la consulta principal: son la misma poblacion contada de otra manera, y
# duplicarlo seria la forma mas facil de que un dia dejaran de coincidir.
_DESDE_ACCIONES = """
  FROM acciones_vigentes a
  LEFT JOIN personas p ON p.id = a.persona_id
  JOIN meetings mo ON mo.id = a.meeting_id_origen
  LEFT JOIN meetings mu ON mu.id = a.meeting_id_ultima
"""

# El orden por defecto es el del tablero: lo estancado arriba y, dentro de eso,
# lo mas repetido. Los otros existen porque "que lleva mas tiempo abierto" y
# "que se movio ayer" son preguntas distintas, y ninguna se puede responder
# reordenando en el navegador: la lista viene paginada del servidor.
ORDENES_ACCIONES = {
    "prioridad": "estancada DESC, a.menciones DESC, ultima_fecha DESC, a.id",
    "antiguedad": "mo.fecha, a.id",
    "reciente": "ultima_fecha DESC, a.id DESC",
    "persona": "persona IS NULL, persona COLLATE NOCASE, estancada DESC, a.id",
}

# "Sin responsable" no es una persona que se llame asi: `obtener_o_crear_persona`
# devuelve None para `SPEAKER_01` o "no identificado", y esas acciones se quedan
# con `persona_id` NULL. Filtrarlas es una de las lecturas utiles del tablero
# ("esto no lo ha cogido nadie") y necesita un valor propio, porque `persona`
# vacio ya significa "no filtres por persona".
SIN_RESPONSABLE = "__sin_responsable__"


def _filtros_acciones(
    *,
    hoy: str,
    estados=None,
    persona: str | None = None,
    texto: str | None = None,
    estancadas: bool | None = None,
    desde: str | None = None,
    hasta: str | None = None,
    dias_sin_tocar: int | None = None,
    omitir: tuple = (),
) -> tuple[str, list]:
    """Condiciones del tablero. `omitir` deja fuera un filtro concreto.

    Los recuentos por estado se calculan **sin** el filtro de estado, y los de
    persona **sin** el de persona: de otro modo el chip ya pulsado se quedaria
    con su propio numero y los demas a cero, y no habria forma de ver a donde
    lleva cambiarlo.
    """
    condiciones, valores = [], []
    if estados and "estados" not in omitir:
        condiciones.append(f"a.estado IN ({_placeholders(estados)})")
        valores.extend(estados)
    if persona and "persona" not in omitir:
        if persona == SIN_RESPONSABLE:
            condiciones.append("a.persona_id IS NULL")
        else:
            condiciones.append("p.nombre = ? COLLATE NOCASE")
            valores.append(persona)
    if texto:
        # LIKE con los acentos ya fuera por los dos lados, igual que hara la
        # busqueda de I4 sobre los segmentos. Los comodines se escapan: quien
        # teclea "100 %" busca ese texto, no "lo que sea".
        patron = sin_acentos(texto).replace("\\", "\\\\")
        patron = patron.replace("%", "\\%").replace("_", "\\_")
        condiciones.append("sin_acentos(a.descripcion) LIKE ? ESCAPE '\\'")
        valores.append(f"%{patron}%")
    if estancadas is not None:
        # La misma definicion que la columna `estancada` del SELECT, negada
        # cuando se piden las que **no** lo estan. Se repite aqui porque un
        # alias del SELECT no se puede usar en el WHERE de forma portable.
        if estancadas:
            condiciones.append(
                f"(a.menciones >= ? AND a.estado NOT IN "
                f"({_placeholders(ESTADOS_CERRADOS)}))"
            )
        else:
            condiciones.append(
                f"(a.menciones < ? OR a.estado IN "
                f"({_placeholders(ESTADOS_CERRADOS)}))"
            )
        valores.append(UMBRAL_ESTANCAMIENTO)
        valores.extend(ESTADOS_CERRADOS)
    # Mismo criterio de periodo que los carriles del timeline: entra lo que
    # **solapa** el rango, no solo lo nacido dentro. Una accion de hace dos
    # meses que sigue viva es justo la que hay que ver al mirar esta semana.
    if desde:
        condiciones.append("COALESCE(mu.fecha, mo.fecha) >= ?")
        valores.append(desde)
    if hasta:
        condiciones.append("mo.fecha <= ?")
        valores.append(hasta)
    if dias_sin_tocar:
        condiciones.append(
            "julianday(?) - julianday(COALESCE(mu.fecha, mo.fecha)) >= ?"
        )
        valores.extend([hoy, dias_sin_tocar])
    return (" WHERE " + " AND ".join(condiciones) if condiciones else ""), valores


def listar_acciones(
    conn: sqlite3.Connection,
    *,
    orden: str = "prioridad",
    limite: int = 100,
    desplazamiento: int = 0,
    **filtros,
) -> list[sqlite3.Row]:
    """Las acciones del historico entero, filtradas, ordenadas y paginadas.

    Es la consulta del tablero (seccion 3.4 del plan). A diferencia de
    `acciones_abiertas`, que le sirve al LLM las de las ultimas N reuniones,
    esta mira todo el historico y no interpreta nada: filtra, ordena y cuenta.
    """
    hoy = _hoy(conn)
    where, valores = _filtros_acciones(hoy=hoy, **filtros)
    sql = _SQL_ACCIONES.format(
        cerrados=_placeholders(ESTADOS_CERRADOS),
        desde_join=_DESDE_ACCIONES.rstrip("\n"),
        where=where,
        orden=ORDENES_ACCIONES.get(orden) or ORDENES_ACCIONES["prioridad"],
    )
    return conn.execute(
        sql,
        (
            hoy,
            UMBRAL_ESTANCAMIENTO,
            *ESTADOS_CERRADOS,
            *valores,
            limite,
            desplazamiento,
        ),
    ).fetchall()


def contar_acciones(conn: sqlite3.Connection, **filtros) -> int:
    """Las que cumplen el filtro, no las que caben en la pagina."""
    where, valores = _filtros_acciones(hoy=_hoy(conn), **filtros)
    return conn.execute(
        f"SELECT count(*) {_DESDE_ACCIONES} {where}", valores
    ).fetchone()[0]


def acciones_por_estado(conn: sqlite3.Connection, **filtros) -> dict[str, int]:
    """Cuantas hay de cada estado con estos filtros, menos el de estado.

    Devuelve **todos** los estados, incluidos los que no tienen ninguna: un
    cero explicito es informacion ("no hay nada bloqueado") y un hueco no.
    """
    where, valores = _filtros_acciones(hoy=_hoy(conn), omitir=("estados",), **filtros)
    filas = conn.execute(
        f"SELECT a.estado, count(*) AS n {_DESDE_ACCIONES} {where} GROUP BY a.estado",
        valores,
    ).fetchall()
    recuento = {estado: 0 for estado in ESTADOS_ACCION}
    for fila in filas:
        # Un estado fuera de ESTADOS_ACCION no deberia existir (`normalizar()`
        # los valida antes de insertar), pero si una base vieja lo tiene, mejor
        # verlo que perderlo del recuento.
        recuento[fila["estado"]] = recuento.get(fila["estado"], 0) + fila["n"]
    return recuento


def responsables_de_acciones(conn: sqlite3.Connection, **filtros) -> list[sqlite3.Row]:
    """Personas con acciones bajo los filtros actuales, menos el de persona.

    Alimenta el desplegable del tablero. No es `/api/personas` (fase I6, con
    alias y altas): aqui solo hacen falta los nombres que tienen algo que
    ensenar, y ofrecer a alguien sin ninguna accion solo lleva a una lista
    vacia.
    """
    where, valores = _filtros_acciones(hoy=_hoy(conn), omitir=("persona",), **filtros)
    return conn.execute(
        f"""
        SELECT COALESCE(p.nombre, ?) AS persona, count(*) AS n
        {_DESDE_ACCIONES} {where}
         GROUP BY p.nombre
         ORDER BY n DESC, persona COLLATE NOCASE
        """,
        (SIN_RESPONSABLE, *valores),
    ).fetchall()


# --------------------------------------------------------------------------
# Busqueda de texto completo (fase I4 de la interfaz)
# --------------------------------------------------------------------------

# Marcadores del resaltado. Se devuelven **dentro del texto** y no como
# posiciones porque `snippet()` ya recorta y marca en una sola pasada, y
# calcular los desplazamientos por nuestra cuenta obligaria a repetir la
# tokenizacion de FTS5 en Python (y a equivocarse en cuanto un termino lleve
# acento). Son dos caracteres de control que no aparecen en una transcripcion:
# quien pinte el resultado escapa el HTML **primero** y los sustituye despues,
# asi que un `<script>` dicho en voz alta sigue siendo texto.
MARCA_INICIO = "\x02"
MARCA_FIN = "\x03"

# Palabras de contexto alrededor de la coincidencia. Un segmento de Whisper son
# una o dos frases, asi que en la practica casi siempre cabe entero; el recorte
# solo actua en los segmentos largos de una reunion procesada sin diarizar.
PALABRAS_DE_CONTEXTO = 32

# Como en el tablero: la clausula no puede viajar como parametro, asi que se
# interpola desde esta lista blanca y un valor desconocido cae en el primero.
# "relevancia" es bm25 (mas negativo = mejor coincidencia, de ahi el ASC).
ORDENES_BUSQUEDA = {
    "relevancia": "bm25(segments_fts), m.fecha DESC, s.idx",
    "reciente": "m.fecha DESC, s.idx",
    "antiguo": "m.fecha, s.idx",
}

_PALABRAS = re.compile(r"\w+", re.UNICODE)
_TROZOS = re.compile(r'"([^"]*)"|(\S+)')


def consulta_fts(texto: str | None) -> str | None:
    """Texto tecleado -> expresion MATCH de FTS5, o None si no queda nada.

    Lo que se teclea en una caja de busqueda no es sintaxis de FTS5: un
    guion, un parentesis o una comilla suelta hacen que `MATCH` lance
    `fts5: syntax error near ...`, y ese error no lo ha cometido quien busca.
    Aqui cada palabra se extrae y se **cita**, de modo que cualquier cosa rara
    se busca literalmente en vez de romper la consulta o, peor, colarse como
    operador.

    Se conservan dos formas deliberadamente, porque son las unicas que la
    gente escribe de verdad:

    - `"pantalla de resultados"` entre comillas busca la frase seguida;
    - `refact*` busca por prefijo.

    Todo lo demas (AND, OR, NEAR, parentesis) se trata como texto: los
    terminos ya se combinan con Y implicita, que es lo que se espera.
    """
    if not texto:
        return None
    partes = []
    for frase, suelto in _TROZOS.findall(texto):
        crudo = frase if frase else suelto
        palabras = _PALABRAS.findall(crudo)
        if not palabras:
            continue
        termino = '"' + " ".join(palabras) + '"'
        # El prefijo solo tiene sentido en una palabra suelta: dentro de una
        # frase entrecomillada el asterisco es parte de lo que se busca.
        if not frase and suelto.endswith("*"):
            termino += "*"
        partes.append(termino)
    return " ".join(partes) or None


_DESDE_BUSQUEDA = """
  FROM segments_fts
  JOIN segments s ON s.id = segments_fts.rowid
  JOIN meetings m ON m.id = s.meeting_id
  LEFT JOIN personas p ON p.id = s.persona_id
"""


def _filtros_busqueda(
    *,
    desde: str | None = None,
    hasta: str | None = None,
    tipo: str | None = None,
    uid: str | None = None,
    omitir: tuple = (),
) -> tuple[str, list]:
    condiciones, valores = ["segments_fts MATCH ?"], []
    if desde:
        condiciones.append("m.fecha >= ?")
        valores.append(desde)
    if hasta:
        condiciones.append("m.fecha <= ?")
        valores.append(hasta)
    if tipo:
        condiciones.append("m.tipo = ?")
        valores.append(tipo)
    if uid and "uid" not in omitir:
        condiciones.append("m.uid = ?")
        valores.append(uid)
    return " WHERE " + " AND ".join(condiciones), valores


def buscar_segmentos(
    conn: sqlite3.Connection,
    consulta: str,
    *,
    orden: str = "relevancia",
    limite: int = 50,
    desplazamiento: int = 0,
    **filtros,
) -> list[sqlite3.Row]:
    """Los segmentos que casan con `consulta`, con su reunion y su resaltado.

    `consulta` es la expresion que devuelve `consulta_fts`, no lo que tecleo
    el usuario: quien llame se encarga de convertirla, para poder distinguir
    "no hay resultados" de "no habia nada buscable".

    Los acentos son indiferentes en los dos sentidos -- buscar "sesion"
    encuentra "sesión" y al reves -- porque la consulta pasa por el mismo
    tokenizador `unicode61 remove_diacritics 2` con el que se indexo. Lo que
    **no** hay es stemming: "reunion" no encuentra "reuniones" (para eso esta
    el prefijo `reunion*`).
    """
    where, valores = _filtros_busqueda(**filtros)
    sql = f"""
        SELECT m.uid, m.fecha, m.titulo, m.tipo,
               s.idx, s.inicio, s.fin, s.etiqueta, p.nombre AS persona,
               snippet(segments_fts, 0, ?, ?, '…', ?) AS fragmento
        {_DESDE_BUSQUEDA} {where}
         ORDER BY {ORDENES_BUSQUEDA.get(orden) or next(iter(ORDENES_BUSQUEDA.values()))}
         LIMIT ? OFFSET ?
    """
    return conn.execute(
        sql,
        (
            MARCA_INICIO,
            MARCA_FIN,
            PALABRAS_DE_CONTEXTO,
            consulta,
            *valores,
            limite,
            desplazamiento,
        ),
    ).fetchall()


def contar_busqueda(conn: sqlite3.Connection, consulta: str, **filtros) -> int:
    """Coincidencias totales, no las de la pagina."""
    where, valores = _filtros_busqueda(**filtros)
    return conn.execute(
        f"SELECT count(*) {_DESDE_BUSQUEDA} {where}", (consulta, *valores)
    ).fetchone()[0]


def reuniones_de_busqueda(
    conn: sqlite3.Connection, consulta: str, limite: int = 50, **filtros
) -> list[sqlite3.Row]:
    """En que reuniones aparece el termino y cuantas veces en cada una.

    Es el equivalente de `acciones_por_estado` en el tablero: el recuento que
    alimenta un filtro se calcula **sin** ese filtro (por eso `omitir=("uid",)`),
    para que acotar a una reunion no borre la lista desde la que se acota.
    """
    where, valores = _filtros_busqueda(omitir=("uid",), **filtros)
    return conn.execute(
        f"""
        SELECT m.uid, m.fecha, m.titulo, m.tipo, count(*) AS n
        {_DESDE_BUSQUEDA} {where}
         GROUP BY m.id
         ORDER BY m.fecha DESC
         LIMIT ?
        """,
        (consulta, *valores, limite),
    ).fetchall()


def resumen_bd(conn: sqlite3.Connection) -> dict:
    """Cifras generales de la base. Alimenta el endpoint de salud."""
    def cuantos(tabla: str) -> int:
        return conn.execute(f"SELECT count(*) FROM {tabla}").fetchone()[0]

    fechas = conn.execute(
        "SELECT min(fecha) AS primera, max(fecha) AS ultima FROM meetings"
    ).fetchone()
    return {
        "esquema": conn.execute("PRAGMA user_version").fetchone()[0],
        "journal_mode": conn.execute("PRAGMA journal_mode").fetchone()[0],
        "reuniones": cuantos("meetings"),
        "personas": cuantos("personas"),
        "acciones": cuantos("acciones_vigentes"),
        "acciones_abiertas": conn.execute(
            "SELECT count(*) FROM acciones_vigentes WHERE estado NOT IN ('completada', 'abandonada')"
        ).fetchone()[0],
        "riesgos": cuantos("risks"),
        "segmentos": cuantos("segments"),
        "primera_reunion": fechas["primera"],
        "ultima_reunion": fechas["ultima"],
    }


# --------------------------------------------------------------------------
# Metricas y carriles de accion (los explota el timeline de la interfaz)
# --------------------------------------------------------------------------

# Las cifras se agrupan por semana en Python y no en SQL a proposito:
# `strftime('%G-W%V')` (semana ISO) solo existe en SQLite 3.46 y posteriores, y
# la version del servidor de despliegue no esta fijada. `date.isocalendar()` de
# la stdlib da lo mismo en cualquier sitio.


def _placeholders(valores) -> str:
    return ", ".join("?" for _ in valores)


def _y(where: str) -> str:
    """Convierte el `WHERE ...` de `_filtros_reuniones` en un `AND ...`.

    Las subconsultas de `metricas` ya traen su propia condicion sobre la
    persona, asi que el filtro de fechas tiene que encadenarse, no abrir un
    WHERE nuevo. Devuelve cadena vacia si no habia filtro.
    """
    return where.replace(" WHERE ", " AND ", 1) if where else ""


def _semana_iso(fecha: str) -> str | None:
    """`2026-09-07` -> `2026-W37`. Devuelve None si la fecha no es una fecha."""
    try:
        anio, semana, _ = date.fromisoformat(fecha).isocalendar()
    except (TypeError, ValueError):
        return None
    return f"{anio}-W{semana:02d}"


def _agrupar_riesgos(filas: list[sqlite3.Row]) -> dict:
    """Riesgos **mencionados** en el periodo, que es lo unico que el dato dice.

    La tabla `risks` no tiene estado ni continuidad entre reuniones (D3): un
    riesgo que nadie volvio a nombrar desaparece del recuento. Por eso ni esta
    funcion ni la interfaz hablan nunca de riesgos "abiertos".
    """
    por_severidad: dict[str, int] = {}
    por_area: dict[str, int] = {}
    total = 0
    for fila in filas:
        total += fila["n"]
        severidad = (fila["severidad"] or "").strip().lower() or "sin severidad"
        por_severidad[severidad] = por_severidad.get(severidad, 0) + fila["n"]
        area = (fila["area"] or "").strip() or "sin área"
        por_area[area] = por_area.get(area, 0) + fila["n"]
    return {
        "total": total,
        "por_severidad": por_severidad,
        "por_area": [
            {"area": area, "n": n}
            for area, n in sorted(por_area.items(), key=lambda par: (-par[1], par[0]))
        ],
    }


def metricas(
    conn: sqlite3.Connection,
    *,
    desde: str | None = None,
    hasta: str | None = None,
) -> dict:
    """Agregados deterministas para el panel de salud del equipo.

    Salen de SQL, nunca del LLM: son numeros comprobables, no una lectura del
    modelo sobre lo que dijo la gente.

    **Dos bloques con dos alcances distintos**, y hay que respetarlos al
    presentarlos. `acciones` y `estancadas` describen el estado **de hoy** del
    historico completo: una accion no tiene fecha propia, asi que recortarla al
    periodo daria un "abiertas: 3" que no significaria nada. Todo lo demas
    (`reuniones`, `semanas`, `personas`, `riesgos`) es lo ocurrido **en el
    periodo** filtrado.

    Lo que no esta, falta por falta de dato y no por olvido: tiempo medio de
    cierre (D4: `cerrada_en` es la fecha de la reunion que la cerro, no la
    del inicio del trabajo), personas sin actualizar
    (D7: no hay roster) y bloqueos recurrentes (D8: texto libre).
    """
    where, valores = _filtros_reuniones(desde, hasta, None)

    por_estado = {estado: 0 for estado in ESTADOS_ACCION}
    for fila in conn.execute(
        "SELECT estado, count(*) AS n FROM acciones_vigentes GROUP BY estado"
    ):
        # Un estado inesperado (escrito por una version anterior) se cuenta
        # igualmente: es preferible a que los totales no cuadren.
        por_estado[fila["estado"]] = por_estado.get(fila["estado"], 0) + fila["n"]

    estancadas = conn.execute(
        f"""
        SELECT count(*) FROM acciones_vigentes
         WHERE menciones >= ?
           AND estado NOT IN ({_placeholders(ESTADOS_CERRADOS)})
        """,
        (UMBRAL_ESTANCAMIENTO, *ESTADOS_CERRADOS),
    ).fetchone()[0]

    reuniones = conn.execute(
        f"SELECT m.fecha, m.tipo, m.duracion_seg FROM meetings m {where}"
        " ORDER BY m.fecha",
        valores,
    ).fetchall()

    # Desde el esquema 3, `cerrada_en` es la fecha de la reunion donde se dio
    # por cerrada (D4), comparable con el mismo rango que las reuniones. La
    # interfaz sigue advirtiendo que no es el dia exacto del cierre.
    cierres = conn.execute(
        "SELECT cerrada_en FROM acciones_vigentes WHERE cerrada_en IS NOT NULL"
        + (" AND cerrada_en >= ?" if desde else "")
        + (" AND cerrada_en <= ?" if hasta else ""),
        [f for f in (desde, hasta) if f],
    ).fetchall()

    semanas: dict[str, dict] = {}

    def semana(clave: str) -> dict:
        return semanas.setdefault(
            clave,
            {
                "semana": clave,
                "reuniones": 0,
                "minutos": 0.0,
                "sin_duracion": 0,
                "cierres": 0,
            },
        )

    minutos_totales = 0.0
    sin_duracion = 0
    por_tipo = {tipo: 0 for tipo in TIPOS_REUNION}
    for fila in reuniones:
        por_tipo[fila["tipo"]] = por_tipo.get(fila["tipo"], 0) + 1
        clave = _semana_iso(fila["fecha"])
        casilla = semana(clave) if clave else None
        if casilla:
            casilla["reuniones"] += 1
        # D9: sin `.srt` la duracion es NULL. Se cuentan aparte para poder
        # decir "90 min en 3 de 5 reuniones" en vez de mentir con la suma.
        if fila["duracion_seg"] is None:
            sin_duracion += 1
            if casilla:
                casilla["sin_duracion"] += 1
        else:
            minutos_totales += fila["duracion_seg"] / 60
            if casilla:
                casilla["minutos"] += fila["duracion_seg"] / 60

    for fila in cierres:
        clave = _semana_iso(fila["cerrada_en"])
        if clave:
            semana(clave)["cierres"] += 1

    for casilla in semanas.values():
        casilla["minutos"] = round(casilla["minutos"])

    personas = [
        dict(fila)
        for fila in conn.execute(
            f"""
            SELECT p.nombre AS persona,
                   (SELECT count(*) FROM updates u
                      JOIN meetings m ON m.id = u.meeting_id
                     WHERE u.persona_id = p.id {_y(where)}) AS updates,
                   (SELECT count(*) FROM acciones_vigentes a
                      JOIN meetings m ON m.id = a.meeting_id_origen
                     WHERE a.persona_id = p.id {_y(where)}) AS acciones,
                   (SELECT count(*) FROM acciones_vigentes a
                     WHERE a.persona_id = p.id
                       AND a.estado NOT IN
                           ({_placeholders(ESTADOS_CERRADOS)})) AS abiertas
              FROM personas p
             ORDER BY p.nombre
            """,
            (*valores, *valores, *ESTADOS_CERRADOS),
        ).fetchall()
    ]
    # Quien no aparece en el periodo y no arrastra nada abierto no se muestra:
    # sin roster (D7) no se puede distinguir "no hablo" de "ya no esta".
    personas = [p for p in personas if p["updates"] or p["acciones"] or p["abiertas"]]
    personas.sort(key=lambda p: (-p["abiertas"], -p["acciones"], p["persona"]))

    riesgos = conn.execute(
        f"""
        SELECT r.severidad, r.area, count(*) AS n
          FROM risks r JOIN meetings m ON m.id = r.meeting_id
          {where}
         GROUP BY r.severidad, r.area
        """,
        valores,
    ).fetchall()

    return {
        "desde": desde,
        "hasta": hasta,
        "umbral_estancamiento": UMBRAL_ESTANCAMIENTO,
        "acciones": por_estado,
        "acciones_abiertas": sum(
            n for estado, n in por_estado.items() if estado not in ESTADOS_CERRADOS
        ),
        "estancadas": estancadas,
        "reuniones": len(reuniones),
        "reuniones_por_tipo": por_tipo,
        "minutos": round(minutos_totales),
        "reuniones_sin_duracion": sin_duracion,
        "semanas": sorted(semanas.values(), key=lambda s: s["semana"]),
        "personas": personas,
        "riesgos": _agrupar_riesgos(riesgos),
    }


def carriles_acciones(
    conn: sqlite3.Connection,
    *,
    desde: str | None = None,
    hasta: str | None = None,
    solo_abiertas: bool = False,
    limite: int = 200,
) -> list[sqlite3.Row]:
    """Cada accion como un tramo entre la reunion donde nacio y la ultima que la menciono.

    Es la lectura visual de la Fase 2 del motor: "esto lleva cinco dailys
    abierto" se ve sin leer nada.

    **Las menciones intermedias no se pueden dibujar** (D10): la base guarda un
    contador `menciones` y la ultima reunion, no la lista de cuales la tocaron.
    El tramo va de origen a ultima y las menciones se muestran como cifra;
    repartir marcas por el medio seria dibujar un dato que nadie ha guardado.

    Se devuelven las acciones cuyo tramo **solapa** el periodo, no solo las
    nacidas dentro: una accion de hace dos meses que sigue abierta es justo la
    que hay que ver al mirar esta semana.
    """
    condiciones, valores = [], []
    if desde:
        condiciones.append("COALESCE(mu.fecha, mo.fecha) >= ?")
        valores.append(desde)
    if hasta:
        condiciones.append("mo.fecha <= ?")
        valores.append(hasta)
    if solo_abiertas:
        condiciones.append(f"a.estado NOT IN ({_placeholders(ESTADOS_CERRADOS)})")
        valores.extend(ESTADOS_CERRADOS)
    where = " WHERE " + " AND ".join(condiciones) if condiciones else ""
    return conn.execute(
        f"""
        SELECT a.id, a.uid, a.descripcion, a.estado, a.menciones,
               p.nombre AS persona,
               mo.uid AS origen_uid, mo.fecha AS origen_fecha,
               COALESCE(mu.uid, mo.uid) AS ultima_uid,
               COALESCE(mu.fecha, mo.fecha) AS ultima_fecha,
               (a.menciones >= ? AND a.estado NOT IN
                    ({_placeholders(ESTADOS_CERRADOS)})) AS estancada
          FROM acciones_vigentes a
          LEFT JOIN personas p ON p.id = a.persona_id
          JOIN meetings mo ON mo.id = a.meeting_id_origen
          LEFT JOIN meetings mu ON mu.id = a.meeting_id_ultima
          {where}
         ORDER BY estancada DESC, a.menciones DESC, mo.fecha, a.id
         LIMIT ?
        """,
        (UMBRAL_ESTANCAMIENTO, *ESTADOS_CERRADOS, *valores, limite),
    ).fetchall()


# --------------------------------------------------------------------------
# Informes agregados (Fase 5 del motor: report_teams.py)
# --------------------------------------------------------------------------

# Las cuatro consultas de abajo existen porque el informe pregunta cosas que
# ni el tablero ni las metricas responden: que se **cerro** en el periodo, que
# bloqueo se repite, quien lleva sin aparecer y que accion lleva reuniones sin
# mencionarse. Viven aqui, como todo el SQL, y no en `report_teams.py`.


def acciones_cerradas(
    conn: sqlite3.Connection,
    *,
    desde: str | None = None,
    hasta: str | None = None,
    persona: str | None = None,
    limite: int = 100,
) -> list[sqlite3.Row]:
    """Acciones que se dieron por cerradas dentro del periodo.

    `cerrada_en` es la fecha de la **reunion** donde se dio por cerrada, o el
    dia de la correccion manual (D4, resuelta en el esquema 3: hasta la v2 era
    la fecha de proceso). Quien la muestre dice que no es el dia exacto en que
    se termino.
    """
    condiciones = [
        f"a.estado IN ({_placeholders(ESTADOS_CERRADOS)})",
        "a.cerrada_en IS NOT NULL",
    ]
    valores: list = list(ESTADOS_CERRADOS)
    if desde:
        condiciones.append("a.cerrada_en >= ?")
        valores.append(desde)
    if hasta:
        condiciones.append("a.cerrada_en <= ?")
        valores.append(hasta)
    if persona:
        if persona == SIN_RESPONSABLE:
            condiciones.append("a.persona_id IS NULL")
        else:
            condiciones.append("p.nombre = ? COLLATE NOCASE")
            valores.append(persona)
    valores.append(limite)
    return conn.execute(
        f"""
        SELECT a.id, a.uid, a.descripcion, a.estado, a.menciones, a.cerrada_en,
               p.nombre AS persona,
               mo.uid AS origen_uid, mo.fecha AS origen_fecha,
               mo.titulo AS origen_titulo,
               COALESCE(mu.uid, mo.uid) AS ultima_uid,
               COALESCE(mu.fecha, mo.fecha) AS ultima_fecha
        {_DESDE_ACCIONES}
         WHERE {" AND ".join(condiciones)}
         ORDER BY a.cerrada_en DESC, a.id
         LIMIT ?
        """,
        valores,
    ).fetchall()


def _huella_bloqueo(texto: str) -> str:
    """Clave de agrupacion de un bloqueo: sin acentos, sin puntuacion, plano."""
    return _huella_texto(texto)


def bloqueos_recurrentes(
    conn: sqlite3.Connection,
    *,
    desde: str | None = None,
    hasta: str | None = None,
    persona: str | None = None,
    minimo: int = 2,
    limite: int = 20,
) -> list[dict]:
    """Bloqueos que aparecen en varias reuniones, agrupados por texto.

    D8 dice que el dato no existe: `updates.bloqueos` es texto libre y nadie
    le ha dado identidad. Lo que se hace aqui es lo unico defendible sin
    inventar nada: **normalizar el texto** (acentos, mayusculas y puntuacion
    fuera) y contar en cuantas reuniones distintas aparece esa misma frase. Un
    bloqueo contado dos veces con otras palabras no se detecta, y el informe
    lo advierte en vez de presentarlo como una deteccion de temas.

    La agrupacion va en Python, no en SQL, por lo mismo que las semanas: se
    apoya en `sin_acentos`, que es una funcion nuestra, y aqui hay que
    quedarse ademas con la redaccion mas reciente de cada grupo.
    """
    condiciones = ["u.bloqueos IS NOT NULL", "trim(u.bloqueos) != ''"]
    valores: list = []
    if desde:
        condiciones.append("m.fecha >= ?")
        valores.append(desde)
    if hasta:
        condiciones.append("m.fecha <= ?")
        valores.append(hasta)
    if persona:
        condiciones.append("sin_acentos(p.nombre) LIKE '%' || sin_acentos(?) || '%'")
        valores.append(persona)
    filas = conn.execute(
        f"""
        SELECT u.bloqueos, u.meeting_id,
               COALESCE(p.nombre, 'no identificado') AS persona,
               m.uid, m.fecha, m.titulo
          FROM updates u
          LEFT JOIN personas p ON p.id = u.persona_id
          JOIN meetings m ON m.id = u.meeting_id
         WHERE {" AND ".join(condiciones)}
         ORDER BY m.fecha, u.id
        """,
        valores,
    ).fetchall()

    grupos: dict[str, dict] = {}
    for fila in filas:
        huella = _huella_bloqueo(fila["bloqueos"])
        if not huella:
            continue
        grupo = grupos.setdefault(
            huella,
            {
                "texto": fila["bloqueos"].strip(),
                "reuniones": [],
                "personas": [],
                "primera_fecha": fila["fecha"],
                "ultima_fecha": fila["fecha"],
            },
        )
        # La redaccion que se ensena es la ultima, que es la que la gente
        # reconoce: las filas vienen ordenadas por fecha ascendente.
        grupo["texto"] = fila["bloqueos"].strip()
        grupo["ultima_fecha"] = fila["fecha"]
        if fila["meeting_id"] not in [r["meeting_id"] for r in grupo["reuniones"]]:
            grupo["reuniones"].append(
                {
                    "meeting_id": fila["meeting_id"],
                    "uid": fila["uid"],
                    "fecha": fila["fecha"],
                    "titulo": fila["titulo"],
                }
            )
        if fila["persona"] not in grupo["personas"]:
            grupo["personas"].append(fila["persona"])

    recurrentes = [
        {**grupo, "n_reuniones": len(grupo["reuniones"])}
        for grupo in grupos.values()
        if len(grupo["reuniones"]) >= minimo
    ]
    recurrentes.sort(key=lambda g: (-g["n_reuniones"], g["texto"].lower()))
    return recurrentes[:limite]


def personas_sin_actualizar(
    conn: sqlite3.Connection,
    *,
    hasta: str | None = None,
    dias: int | None = None,
    limite: int = 50,
) -> list[dict]:
    """Cuando dio cada persona su ultima actualizacion, y que arrastra abierto.

    **No es un control de asistencia** (D7): sin roster no se puede distinguir
    "no ha hablado" de "ya no esta en el equipo", ni saber quien falta y no
    aparece en ninguna tabla. Solo se ven las personas que la base conoce, y
    el informe lo dice.

    `hasta` recorta el "hoy" contra el que se cuentan los dias, para que un
    informe de un periodo pasado no diga que todo el mundo lleva meses sin
    hablar. `dias` filtra a partir de cuantos se considera reportable.
    """
    referencia = hasta or _hoy(conn)
    filas = conn.execute(
        f"""
        SELECT p.nombre AS persona,
               (SELECT max(m.fecha) FROM updates u
                  JOIN meetings m ON m.id = u.meeting_id
                 WHERE u.persona_id = p.id
                   AND m.fecha <= ?) AS ultimo_update,
               (SELECT count(*) FROM acciones_vigentes a
                 WHERE a.persona_id = p.id
                   AND a.estado NOT IN ({_placeholders(ESTADOS_CERRADOS)}))
                   AS abiertas,
               (SELECT count(*) FROM acciones_vigentes a
                 WHERE a.persona_id = p.id
                   AND a.menciones >= ?
                   AND a.estado NOT IN ({_placeholders(ESTADOS_CERRADOS)}))
                   AS estancadas
          FROM personas p
         ORDER BY p.nombre
        """,
        (referencia, *ESTADOS_CERRADOS, UMBRAL_ESTANCAMIENTO, *ESTADOS_CERRADOS),
    ).fetchall()

    resultado = []
    for fila in filas:
        registro = dict(fila)
        if registro["ultimo_update"]:
            try:
                registro["dias_sin_update"] = (
                    date.fromisoformat(referencia)
                    - date.fromisoformat(registro["ultimo_update"])
                ).days
            except ValueError:
                registro["dias_sin_update"] = None
        else:
            # Nunca ha dado un update: existe porque alguien le asigno una
            # accion o porque hablo en una transcripcion.
            registro["dias_sin_update"] = None
        if not registro["abiertas"] and registro["ultimo_update"] is None:
            continue
        if dias is not None:
            if registro["dias_sin_update"] is None or registro["dias_sin_update"] < dias:
                continue
        resultado.append(registro)

    resultado.sort(
        key=lambda r: (
            -(r["dias_sin_update"] if r["dias_sin_update"] is not None else 10**6),
            -r["abiertas"],
            r["persona"].lower(),
        )
    )
    return resultado[:limite]


def acciones_sin_mencion(
    conn: sqlite3.Connection,
    *,
    minimo: int = 1,
    limite: int = 50,
) -> list[sqlite3.Row]:
    """Acciones abiertas y cuantas reuniones se han celebrado sin nombrarlas.

    Es el pendiente que la Fase 2 dejo anotado: el modelo puede responder
    `sin_mencion` a un arrastre, pero eso no cambia nada en la base y por
    tanto no se ve en ningun `.md`. Aqui el dato se **deduce**: reuniones
    posteriores a la ultima que la menciono.

    Se cuenta por fecha, asi que dos reuniones del mismo dia que la ultima
    mencion no suman. Es el historico entero, no el periodo: una accion no
    tiene fecha propia y recortarla daria un numero sin sentido, igual que en
    `metricas()`.
    """
    return conn.execute(
        f"""
        SELECT a.id, a.uid, a.descripcion, a.estado, a.menciones,
               p.nombre AS persona,
               mo.uid AS origen_uid, mo.fecha AS origen_fecha,
               mo.titulo AS origen_titulo,
               COALESCE(mu.uid, mo.uid) AS ultima_uid,
               COALESCE(mu.fecha, mo.fecha) AS ultima_fecha,
               -- El WHERE ya deja fuera las cerradas, asi que aqui la
               -- definicion de estancada se reduce al umbral de menciones.
               (a.menciones >= ?) AS estancada,
               (SELECT count(*) FROM meetings m2
                 WHERE m2.fecha > COALESCE(mu.fecha, mo.fecha))
                   AS reuniones_sin_mencion
        {_DESDE_ACCIONES}
         WHERE a.estado NOT IN ({_placeholders(ESTADOS_CERRADOS)})
           AND (SELECT count(*) FROM meetings m2
                 WHERE m2.fecha > COALESCE(mu.fecha, mo.fecha)) >= ?
         ORDER BY reuniones_sin_mencion DESC, a.menciones DESC, a.id
         LIMIT ?
        """,
        (UMBRAL_ESTANCAMIENTO, *ESTADOS_CERRADOS, minimo, limite),
    ).fetchall()


def acciones_abiertas(
    conn: sqlite3.Connection,
    ultimas_reuniones: int = 5,
    excluir_meeting_id: int | None = None,
) -> list[dict]:
    """Acciones vigentes sin cerrar vistas por ultima vez en las ultimas N reuniones.

    Lee de `acciones_vigentes`: lo descartado y lo absorbido no se le ofrece
    al modelo. La descripcion es la vigente (la corregida, si la hay), que es
    la que el equipo reconocera. Cada una lleva en `bloqueada_por` los ids de
    las acciones abiertas de las que depende.

    `excluir_meeting_id` no se limita a filtrar: al reprocesar, esa pasada se
    va a deshacer igualmente, asi que sus acciones propias no se ofrecen y las
    ajenas se devuelven con el estado y las menciones **derivados sin ella**.
    Sin esto, una accion que la pasada anterior dio por completada no volveria
    a ofrecerse y el reproceso no seria idempotente.
    """
    recientes = {
        fila["id"]: fila["fecha"]
        for fila in conn.execute(
            "SELECT id, fecha FROM meetings WHERE id IS NOT ? "
            "ORDER BY fecha DESC, id DESC LIMIT ?",
            (excluir_meeting_id, ultimas_reuniones),
        )
    }
    tocadas: set[int] = set()
    if excluir_meeting_id is not None:
        for fila in conn.execute(
            """
            SELECT am.action_id, a.absorbida_por
              FROM action_mentions am JOIN actions a ON a.id = am.action_id
             WHERE am.meeting_id = ?
            """,
            (excluir_meeting_id,),
        ):
            tocadas.add(fila["absorbida_por"] or fila["action_id"])

    resultado = []
    for fila in conn.execute(
        """
        SELECT a.id, a.uid, a.descripcion, a.estado, a.menciones,
               a.meeting_id_ultima, p.nombre AS persona
          FROM acciones_vigentes a
          LEFT JOIN personas p ON p.id = a.persona_id
         WHERE a.meeting_id_origen IS NOT ?
        """,
        (excluir_meeting_id,),
    ).fetchall():
        accion = dict(fila)
        if accion["id"] in tocadas:
            derivado = _derivar_accion(conn, accion["id"], excluir_meeting_id)
            if derivado is None:
                continue
            accion.update(
                estado=derivado["estado"],
                menciones=derivado["menciones"],
                meeting_id_ultima=derivado["meeting_id_ultima"],
            )
        if accion["estado"] in ESTADOS_CERRADOS:
            continue
        if accion["meeting_id_ultima"] not in recientes:
            continue
        accion["ultima_fecha"] = recientes[accion["meeting_id_ultima"]]
        accion["bloqueada_por"] = [
            dep["id"]
            for dep in conn.execute(
                f"""
                SELECT b.id FROM action_dependencias d
                  JOIN acciones_vigentes b ON b.id = d.depende_de_id
                 WHERE d.action_id = ?
                   AND b.estado NOT IN ({_placeholders(ESTADOS_CERRADOS)})
                 ORDER BY b.id
                """,
                (accion["id"], *ESTADOS_CERRADOS),
            )
        ]
        del accion["meeting_id_ultima"]
        resultado.append(accion)
    resultado.sort(key=lambda a: (-a["menciones"], a["id"]))
    return resultado


def aplicar_arrastres(
    conn: sqlite3.Connection, meeting_id: int, arrastres: list[dict]
) -> int:
    """Anota lo que esta reunion dijo de acciones de reuniones anteriores.

    Cada arrastre es una fila de `action_mentions` (con su comentario) y la
    accion se recalcula desde ahi, asi que el orden importa por **fecha** de
    reunion y no por orden de proceso, y una correccion humana de estado solo
    cede ante reuniones posteriores a ella.

    Los `action_id` inexistentes se descartan en silencio: el LLM puede
    inventarselos, y es mas seguro perder una actualizacion que corromper una
    accion ajena. Tampoco se anota una mencion de una accion a su propia
    reunion de origen.
    """
    aplicados = 0
    for arrastre in arrastres:
        try:
            action_id = int(arrastre.get("action_id"))
        except (TypeError, ValueError):
            continue
        estado = str(arrastre.get("estado") or "").strip().lower()
        if estado in ("", "sin_mencion") or estado not in ESTADOS_ACCION:
            continue
        fila = conn.execute(
            "SELECT id, meeting_id_origen FROM actions WHERE id = ?", (action_id,)
        ).fetchone()
        if not fila or fila["meeting_id_origen"] == meeting_id:
            continue
        _anotar_mencion(
            conn,
            action_id,
            meeting_id,
            estado,
            str(arrastre.get("comentario") or "").strip() or None,
        )
        _recalcular_accion(conn, action_id)
        aplicados += 1
    return aplicados


# --------------------------------------------------------------------------
# Correccion humana de acciones (Fase 7, seccion 7.2)
# --------------------------------------------------------------------------
# Todas las operaciones corren en una transaccion (`with conn`: confirman al
# terminar y deshacen si algo falla), dejan su fila en `correcciones` y
# devuelven la ficha de la accion resultante. La API solo las llama.
#
# Deshacer no borra el historial: marca `deshecha_en` en la correccion que se
# revierte, y con eso deja de proteger la accion frente al reproceso.


class AccionNoEncontrada(LookupError):
    """No hay ninguna accion con ese uid."""


class CorreccionInvalida(ValueError):
    """La operacion rompe una regla (ciclo, accion descartada, fusion en cadena...)."""


# `persona=None` significa "sin responsable"; para "no cambiar" hace falta
# un valor distinto.
SIN_CAMBIO = object()


def _accion_por_uid(conn: sqlite3.Connection, uid: str) -> sqlite3.Row:
    fila = conn.execute("SELECT * FROM actions WHERE uid = ?", (uid,)).fetchone()
    if fila is None:
        raise AccionNoEncontrada(f"No existe la accion {uid!r}")
    return fila


def _registrar_correccion(
    conn: sqlite3.Connection,
    action_id: int,
    campo: str,
    anterior,
    nuevo,
    origen: str,
) -> int:
    if origen not in ORIGENES_CORRECCION:
        raise CorreccionInvalida(
            f"Origen desconocido {origen!r}; usa uno de {ORIGENES_CORRECCION}"
        )
    cur = conn.execute(
        """
        INSERT INTO correcciones
            (action_id, campo, valor_anterior, valor_nuevo, origen, creado_en)
        VALUES (?, ?, ?, ?, ?, datetime('now'))
        """,
        (action_id, campo, anterior, nuevo, origen),
    )
    return cur.lastrowid


def _nombre_persona(conn: sqlite3.Connection, persona_id: int | None) -> str | None:
    if persona_id is None:
        return None
    fila = conn.execute("SELECT nombre FROM personas WHERE id = ?", (persona_id,)).fetchone()
    return fila["nombre"] if fila else None


def _exigir_vigente(accion: sqlite3.Row, que: str) -> None:
    if accion["descartada_en"]:
        raise CorreccionInvalida(
            f"La accion {accion['uid']} esta descartada; restaurala antes de {que}."
        )
    if accion["absorbida_por"]:
        raise CorreccionInvalida(
            f"La accion {accion['uid']} esta fusionada en otra; separala antes "
            f"de {que}."
        )


def _dependencias_abiertas(conn: sqlite3.Connection, action_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        f"""
        SELECT b.uid, b.descripcion, b.estado
          FROM action_dependencias d
          JOIN acciones_vigentes b ON b.id = d.depende_de_id
         WHERE d.action_id = ?
           AND b.estado NOT IN ({_placeholders(ESTADOS_CERRADOS)})
         ORDER BY b.id
        """,
        (action_id, *ESTADOS_CERRADOS),
    ).fetchall()


def _crea_ciclo(conn: sqlite3.Connection, action_id: int, depende_de_id: int) -> bool:
    """Si `action_id -> depende_de_id` cerraria un ciclo.

    Recorrido en Python sobre la tabla: hay cientos de filas, no millones, y
    una CTE recursiva no dice nada que este bucle no diga mas claro.
    """
    pendientes, vistos = [depende_de_id], set()
    while pendientes:
        actual = pendientes.pop()
        if actual == action_id:
            return True
        if actual in vistos:
            continue
        vistos.add(actual)
        pendientes.extend(
            fila[0]
            for fila in conn.execute(
                "SELECT depende_de_id FROM action_dependencias WHERE action_id = ?",
                (actual,),
            )
        )
    return False


def corregir_accion(
    conn: sqlite3.Connection,
    uid: str,
    *,
    descripcion: str | None = None,
    persona=SIN_CAMBIO,
    estado: str | None = None,
    origen: str = "web",
) -> dict:
    """Cambia descripcion, responsable y/o estado de una accion vigente.

    `persona` pasa por `obtener_o_crear_persona` (`None` o "" la deja sin
    responsable). Cerrar una accion con dependencias abiertas **se permite y
    se avisa** en `avisos`: si alguien dice en la daily que ya esta, el
    vinculo era erroneo o ya no importa.
    """
    avisos: list[str] = []
    with conn:
        accion = _accion_por_uid(conn, uid)
        _exigir_vigente(accion, "corregirla")

        if descripcion is not None:
            nueva = " ".join(str(descripcion).split())
            if not nueva:
                raise CorreccionInvalida("La descripcion no puede quedar vacia.")
            if nueva != accion["descripcion"]:
                conn.execute(
                    "UPDATE actions SET descripcion = ? WHERE id = ?", (nueva, accion["id"])
                )
                _registrar_correccion(
                    conn, accion["id"], "descripcion", accion["descripcion"], nueva, origen
                )

        if persona is not SIN_CAMBIO:
            persona_id = obtener_o_crear_persona(conn, persona) if persona else None
            if persona_id != accion["persona_id"]:
                conn.execute(
                    "UPDATE actions SET persona_id = ? WHERE id = ?",
                    (persona_id, accion["id"]),
                )
                _registrar_correccion(
                    conn,
                    accion["id"],
                    "persona",
                    _nombre_persona(conn, accion["persona_id"]),
                    _nombre_persona(conn, persona_id),
                    origen,
                )

        if estado is not None:
            nuevo = str(estado).strip().lower()
            if nuevo not in ESTADOS_ACCION:
                raise CorreccionInvalida(
                    f"Estado desconocido {estado!r}; usa uno de {ESTADOS_ACCION}"
                )
            if nuevo != accion["estado"]:
                _registrar_correccion(
                    conn, accion["id"], "estado", accion["estado"], nuevo, origen
                )
                _recalcular_accion(conn, accion["id"])
                if nuevo in ESTADOS_CERRADOS:
                    for dep in _dependencias_abiertas(conn, accion["id"]):
                        avisos.append(
                            f"Se ha cerrado aunque depende de [{dep['uid']}] "
                            f"«{dep['descripcion']}», que sigue {dep['estado']}."
                        )
    return {**detalle_accion(conn, uid), "avisos": avisos}


def deshacer_correccion(
    conn: sqlite3.Connection, correccion_id: int, origen: str = "web"
) -> dict:
    """Revierte una correccion de descripcion, responsable o estado.

    Solo la ultima vigente de ese campo: deshacer una intermedia dejaria un
    valor que nadie eligio. Descartes, fusiones y dependencias tienen su propia
    operacion inversa (`restaurar_accion`, `separar_acciones`,
    `quitar_dependencia`).
    """
    with conn:
        correccion = conn.execute(
            "SELECT * FROM correcciones WHERE id = ?", (correccion_id,)
        ).fetchone()
        if correccion is None:
            raise CorreccionInvalida(f"No existe la correccion {correccion_id}")
        if correccion["deshecha_en"]:
            raise CorreccionInvalida(f"La correccion {correccion_id} ya estaba deshecha")
        campo = correccion["campo"]
        if campo not in ("descripcion", "persona", "estado"):
            raise CorreccionInvalida(
                f"Una correccion de tipo {campo!r} se deshace con su propia operacion"
            )
        ultima = _correccion_vigente(conn, correccion["action_id"], campo)
        if ultima["id"] != correccion_id:
            raise CorreccionInvalida(
                f"Hay una correccion de {campo} posterior ({ultima['id']}); "
                "deshaz esa primero"
            )
        if origen not in ORIGENES_CORRECCION:
            raise CorreccionInvalida(f"Origen desconocido {origen!r}")
        action_id = correccion["action_id"]
        if campo == "descripcion":
            conn.execute(
                "UPDATE actions SET descripcion = ? WHERE id = ?",
                (correccion["valor_anterior"], action_id),
            )
        elif campo == "persona":
            conn.execute(
                "UPDATE actions SET persona_id = ? WHERE id = ?",
                (obtener_o_crear_persona(conn, correccion["valor_anterior"]), action_id),
            )
        conn.execute(
            "UPDATE correcciones SET deshecha_en = datetime('now') WHERE id = ?",
            (correccion_id,),
        )
        if campo == "estado":
            _recalcular_accion(conn, action_id)
        uid = conn.execute("SELECT uid FROM actions WHERE id = ?", (action_id,)).fetchone()[0]
    return detalle_accion(conn, uid)


def descartar_accion(
    conn: sqlite3.Connection, uid: str, motivo: str | None = None, origen: str = "web"
) -> dict:
    """Descarte reversible: la accion no se ve, no cuenta y no se ofrece al LLM.

    Nunca es un DELETE. Si se reprocesa la reunion y el modelo la vuelve a
    generar, sigue descartada. Las dependencias **hacia** ella se borran (una
    tarea no puede esperar a algo que no existe) y quedan anotadas en la
    correccion para que `restaurar_accion` las reponga.
    """
    with conn:
        accion = _accion_por_uid(conn, uid)
        _exigir_vigente(accion, "descartarla")
        dependientes = [
            {"action_id": fila["action_id"], "creado_en": fila["creado_en"]}
            for fila in conn.execute(
                "SELECT action_id, creado_en FROM action_dependencias "
                "WHERE depende_de_id = ? ORDER BY action_id",
                (accion["id"],),
            )
        ]
        conn.execute("DELETE FROM action_dependencias WHERE depende_de_id = ?", (accion["id"],))
        motivo = " ".join(str(motivo or "").split()) or None
        conn.execute(
            "UPDATE actions SET descartada_en = datetime('now'), motivo_descarte = ? "
            "WHERE id = ?",
            (motivo, accion["id"]),
        )
        _registrar_correccion(
            conn,
            accion["id"],
            "descarte",
            json.dumps({"dependientes": dependientes}),
            motivo,
            origen,
        )
    return detalle_accion(conn, uid)


def restaurar_accion(conn: sqlite3.Connection, uid: str, origen: str = "web") -> dict:
    """Deshace `descartar_accion`, dependencias incluidas si siguen siendo validas."""
    if origen not in ORIGENES_CORRECCION:
        raise CorreccionInvalida(f"Origen desconocido {origen!r}")
    with conn:
        accion = _accion_por_uid(conn, uid)
        if not accion["descartada_en"]:
            raise CorreccionInvalida(f"La accion {uid} no esta descartada")
        conn.execute(
            "UPDATE actions SET descartada_en = NULL, motivo_descarte = NULL WHERE id = ?",
            (accion["id"],),
        )
        correccion = _correccion_vigente(conn, accion["id"], "descarte")
        if correccion:
            try:
                dependientes = json.loads(correccion["valor_anterior"] or "{}").get(
                    "dependientes", []
                )
            except (json.JSONDecodeError, AttributeError):
                dependientes = []
            for dep in dependientes:
                otra = conn.execute(
                    "SELECT id FROM acciones_vigentes WHERE id = ?", (dep["action_id"],)
                ).fetchone()
                if otra and not _crea_ciclo(conn, otra["id"], accion["id"]):
                    conn.execute(
                        "INSERT OR IGNORE INTO action_dependencias "
                        "(action_id, depende_de_id, creado_en) VALUES (?, ?, ?)",
                        (otra["id"], accion["id"], dep["creado_en"]),
                    )
            conn.execute(
                "UPDATE correcciones SET deshecha_en = datetime('now') WHERE id = ?",
                (correccion["id"],),
            )
    return detalle_accion(conn, uid)


def fusionar_acciones(
    conn: sqlite3.Connection,
    principal_uid: str,
    duplicada_uid: str,
    origen: str = "web",
) -> dict:
    """Marca `duplicada` como absorbida por `principal`.

    - Las menciones no se mueven: la principal pasa a contar la **union** de
      las suyas y las de sus absorbidas, asi que una reunion que nombro a las
      dos cuenta una vez y separar devuelve a cada una lo suyo.
    - El estado de la principal no cambia.
    - No hay fusiones en cadena: si la duplicada ya absorbia otras, pasan a la
      principal; y no se puede fusionar una accion ya absorbida ni en una.
    - Las dependencias de la duplicada se trasladan a la principal (salvo las
      que serian consigo misma o cerrarian un ciclo) y quedan anotadas para
      poder separarlas.

    `meeting_id_origen` de la principal **no** cambia: es el ancla con la que
    la reconciliacion encuentra la accion al reprocesar su reunion. La fecha
    mas antigua se ve en las menciones de `detalle_accion`.
    """
    with conn:
        principal = _accion_por_uid(conn, principal_uid)
        duplicada = _accion_por_uid(conn, duplicada_uid)
        if principal["id"] == duplicada["id"]:
            raise CorreccionInvalida("Una accion no se puede fusionar consigo misma.")
        if duplicada["absorbida_por"]:
            raise CorreccionInvalida(f"La accion {duplicada_uid} ya esta fusionada en otra.")
        _exigir_vigente(principal, "fusionar en ella")
        if duplicada["descartada_en"]:
            raise CorreccionInvalida(
                f"La accion {duplicada_uid} esta descartada; restaurala antes de fusionarla."
            )

        trasladadas = [
            fila["id"]
            for fila in conn.execute(
                "SELECT id FROM actions WHERE absorbida_por = ? ORDER BY id",
                (duplicada["id"],),
            )
        ]
        conn.execute(
            "UPDATE actions SET absorbida_por = ? WHERE absorbida_por = ?",
            (principal["id"], duplicada["id"]),
        )

        originales = [
            dict(fila)
            for fila in conn.execute(
                "SELECT action_id, depende_de_id, creado_en FROM action_dependencias "
                "WHERE action_id = ? OR depende_de_id = ? ORDER BY action_id, depende_de_id",
                (duplicada["id"], duplicada["id"]),
            )
        ]
        conn.execute(
            "DELETE FROM action_dependencias WHERE action_id = ? OR depende_de_id = ?",
            (duplicada["id"], duplicada["id"]),
        )
        insertadas = []
        for dep in originales:
            desde = principal["id"] if dep["action_id"] == duplicada["id"] else dep["action_id"]
            hacia = (
                principal["id"] if dep["depende_de_id"] == duplicada["id"] else dep["depende_de_id"]
            )
            if desde == hacia or _crea_ciclo(conn, desde, hacia):
                continue
            cur = conn.execute(
                "INSERT OR IGNORE INTO action_dependencias "
                "(action_id, depende_de_id, creado_en) VALUES (?, ?, ?)",
                (desde, hacia, dep["creado_en"]),
            )
            if cur.rowcount:
                insertadas.append({"action_id": desde, "depende_de_id": hacia})

        conn.execute(
            "UPDATE actions SET absorbida_por = ? WHERE id = ?",
            (principal["id"], duplicada["id"]),
        )
        _registrar_correccion(
            conn,
            duplicada["id"],
            "fusion",
            json.dumps(
                {
                    "trasladadas": trasladadas,
                    "dependencias": originales,
                    "insertadas": insertadas,
                }
            ),
            principal_uid,
            origen,
        )
        _recalcular_accion(conn, duplicada["id"])
        for otra in trasladadas:
            _recalcular_accion(conn, otra)
    return detalle_accion(conn, principal_uid)


def separar_acciones(conn: sqlite3.Connection, duplicada_uid: str, origen: str = "web") -> dict:
    """Deshace `fusionar_acciones`: cada una recupera sus menciones y sus vinculos."""
    if origen not in ORIGENES_CORRECCION:
        raise CorreccionInvalida(f"Origen desconocido {origen!r}")
    with conn:
        duplicada = _accion_por_uid(conn, duplicada_uid)
        if not duplicada["absorbida_por"]:
            raise CorreccionInvalida(f"La accion {duplicada_uid} no esta fusionada.")
        principal_id = duplicada["absorbida_por"]
        correccion = _correccion_vigente(conn, duplicada["id"], "fusion")
        try:
            info = json.loads(correccion["valor_anterior"]) if correccion else {}
        except json.JSONDecodeError:
            info = {}

        for dep in info.get("insertadas", []):
            conn.execute(
                "DELETE FROM action_dependencias WHERE action_id = ? AND depende_de_id = ?",
                (dep["action_id"], dep["depende_de_id"]),
            )
        for dep in info.get("dependencias", []):
            conn.execute(
                "INSERT OR IGNORE INTO action_dependencias "
                "(action_id, depende_de_id, creado_en) VALUES (?, ?, ?)",
                (dep["action_id"], dep["depende_de_id"], dep["creado_en"]),
            )
        for otra in info.get("trasladadas", []):
            conn.execute(
                "UPDATE actions SET absorbida_por = ? WHERE id = ? AND absorbida_por = ?",
                (duplicada["id"], otra, principal_id),
            )
        conn.execute("UPDATE actions SET absorbida_por = NULL WHERE id = ?", (duplicada["id"],))
        if correccion:
            conn.execute(
                "UPDATE correcciones SET deshecha_en = datetime('now') WHERE id = ?",
                (correccion["id"],),
            )
        _recalcular_accion(conn, duplicada["id"])
        _recalcular_accion(conn, principal_id)
    return detalle_accion(conn, duplicada_uid)


def anadir_dependencia(
    conn: sqlite3.Connection, uid: str, depende_de_uid: str, origen: str = "web"
) -> dict:
    """`uid` no puede avanzar hasta `depende_de_uid`.

    Solo se muestra y se da como contexto al LLM. Rechaza ciclos, la
    dependencia consigo misma y las acciones descartadas o absorbidas.
    """
    with conn:
        accion = _accion_por_uid(conn, uid)
        otra = _accion_por_uid(conn, depende_de_uid)
        if accion["id"] == otra["id"]:
            raise CorreccionInvalida("Una accion no puede depender de si misma.")
        _exigir_vigente(accion, "anadirle dependencias")
        _exigir_vigente(otra, "depender de ella")
        existe = conn.execute(
            "SELECT 1 FROM action_dependencias WHERE action_id = ? AND depende_de_id = ?",
            (accion["id"], otra["id"]),
        ).fetchone()
        if not existe:
            if _crea_ciclo(conn, accion["id"], otra["id"]):
                raise CorreccionInvalida(
                    f"{depende_de_uid} ya depende (directa o indirectamente) de {uid}: "
                    "seria un ciclo."
                )
            conn.execute(
                "INSERT INTO action_dependencias (action_id, depende_de_id, creado_en) "
                "VALUES (?, ?, datetime('now'))",
                (accion["id"], otra["id"]),
            )
            _registrar_correccion(
                conn, accion["id"], "dependencia", None, depende_de_uid, origen
            )
    return detalle_accion(conn, uid)


def quitar_dependencia(
    conn: sqlite3.Connection, uid: str, depende_de_uid: str, origen: str = "web"
) -> dict:
    with conn:
        accion = _accion_por_uid(conn, uid)
        otra = _accion_por_uid(conn, depende_de_uid)
        cur = conn.execute(
            "DELETE FROM action_dependencias WHERE action_id = ? AND depende_de_id = ?",
            (accion["id"], otra["id"]),
        )
        if not cur.rowcount:
            raise CorreccionInvalida(f"{uid} no depende de {depende_de_uid}")
        alta = conn.execute(
            """
            SELECT id FROM correcciones
             WHERE action_id = ? AND campo = 'dependencia' AND valor_nuevo = ?
               AND deshecha_en IS NULL
             ORDER BY id DESC LIMIT 1
            """,
            (accion["id"], depende_de_uid),
        ).fetchone()
        if alta:
            conn.execute(
                "UPDATE correcciones SET deshecha_en = datetime('now') WHERE id = ?",
                (alta["id"],),
            )
        else:
            # Un vinculo que no se dio de alta con `anadir_dependencia` (lo
            # trasladó una fusion, lo repuso una restauracion): queda anotado
            # que alguien lo quito.
            _registrar_correccion(
                conn, accion["id"], "dependencia", depende_de_uid, None, origen
            )
    return detalle_accion(conn, uid)


def detalle_accion(conn: sqlite3.Connection, uid: str) -> dict:
    """Ficha completa de una accion, **tambien si esta descartada o absorbida**.

    Menciones con fecha y comentario (las de sus absorbidas incluidas, con el
    uid de la que las recibio), dependencias en los dos sentidos, absorbidas y
    el historial de correcciones.
    """
    accion = conn.execute(
        """
        SELECT a.id, a.uid, a.descripcion, a.descripcion_llm, a.estado,
               a.menciones, a.cerrada_en, a.descartada_en, a.motivo_descarte,
               a.revisar, p.nombre AS persona,
               mo.uid AS origen_uid, mo.fecha AS origen_fecha,
               COALESCE(mu.uid, mo.uid) AS ultima_uid,
               COALESCE(mu.fecha, mo.fecha) AS ultima_fecha,
               ab.uid AS absorbida_por
          FROM actions a
          LEFT JOIN personas p ON p.id = a.persona_id
          JOIN meetings mo ON mo.id = a.meeting_id_origen
          LEFT JOIN meetings mu ON mu.id = a.meeting_id_ultima
          LEFT JOIN actions ab ON ab.id = a.absorbida_por
         WHERE a.uid = ?
        """,
        (uid,),
    ).fetchone()
    if accion is None:
        raise AccionNoEncontrada(f"No existe la accion {uid!r}")
    ficha = dict(accion)
    action_id = ficha.pop("id")
    ficha["revisar"] = bool(ficha["revisar"])
    ficha["estancada"] = (
        ficha["menciones"] >= UMBRAL_ESTANCAMIENTO
        and ficha["estado"] not in ESTADOS_CERRADOS
    )
    ficha["absorbidas"] = [
        dict(f)
        for f in conn.execute(
            "SELECT uid, descripcion, estado FROM actions WHERE absorbida_por = ? ORDER BY id",
            (action_id,),
        )
    ]
    ficha["menciones_detalle"] = [
        dict(f)
        for f in conn.execute(
            """
            SELECT m.uid AS reunion_uid, m.fecha, m.titulo, am.estado,
                   am.comentario, x.uid AS accion_uid
              FROM action_mentions am
              JOIN meetings m ON m.id = am.meeting_id
              JOIN actions x ON x.id = am.action_id
             WHERE am.action_id = ? OR x.absorbida_por = ?
             ORDER BY m.fecha, m.id, x.id
            """,
            (action_id, action_id),
        )
    ]
    ficha["depende_de"] = [
        dict(f)
        for f in conn.execute(
            """
            SELECT b.uid, b.descripcion, b.estado, d.creado_en
              FROM action_dependencias d JOIN actions b ON b.id = d.depende_de_id
             WHERE d.action_id = ?
             ORDER BY b.id
            """,
            (action_id,),
        )
    ]
    ficha["bloquea_a"] = [
        dict(f)
        for f in conn.execute(
            """
            SELECT b.uid, b.descripcion, b.estado, d.creado_en
              FROM action_dependencias d JOIN actions b ON b.id = d.action_id
             WHERE d.depende_de_id = ?
             ORDER BY b.id
            """,
            (action_id,),
        )
    ]
    ficha["correcciones"] = [
        dict(f)
        for f in conn.execute(
            """
            SELECT id, campo, valor_anterior, valor_nuevo, origen, creado_en, deshecha_en
              FROM correcciones
             WHERE action_id = ?
             ORDER BY creado_en, id
            """,
            (action_id,),
        )
    ]
    return ficha


# --------------------------------------------------------------------------
# CLI de mantenimiento
# --------------------------------------------------------------------------


def _main() -> None:
    """`python memoria.py --migrar` / `--info`.

    Existe por el servidor cerrado: la API se abre en solo lectura y no puede
    migrar la base, asi que hace falta una forma explicita de hacerlo sin
    tener que procesar una reunion entera ni abrir una shell de Python.
    """
    import argparse

    parser = argparse.ArgumentParser(description="Mantenimiento de meetings.db")
    parser.add_argument("--db", help="Ruta de la base (por defecto: TEAMS_DB)")
    parser.add_argument(
        "--migrar",
        action="store_true",
        help="Aplica las migraciones pendientes del esquema",
    )
    parser.add_argument(
        "--info", action="store_true", help="Muestra las cifras de la base"
    )
    args = parser.parse_args()

    path = ruta_bd(args.db)
    if not (args.migrar or args.info):
        parser.error("indica --migrar o --info")

    if args.migrar:
        if not path.exists():
            parser.error(f"No existe la base de datos: {path}")
        cruda = sqlite3.connect(path)
        try:
            antes = cruda.execute("PRAGMA user_version").fetchone()[0]
            perdidas = menciones_perdidas_en_migracion(cruda) if antes < 3 else 0
        finally:
            cruda.close()
        conn = conectar(path)  # migra al abrir
        despues = conn.execute("PRAGMA user_version").fetchone()[0]
        conn.close()
        if antes == despues:
            print(f"{path}: ya estaba en el esquema {despues}, nada que hacer.")
        else:
            print(f"{path}: migrada del esquema {antes} al {despues}.")
            if perdidas:
                print(
                    f"  Aviso: {perdidas} acciones tenian mas menciones que las dos "
                    "que v2 guardaba (origen y ultima). Las intermedias no se "
                    "pueden recuperar; su contador se conserva hasta que se "
                    "recalculen."
                )

    if args.info:
        conn = conectar(path, solo_lectura=True)
        try:
            for clave, valor in resumen_bd(conn).items():
                print(f"{clave:>18}: {valor}")
        finally:
            conn.close()


if __name__ == "__main__":
    _main()
