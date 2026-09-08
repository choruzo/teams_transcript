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
from pathlib import Path

ESQUEMA_VERSION = 2

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

# El LLM razona mejor con etiquetas que con numeros; la BD guarda un valor
# comparable. Este es el unico punto donde se traduce entre ambos.
CONFIANZA_A_NUMERO = {"alta": 0.9, "media": 0.6, "baja": 0.3}

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
  cerrada_en          TEXT
);

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


def conectar(
    ruta: Path | str | None = None, solo_lectura: bool = False
) -> sqlite3.Connection:
    """Abre (creando si hace falta) la BD y garantiza el esquema.

    Con `solo_lectura` la conexion rechaza cualquier escritura y **no** toca el
    esquema: es la que debe usar todo lo que solo consulta (la API web), para
    que un error de programacion no pueda corromper el historico.
    """
    path = ruta_bd(ruta)
    if solo_lectura:
        if not path.exists():
            raise FileNotFoundError(f"No existe la base de datos: {path}")
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        # Se usa `query_only` y no la URI `mode=ro` a proposito: con la base en
        # modo WAL, una conexion abierta como `mode=ro` no puede crear el
        # fichero `-shm` que SQLite necesita para leer, y falla justo cuando
        # nadie mas esta escribiendo (el caso normal). `query_only` da la misma
        # garantia -- cualquier INSERT/UPDATE/DELETE lanza excepcion -- sin ese
        # problema.
        conn.execute("PRAGMA query_only = ON")
        return conn

    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
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
    manuales, asi que tiene que sobrevivir a un reprocesado: `meetings.id` no
    sirve porque reprocesar borra e inserta la fila.

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
    """Inserta la reunion y devuelve su id.

    Con `reemplazar` (por defecto), volver a procesar la misma transcripcion
    borra la reunion anterior en vez de duplicarla: el pipeline se ejecuta a
    mano y se repite a menudo mientras se afinan prompts. El borrado arrastra
    en cascada segmentos, updates, riesgos y acciones originadas en ella.

    **La identidad de la reunion sobrevive al reemplazo**: la fila nueva
    conserva el `uid` y tambien el `id` de la anterior. Sin eso, cada
    reprocesado dejaria invalidos los enlaces guardados y las citas que la
    interfaz genera para justificar sus respuestas.
    """
    if tipo not in TIPOS_REUNION:
        raise ValueError(f"Tipo de reunion desconocido: {tipo!r}")

    id_previo = None
    uid = None
    if reemplazar and transcript_path:
        for antigua in conn.execute(
            "SELECT id, uid FROM meetings WHERE transcript_path = ? ORDER BY id",
            (transcript_path,),
        ).fetchall():
            _deshacer_arrastres(conn, antigua["id"])
            id_previo = antigua["id"]
            uid = antigua["uid"] or uid
        conn.execute(
            "DELETE FROM meetings WHERE transcript_path = ?", (transcript_path,)
        )

    if uid is None:
        uid = _uid_disponible(conn, transcript_path)

    # `id_previo` a None deja que SQLite asigne el siguiente id; con valor,
    # reutiliza el de la fila que se acaba de borrar.
    cur = conn.execute(
        """
        INSERT INTO meetings (
            id, uid, fecha, titulo, tipo, audio_path, transcript_path,
            duracion_seg, modelo_whisper, modelo_llm, resumen, datos_json,
            creado_en
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
        """,
        (
            id_previo,
            uid,
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
        ),
    )
    return cur.lastrowid


def _deshacer_arrastres(conn: sqlite3.Connection, meeting_id: int) -> None:
    """Revierte el efecto de una reunion sobre acciones de reuniones anteriores.

    Necesario antes de borrar una reunion que se va a reprocesar: sus acciones
    propias se van en cascada, pero las ajenas la referencian en
    `meeting_id_ultima` (sin ON DELETE, el borrado fallaria) y ademas conservan
    las menciones y el estado que esa pasada les puso.

    El estado anterior no se guarda en ninguna parte, asi que las que quedaron
    cerradas por esta reunion vuelven a 'abierta': es lo menos danino, y la
    nueva pasada las volvera a marcar segun lo que diga la transcripcion.
    """
    conn.execute(
        """
        UPDATE actions
           SET meeting_id_ultima = meeting_id_origen,
               menciones = MAX(1, menciones - 1),
               cerrada_en = NULL,
               estado = CASE WHEN estado IN ('completada', 'abandonada')
                             THEN 'abierta' ELSE estado END
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
    """Da de alta las acciones nuevas nacidas en esta reunion."""
    insertadas = 0
    for accion in acciones:
        descripcion = str(accion.get("descripcion") or "").strip()
        if not descripcion:
            continue
        estado = str(accion.get("estado") or "abierta").strip().lower()
        if estado not in ESTADOS_ACCION:
            estado = "abierta"
        persona_id = obtener_o_crear_persona(conn, accion.get("persona"))
        conn.execute(
            """
            INSERT INTO actions (
                descripcion, persona_id, meeting_id_origen, meeting_id_ultima,
                estado, menciones, cerrada_en
            ) VALUES (?, ?, ?, ?, ?, 1, ?)
            """,
            (
                descripcion,
                persona_id,
                meeting_id,
                meeting_id,
                estado,
                _hoy(conn) if estado in ("completada", "abandonada") else None,
            ),
        )
        insertadas += 1
    return insertadas


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


# `excluir_meeting_id` no se limita a filtrar: simula el efecto de
# `_deshacer_arrastres`, porque al reprocesar una reunion esa pasada se va a
# deshacer igualmente. Sin esto, una accion ajena que la pasada anterior marco
# como completada no volveria a ofrecerse al modelo y el reproceso no seria
# idempotente.
_SQL_ACCIONES_ABIERTAS = """
WITH excluida(id) AS (SELECT ?),
recientes AS (
    SELECT id FROM meetings
     WHERE id IS NOT (SELECT id FROM excluida)
     ORDER BY fecha DESC, id DESC
     LIMIT ?
),
calc AS (
    SELECT a.id,
           a.descripcion,
           a.persona_id,
           CASE WHEN a.meeting_id_ultima IS (SELECT id FROM excluida)
                     AND a.estado IN ('completada', 'abandonada')
                THEN 'abierta' ELSE a.estado END AS estado,
           CASE WHEN a.meeting_id_ultima IS (SELECT id FROM excluida)
                THEN MAX(1, a.menciones - 1) ELSE a.menciones END AS menciones,
           CASE WHEN a.meeting_id_ultima IS (SELECT id FROM excluida)
                THEN a.meeting_id_origen ELSE a.meeting_id_ultima END AS meeting_ref
      FROM actions a
     WHERE a.meeting_id_origen IS NOT (SELECT id FROM excluida)
)
SELECT c.id, c.descripcion, c.estado, c.menciones,
       p.nombre AS persona, m.fecha AS ultima_fecha
  FROM calc c
  LEFT JOIN personas p ON p.id = c.persona_id
  LEFT JOIN meetings m ON m.id = c.meeting_ref
 WHERE c.estado NOT IN ('completada', 'abandonada')
   AND c.meeting_ref IN (SELECT id FROM recientes)
 ORDER BY c.menciones DESC, c.id
"""


def acciones_abiertas(
    conn: sqlite3.Connection,
    ultimas_reuniones: int = 5,
    excluir_meeting_id: int | None = None,
) -> list[sqlite3.Row]:
    """Acciones sin cerrar vistas por ultima vez en las ultimas N reuniones.

    Con `excluir_meeting_id` se ignora esa reunion: sus acciones propias no
    se ofrecen (se borraran al reprocesarla) y las ajenas se devuelven con el
    estado y las menciones que tenian antes de que esa pasada las tocara.
    """
    return conn.execute(
        _SQL_ACCIONES_ABIERTAS, (excluir_meeting_id, ultimas_reuniones)
    ).fetchall()


def aplicar_arrastres(
    conn: sqlite3.Connection, meeting_id: int, arrastres: list[dict]
) -> int:
    """Actualiza estado y menciones de acciones de reuniones anteriores.

    Los `action_id` inexistentes se descartan en silencio: el LLM puede
    inventarselos, y es mas seguro perder una actualizacion que corromper una
    accion ajena.
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
            "SELECT id FROM actions WHERE id = ?", (action_id,)
        ).fetchone()
        if not fila:
            continue
        conn.execute(
            """
            UPDATE actions
               SET estado = ?,
                   menciones = menciones + 1,
                   meeting_id_ultima = ?,
                   cerrada_en = CASE WHEN ? IN ('completada', 'abandonada')
                                     THEN date('now') ELSE NULL END
             WHERE id = ?
            """,
            (estado, meeting_id, estado, action_id),
        )
        aplicados += 1
    return aplicados
