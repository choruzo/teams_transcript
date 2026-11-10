"""
rag.py

Indice semantico de las transcripciones: `datos/indice.db`.

Es la capa de datos de ese fichero, igual que `memoria.py` lo es de
`meetings.db`. **Ningun otro modulo escribe SQL del indice**: ni
`indexar_teams.py`, ni `ask_teams.py`, ni la API.

Por que un fichero aparte y no una tabla mas en `meetings.db`:

  - El indice es un **artefacto derivado**. Se puede borrar y reconstruir
    entero desde el historico; el historico no.
  - `meetings.db` se sigue abriendo con la stdlib pura. El servidor de
    transcripcion, los tests y una API sin `sqlite-vec` instalado no se rompen
    por la existencia de una tabla virtual que no saben leer.
  - No obliga a subir `memoria.ESQUEMA_VERSION`, que es la migracion que la
    fase I6 necesita para `overrides`.

La busqueda es **hibrida**: FTS5 (literal, ya existente en `memoria.py`) y
vectorial (semantica, aqui), fusionadas con Reciprocal Rank Fusion. FTS5 no
hace stemming en espaniol -- "bloqueado" no encuentra "bloquear" -- y ahi es
donde aporta la parte densa; la densa, por su lado, falla justo en lo que la
literal clava: un nombre propio, una sigla, un numero de ticket.

**Todo esto es opcional.** Sin `sqlite-vec`, sin fichero de indice o sin
modelo de embeddings alcanzable, `buscar()` responde solo con FTS5 y lo dice
en el resultado. Es lo que mantiene el chat en pie en el servidor cerrado
mientras el bundle no haya llegado.
"""

import array
import hashlib
import math
import os
import sqlite3
from collections.abc import Callable, Iterable
from datetime import datetime, timezone
from pathlib import Path

import memoria

RAIZ = Path(__file__).resolve().parent
RUTA_POR_DEFECTO = RAIZ / "datos" / "indice.db"
VARIABLE_ENTORNO = "TEAMS_INDICE"

# Version del esquema del indice, independiente de la del historico. Al ser
# reconstruible, "migrar" es siempre reconstruir: no hay datos que preservar.
VERSION_INDICE = 1

# Version del troceador. Si cambia el criterio con el que se parten las
# transcripciones, los fragmentos guardados dejan de corresponderse con los
# que produciria hoy el codigo y hay que rehacerlos aunque el modelo sea el
# mismo. Subir este numero es lo que obliga a ello.
VERSION_TROCEADO = 1

# Version del texto con el que se embeben las acciones (Fase 8). Va aparte de
# `VERSION_TROCEADO` porque son dos artefactos distintos: cambiar como se
# prepara una accion no invalida los fragmentos de transcripcion, ni al reves.
VERSION_ACCIONES = 1

# Similitud minima para proponer dos acciones como posible duplicado.
# **Medida, no elegida** (2026-11-10, servidor, 14 reuniones / 139 acciones /
# 115 abiertas, bge-m3 q8_0 de 1024 dimensiones). Con 0,74 salen 6 parejas: las
# 5 mas claras (eqsim, banda base, PCIs LORCH, PRs en el limbo, testtools) y el
# LDAP de TTC; a 0,75 se pierde esta ultima y a 0,73 empiezan los falsos
# positivos (crear vs probar la tabla de Star Contact, GCS4-1). A 0,70 hay 21
# parejas y la mayoria son ruido. La unica fusion manual de la Fase 7 queda en
# 0,6614: es una parafrasis que el embedding no distingue, y bajaria el umbral
# a un punto inservible (19 sugerencias para 115 acciones), asi que se asume
# como falso negativo conocido. Recalibrar con `--dry-run` si cambia el modelo.
UMBRAL_SUGERENCIA = 0.74

# Cuantas acciones se embeben por llamada. No es un limite del modelo: es para
# no mandar doscientos textos en una sola peticion cuando el indice se
# construye desde cero.
TAMANO_LOTE_ACCIONES = 64

# --------------------------------------------------------------------------
# Troceado
# --------------------------------------------------------------------------
# Los segmentos de una transcripcion son lineas cortas (una reunion de una
# hora ronda los 350), y una linea suelta -- "si, eso" -- no es un fragmento
# recuperable. Se agrupan en ventanas de segmentos consecutivos.

OBJETIVO_CARACTERES = 700
MAXIMO_CARACTERES = 1200

# Un segmento de solape entre fragmentos contiguos: una idea que cae justo en
# la frontera aparece entera en uno de los dos lados.
SOLAPE_SEGMENTOS = 1

# --------------------------------------------------------------------------
# Busqueda
# --------------------------------------------------------------------------

# Cuantos candidatos pide cada rama antes de fusionar. Generoso a proposito:
# la fusion solo puede ordenar lo que le llega, y el recorte de verdad lo hace
# el presupuesto de caracteres.
CANDIDATOS_LEXICOS = 120
CANDIDATOS_DENSOS = 60

# La constante de Reciprocal Rank Fusion. 60 es el valor del articulo
# original y el que usa todo el mundo; mueve el peso hacia los primeros
# puestos sin que un unico primer puesto arrase.
K_RRF = 60

# Lo que limita de verdad no es el numero de fragmentos sino la ventana del
# modelo local. 12.000 caracteres son unos 3.400 tokens de contexto.
PRESUPUESTO_CARACTERES = 12000

# Tope de parametros de un `IN (...)`. SQLite admite mas, pero pasado ese
# tamanio el filtro deja de ser selectivo y sale mas barato post-filtrar.
MAXIMO_IDS_EN_FILTRO = 900

# De lo que hace `indexar_reunion` a la casilla del resumen que lo cuenta.
_CLAVES_RESUMEN = {
    "indexada": "indexadas",
    "reindexada": "reindexadas",
    "al dia": "al dia",
    "omitida": "omitidas",
}


_ESQUEMA = """
CREATE TABLE IF NOT EXISTS meta (
    clave TEXT PRIMARY KEY,
    valor TEXT
);

-- Un fragmento de transcripcion: N segmentos consecutivos de una reunion.
-- Se referencia por `uid` (identidad estable de la reunion, D1) y por el
-- `idx` del primer segmento, que es el ancla `#s-<idx>` que la vista de
-- reunion ya expone: las citas del chat no necesitan nada nuevo.
CREATE TABLE IF NOT EXISTS chunks (
    id INTEGER PRIMARY KEY,
    uid TEXT NOT NULL,
    fecha TEXT NOT NULL,
    tipo TEXT NOT NULL,
    titulo TEXT,
    idx_inicio INTEGER NOT NULL,
    idx_fin INTEGER NOT NULL,
    inicio REAL,
    fin REAL,
    personas TEXT,
    texto TEXT NOT NULL,
    hash TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_chunks_uid_idx ON chunks(uid, idx_inicio);
CREATE INDEX IF NOT EXISTS idx_chunks_fecha ON chunks(fecha);

-- Que reuniones estan indexadas y con que. `huella` resume el contenido de
-- sus segmentos: si cambia, la reunion se reproceso y hay que rehacerla.
CREATE TABLE IF NOT EXISTS reuniones_indexadas (
    uid TEXT PRIMARY KEY,
    huella TEXT NOT NULL,
    fecha TEXT,
    n_chunks INTEGER NOT NULL DEFAULT 0,
    indexado_en TEXT NOT NULL
);

-- Vectores de acciones (Fase 8). Tabla normal con el BLOB y no `vec0`: aqui
-- no se busca el vecino mas cercano de una consulta, se comparan todas las
-- parejas abiertas entre si, y tener el vector junto a su texto evita dos
-- escrituras que se puedan desincronizar. El texto guardado es el que se
-- embebio, para que `--dry-run` pueda ensenarlo.
CREATE TABLE IF NOT EXISTS acciones_indexadas (
    uid TEXT PRIMARY KEY,
    huella TEXT NOT NULL,
    texto TEXT NOT NULL,
    vector BLOB NOT NULL,
    dimensiones INTEGER NOT NULL,
    indexado_en TEXT NOT NULL
);
"""


class ErrorIndice(RuntimeError):
    """Fallo del indice que el usuario puede arreglar (y se le dice como)."""


# --------------------------------------------------------------------------
# Conexion
# --------------------------------------------------------------------------


def ruta_indice(explicita: Path | str | None = None) -> Path:
    """Orden: argumento > TEAMS_INDICE > datos/indice.db."""
    if explicita:
        return Path(explicita)
    variable = os.environ.get(VARIABLE_ENTORNO)
    if variable:
        return Path(variable)
    return RUTA_POR_DEFECTO


def hay_extension() -> bool:
    """Si `sqlite-vec` esta instalado y este Python puede cargar extensiones.

    Las dos cosas pueden faltar por separado: el paquete no esta en el venv de
    Whisper, y un Python compilado sin `--enable-loadable-sqlite-extensions`
    no tiene siquiera el metodo.
    """
    try:
        import sqlite_vec  # noqa: F401
    except ImportError:
        return False
    return hasattr(sqlite3.Connection, "enable_load_extension")


def _cargar_extension(conn: sqlite3.Connection) -> bool:
    if not hay_extension():
        return False
    import sqlite_vec

    try:
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
    except sqlite3.OperationalError:
        return False
    finally:
        try:
            conn.enable_load_extension(False)
        except sqlite3.OperationalError:  # pragma: no cover - depende del build
            pass
    return True


def conectar(
    ruta: Path | str | None = None,
    *,
    solo_lectura: bool = False,
    entre_hilos: bool = False,
) -> sqlite3.Connection:
    """Abre `indice.db`, cargando `sqlite-vec` si se puede.

    Que la extension no cargue **no es un error**: la conexion sirve igual
    para consultar `chunks` y para saber que hay indexado. Solo la busqueda
    densa queda fuera, y `buscar()` lo detecta con `hay_vectores()`.

    `solo_lectura` exige que el fichero exista y aplica `query_only`, el mismo
    criterio (y por la misma razon de WAL) que `memoria.conectar`.
    """
    path = ruta_indice(ruta)
    if solo_lectura and not path.exists():
        raise FileNotFoundError(f"No existe el indice en {path}")
    if not solo_lectura:
        path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(path), check_same_thread=not entre_hilos)
    conn.row_factory = sqlite3.Row
    _cargar_extension(conn)

    if solo_lectura:
        conn.execute("PRAGMA query_only = ON")
        return conn

    conn.execute("PRAGMA journal_mode = WAL")
    try:
        _inicializar(conn)
    except Exception:
        conn.close()
        raise
    return conn


def _inicializar(conn: sqlite3.Connection) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version and version != VERSION_INDICE:
        # El indice no guarda nada que no se pueda recalcular, asi que no hay
        # migraciones: se vacia y se vuelve a construir.
        _vaciar(conn)
    conn.executescript(_ESQUEMA)
    conn.execute(f"PRAGMA user_version = {VERSION_INDICE}")
    conn.commit()


def _vaciar(conn: sqlite3.Connection) -> None:
    for tabla in (
        "chunks_vec",
        "chunks",
        "reuniones_indexadas",
        "acciones_indexadas",
        "meta",
    ):
        try:
            conn.execute(f"DROP TABLE IF EXISTS {tabla}")
        except sqlite3.OperationalError:
            # `chunks_vec` sin la extension cargada: se queda ahi y la
            # reconstruccion la rehara cuando la extension este.
            pass
    conn.commit()


def hay_vectores(conn: sqlite3.Connection) -> bool:
    """Si esta conexion puede hacer busqueda densa **ahora mismo**."""
    fila = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='chunks_vec'"
    ).fetchone()
    if not fila:
        return False
    try:
        conn.execute("SELECT count(*) FROM chunks_vec").fetchone()
    except sqlite3.OperationalError:
        return False  # la tabla existe pero la extension no esta cargada
    return True


# --------------------------------------------------------------------------
# meta
# --------------------------------------------------------------------------


def leer_meta(conn: sqlite3.Connection) -> dict:
    filas = conn.execute("SELECT clave, valor FROM meta").fetchall()
    return {f["clave"]: f["valor"] for f in filas}


def _escribir_meta(conn: sqlite3.Connection, valores: dict) -> None:
    for clave, valor in valores.items():
        conn.execute(
            "INSERT INTO meta(clave, valor) VALUES (?, ?) "
            "ON CONFLICT(clave) DO UPDATE SET valor = excluded.valor",
            (clave, str(valor)),
        )


def _comprobar_compatible(conn: sqlite3.Connection, modelo: str, dimensiones: int):
    """Un indice con vectores de dos modelos distintos devuelve basura.

    Y lo hace **sin dar ningun error**: las distancias se calculan igual, solo
    que no significan nada. Es la forma mas comun de que un indice se pudra en
    silencio, asi que aqui se para en seco y se pide reconstruir.
    """
    meta = leer_meta(conn)
    previo = meta.get("modelo")
    if previo and previo != modelo:
        raise ErrorIndice(
            f"El indice se construyo con el modelo '{previo}' y ahora se pide "
            f"'{modelo}'. Mezclar vectores de dos modelos da resultados sin "
            f"sentido: reconstruyelo con `python indexar_teams.py --reconstruir`."
        )
    dims_previas = meta.get("dimensiones")
    if dims_previas and int(dims_previas) != dimensiones:
        raise ErrorIndice(
            f"El indice tiene vectores de {dims_previas} dimensiones y el "
            f"modelo devuelve {dimensiones}. Reconstruyelo con "
            f"`python indexar_teams.py --reconstruir`."
        )
    troceado = meta.get("version_troceado")
    if troceado and int(troceado) != VERSION_TROCEADO:
        raise ErrorIndice(
            f"El indice se troceo con la version {troceado} y el codigo usa la "
            f"{VERSION_TROCEADO}. Reconstruyelo con "
            f"`python indexar_teams.py --reconstruir`."
        )


# --------------------------------------------------------------------------
# Troceado
# --------------------------------------------------------------------------


def _nombre_hablante(fila) -> str:
    return (fila["persona"] or fila["etiqueta"] or "").strip()


def trocear(segmentos: list, *, uid: str, fecha: str, tipo: str, titulo=None) -> list[dict]:
    """Agrupa segmentos consecutivos en fragmentos indexables.

    Se cierra un fragmento cuando anadir el siguiente segmento pasaria del
    maximo, o cuando ya se ha alcanzado el objetivo **y** cambia el hablante:
    cortar en un cambio de turno produce fragmentos que se leen solos.
    """
    fragmentos: list[dict] = []
    actual: list = []
    largo = 0

    def cerrar() -> list:
        nonlocal largo
        if not actual:
            return []
        fragmentos.append(_fragmento(actual, uid=uid, fecha=fecha, tipo=tipo, titulo=titulo))
        # El solape solo tiene sentido si queda algo detras: con un unico
        # segmento, arrastrarlo daria un fragmento con el mismo `idx_inicio`
        # que el anterior y chocaria con el indice UNIQUE.
        cola = actual[-SOLAPE_SEGMENTOS:] if len(actual) > SOLAPE_SEGMENTOS else []
        largo = sum(len(s["texto"] or "") for s in cola)
        return list(cola)

    anterior_hablante = None
    for segmento in segmentos:
        texto = (segmento["texto"] or "").strip()
        if not texto:
            continue
        hablante = _nombre_hablante(segmento)
        pasa_del_maximo = actual and largo + len(texto) > MAXIMO_CARACTERES
        turno_nuevo = (
            actual
            and largo >= OBJETIVO_CARACTERES
            and hablante != anterior_hablante
        )
        if pasa_del_maximo or turno_nuevo:
            actual = cerrar()
        actual.append(segmento)
        largo += len(texto)
        anterior_hablante = hablante

    if actual:
        fragmentos.append(_fragmento(actual, uid=uid, fecha=fecha, tipo=tipo, titulo=titulo))
    return fragmentos


def _fragmento(segmentos: list, *, uid: str, fecha: str, tipo: str, titulo) -> dict:
    lineas = []
    personas = []
    for segmento in segmentos:
        quien = _nombre_hablante(segmento)
        if quien and quien not in personas:
            personas.append(quien)
        lineas.append(f"{quien}: {segmento['texto']}" if quien else segmento["texto"])
    texto = "\n".join(lineas)
    return {
        "uid": uid,
        "fecha": fecha,
        "tipo": tipo,
        "titulo": titulo,
        "idx_inicio": segmentos[0]["idx"],
        "idx_fin": segmentos[-1]["idx"],
        "inicio": segmentos[0]["inicio"],
        "fin": segmentos[-1]["fin"],
        "personas": " | ".join(personas),
        "texto": texto,
        "hash": hashlib.sha1(texto.encode("utf-8")).hexdigest(),
    }


def texto_a_embeber(fragmento: dict) -> str:
    """El fragmento con una cabecera que lo situa.

    Sin ella, "lo dejamos para el jueves" es un vector que no dice de que
    reunion es ni de quien. La cabecera va **solo** al modelo: lo que se
    guarda y se ensenia es el texto literal.
    """
    cabecera = f"{fragmento['tipo'].capitalize()} del {fragmento['fecha']}"
    if fragmento.get("personas"):
        cabecera += f" — {fragmento['personas']}"
    return f"{cabecera}:\n{fragmento['texto']}"


# --------------------------------------------------------------------------
# Indexacion
# --------------------------------------------------------------------------


def _todos_los_segmentos(conn_hist: sqlite3.Connection, meeting_id: int) -> list:
    """Todos, no la primera pagina: `segmentos_de_reunion` esta paginada."""
    filas: list = []
    pagina = 500
    while True:
        trozo = memoria.segmentos_de_reunion(
            conn_hist, meeting_id, limite=pagina, desplazamiento=len(filas)
        )
        filas.extend(trozo)
        if len(trozo) < pagina:
            return filas


def huella_reunion(segmentos: list) -> str:
    """Resume el contenido de una transcripcion.

    Reprocesar una reunion borra y reinserta sus segmentos conservando el
    `uid`; comparar la huella es lo que detecta que hay que reindexarla.
    """
    h = hashlib.sha1()
    h.update(str(len(segmentos)).encode())
    for segmento in segmentos:
        h.update(str(segmento["idx"]).encode())
        h.update((segmento["texto"] or "").encode("utf-8"))
        h.update((_nombre_hablante(segmento) or "").encode("utf-8"))
    return h.hexdigest()


def _asegurar_tabla_vectores(conn: sqlite3.Connection, dimensiones: int) -> bool:
    """Crea `chunks_vec` la primera vez, cuando ya se sabe la dimension.

    No puede ir en `_ESQUEMA`: el numero de dimensiones lo decide el modelo, y
    hasta que no responde no se conoce.
    """
    if not hay_vectores(conn):
        try:
            conn.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS chunks_vec USING vec0("
                "chunk_id INTEGER PRIMARY KEY, "
                f"embedding float[{dimensiones}] distance_metric=cosine)"
            )
        except sqlite3.OperationalError:
            return False
    return True


def _blob(vector: Iterable[float]) -> bytes:
    return array.array("f", [float(v) for v in vector]).tobytes()


def _borrar_reunion(conn: sqlite3.Connection, uid: str) -> None:
    ids = [f["id"] for f in conn.execute("SELECT id FROM chunks WHERE uid = ?", (uid,))]
    if ids and hay_vectores(conn):
        conn.executemany("DELETE FROM chunks_vec WHERE chunk_id = ?", [(i,) for i in ids])
    conn.execute("DELETE FROM chunks WHERE uid = ?", (uid,))
    conn.execute("DELETE FROM reuniones_indexadas WHERE uid = ?", (uid,))


def indexar_reunion(
    conn_idx: sqlite3.Connection,
    conn_hist: sqlite3.Connection,
    fila_reunion,
    *,
    embebedor: Callable[[list[str]], list[list[float]]],
    modelo: str,
    forzar: bool = False,
) -> dict:
    """Indexa (o reindexa) una reunion. Devuelve que ha hecho y por que."""
    uid = fila_reunion["uid"]
    if not uid:
        return {"uid": None, "accion": "omitida", "motivo": "la reunion no tiene uid"}

    segmentos = _todos_los_segmentos(conn_hist, fila_reunion["id"])
    if not segmentos:
        return {"uid": uid, "accion": "omitida", "motivo": "sin segmentos", "chunks": 0}

    huella = huella_reunion(segmentos)
    previa = conn_idx.execute(
        "SELECT huella FROM reuniones_indexadas WHERE uid = ?", (uid,)
    ).fetchone()
    if previa and previa["huella"] == huella and not forzar:
        return {"uid": uid, "accion": "al dia", "chunks": 0}

    fragmentos = trocear(
        segmentos,
        uid=uid,
        fecha=fila_reunion["fecha"],
        tipo=fila_reunion["tipo"],
        titulo=fila_reunion["titulo"],
    )
    vectores = embebedor([texto_a_embeber(f) for f in fragmentos]) if fragmentos else []
    if vectores and len(vectores) != len(fragmentos):
        raise ErrorIndice(
            f"{len(vectores)} vectores para {len(fragmentos)} fragmentos de {uid}."
        )

    dimensiones = len(vectores[0]) if vectores else 0
    if dimensiones:
        _comprobar_compatible(conn_idx, modelo, dimensiones)

    _borrar_reunion(conn_idx, uid)
    con_vectores = bool(dimensiones) and _asegurar_tabla_vectores(conn_idx, dimensiones)

    for posicion, fragmento in enumerate(fragmentos):
        cursor = conn_idx.execute(
            "INSERT INTO chunks(uid, fecha, tipo, titulo, idx_inicio, idx_fin, "
            "inicio, fin, personas, texto, hash) "
            "VALUES (:uid, :fecha, :tipo, :titulo, :idx_inicio, :idx_fin, "
            ":inicio, :fin, :personas, :texto, :hash)",
            fragmento,
        )
        if con_vectores:
            conn_idx.execute(
                "INSERT INTO chunks_vec(chunk_id, embedding) VALUES (?, ?)",
                (cursor.lastrowid, _blob(vectores[posicion])),
            )

    conn_idx.execute(
        "INSERT INTO reuniones_indexadas(uid, huella, fecha, n_chunks, indexado_en) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            uid,
            huella,
            fila_reunion["fecha"],
            len(fragmentos),
            datetime.now(timezone.utc).isoformat(timespec="seconds"),
        ),
    )
    if dimensiones:
        _escribir_meta(
            conn_idx,
            {
                "modelo": modelo,
                "dimensiones": dimensiones,
                "version_troceado": VERSION_TROCEADO,
            },
        )
    conn_idx.commit()
    return {
        "uid": uid,
        "accion": "reindexada" if previa else "indexada",
        "chunks": len(fragmentos),
        "densa": con_vectores,
    }


def indexar(
    conn_idx: sqlite3.Connection,
    conn_hist: sqlite3.Connection,
    *,
    embebedor: Callable[[list[str]], list[list[float]]],
    modelo: str,
    uids: list[str] | None = None,
    reconstruir: bool = False,
    progreso: Callable[[dict], None] | None = None,
) -> dict:
    """Recorre las reuniones del historico y pone el indice al dia."""
    if reconstruir:
        _vaciar(conn_idx)
        conn_idx.executescript(_ESQUEMA)
        conn_idx.commit()

    if uids:
        filas = [f for f in (memoria.reunion_por_uid(conn_hist, u) for u in uids) if f]
        faltan = set(uids) - {f["uid"] for f in filas}
        if faltan:
            raise ErrorIndice(f"No hay reunion con uid: {', '.join(sorted(faltan))}")
    else:
        filas = conn_hist.execute(
            "SELECT * FROM meetings WHERE uid IS NOT NULL ORDER BY fecha, id"
        ).fetchall()

    resumen = {"indexadas": 0, "reindexadas": 0, "al dia": 0, "omitidas": 0, "chunks": 0}
    for fila in filas:
        resultado = indexar_reunion(
            conn_idx, conn_hist, fila, embebedor=embebedor, modelo=modelo,
            forzar=reconstruir,
        )
        clave = _CLAVES_RESUMEN[resultado["accion"]]
        resumen[clave] = resumen.get(clave, 0) + 1
        resumen["chunks"] += resultado.get("chunks", 0)
        if progreso:
            progreso(resultado)

    # Sin vectores el indice sigue siendo util (agrupa los segmentos en
    # fragmentos), pero conviene que quien mire `--info` lo sepa.
    resumen["densa"] = hay_vectores(conn_idx)
    return resumen


def limpiar_huerfanas(conn_idx: sqlite3.Connection, conn_hist: sqlite3.Connection) -> int:
    """Borra del indice las reuniones que ya no estan en el historico."""
    vivos = {f["uid"] for f in conn_hist.execute("SELECT uid FROM meetings")}
    sobran = [
        f["uid"]
        for f in conn_idx.execute("SELECT uid FROM reuniones_indexadas")
        if f["uid"] not in vivos
    ]
    for uid in sobran:
        _borrar_reunion(conn_idx, uid)
    if sobran:
        conn_idx.commit()
    return len(sobran)


def info(conn_idx: sqlite3.Connection) -> dict:
    """Estado del indice: para `--info` y para `/api/chat/estado`."""
    meta = leer_meta(conn_idx)
    fila = conn_idx.execute(
        "SELECT count(*) AS chunks, count(DISTINCT uid) AS reuniones FROM chunks"
    ).fetchone()
    ultima = conn_idx.execute(
        "SELECT max(indexado_en) AS cuando FROM reuniones_indexadas"
    ).fetchone()
    try:
        acciones = conn_idx.execute(
            "SELECT count(*) AS n FROM acciones_indexadas"
        ).fetchone()["n"]
    except sqlite3.OperationalError:
        # Indice anterior a la Fase 8, abierto en solo lectura (la API): no se
        # puede crear la tabla, y su ausencia no puede tumbar /api/chat/estado.
        acciones = 0
    return {
        "chunks": fila["chunks"],
        "reuniones": fila["reuniones"],
        "acciones": acciones,
        "modelo": meta.get("modelo"),
        "modelo_acciones": meta.get("acciones_modelo"),
        "dimensiones": int(meta["dimensiones"]) if meta.get("dimensiones") else None,
        "densa": hay_vectores(conn_idx),
        "extension": hay_extension(),
        "indexado_en": ultima["cuando"] if ultima else None,
    }


# --------------------------------------------------------------------------
# Busqueda hibrida
# --------------------------------------------------------------------------


def _filtros_chunks(filtros: dict) -> tuple[str, list]:
    where = []
    valores: list = []
    if filtros.get("desde"):
        where.append("fecha >= ?")
        valores.append(filtros["desde"])
    if filtros.get("hasta"):
        where.append("fecha <= ?")
        valores.append(filtros["hasta"])
    if filtros.get("tipo"):
        where.append("tipo = ?")
        valores.append(filtros["tipo"])
    if filtros.get("uid"):
        where.append("uid = ?")
        valores.append(filtros["uid"])
    if filtros.get("persona"):
        where.append("sin_acentos(personas) LIKE '%' || sin_acentos(?) || '%'")
        valores.append(filtros["persona"])
    return (" WHERE " + " AND ".join(where)) if where else "", valores


def _sin_acentos_disponible(conn: sqlite3.Connection) -> None:
    """`memoria.conectar` la registra; la del indice la registra aqui."""
    conn.create_function("sin_acentos", 1, memoria.sin_acentos, deterministic=True)


def _candidatos_filtrados(conn_idx: sqlite3.Connection, filtros: dict) -> list[int] | None:
    """Ids que pasan el filtro, o None si el filtro no acota nada.

    None significa "no restrinjas la busqueda densa"; una lista vacia
    significa "no hay nada que buscar", que es muy distinto.
    """
    where, valores = _filtros_chunks(filtros)
    if not where:
        return None
    filas = conn_idx.execute(f"SELECT id FROM chunks{where}", valores).fetchall()
    return [f["id"] for f in filas]


def _rama_densa(
    conn_idx: sqlite3.Connection,
    vector: list[float],
    filtros: dict,
    limite: int,
) -> list[int]:
    if not hay_vectores(conn_idx):
        return []
    ids = _candidatos_filtrados(conn_idx, filtros)
    if ids is not None and not ids:
        return []
    sql = "SELECT chunk_id FROM chunks_vec WHERE embedding MATCH ? AND k = ?"
    valores: list = [_blob(vector), limite]
    acotado = ids is not None and len(ids) <= MAXIMO_IDS_EN_FILTRO
    if acotado:
        sql += f" AND chunk_id IN ({','.join('?' * len(ids))})"
        valores.extend(ids)
    filas = conn_idx.execute(sql, valores).fetchall()
    encontrados = [f["chunk_id"] for f in filas]
    if not acotado and ids is not None:
        # Demasiados ids para meterlos en un IN: se post-filtra. Con un filtro
        # tan poco selectivo la diferencia de recall es despreciable.
        permitidos = set(ids)
        encontrados = [i for i in encontrados if i in permitidos]
    return encontrados


def _rama_lexica(
    conn_hist: sqlite3.Connection,
    conn_idx: sqlite3.Connection,
    texto: str,
    filtros: dict,
    limite: int,
) -> list[int]:
    """FTS5 sobre `segments`, traducido a ids de fragmento.

    Cada segmento acertado cae dentro de un fragmento; el orden de bm25 se
    conserva, y un fragmento con varios aciertos entra una sola vez, en la
    posicion del mejor.
    """
    consulta = memoria.consulta_fts(texto)
    if not consulta:
        return []
    filas = memoria.buscar_segmentos(
        conn_hist,
        consulta,
        orden="relevancia",
        limite=limite,
        desde=filtros.get("desde"),
        hasta=filtros.get("hasta"),
        tipo=filtros.get("tipo"),
        uid=filtros.get("uid"),
    )
    persona = memoria.sin_acentos(filtros.get("persona")) if filtros.get("persona") else None
    ids: list[int] = []
    vistos: set[int] = set()
    for fila in filas:
        if persona and persona not in memoria.sin_acentos(fila["persona"]):
            continue
        encontrado = conn_idx.execute(
            "SELECT id FROM chunks WHERE uid = ? AND idx_inicio <= ? AND idx_fin >= ? "
            "ORDER BY idx_inicio LIMIT 1",
            (fila["uid"], fila["idx"], fila["idx"]),
        ).fetchone()
        if encontrado and encontrado["id"] not in vistos:
            vistos.add(encontrado["id"])
            ids.append(encontrado["id"])
    return ids


def _rrf(listas: dict[str, list[int]]) -> list[tuple[int, float, list[str]]]:
    """Reciprocal Rank Fusion.

    Suma 1/(K + puesto) de cada lista donde aparece el id. No compara las
    puntuaciones -- bm25 y distancia coseno viven en escalas distintas y
    normalizarlas es inventarse una equivalencia --, solo los puestos.
    """
    puntos: dict[int, float] = {}
    origen: dict[int, list[str]] = {}
    for nombre, ids in listas.items():
        for puesto, identificador in enumerate(ids):
            puntos[identificador] = puntos.get(identificador, 0.0) + 1.0 / (K_RRF + puesto + 1)
            origen.setdefault(identificador, []).append(nombre)
    ordenados = sorted(puntos.items(), key=lambda par: (-par[1], par[0]))
    return [(i, p, origen[i]) for i, p in ordenados]


def buscar(
    conn_hist: sqlite3.Connection,
    conn_idx: sqlite3.Connection | None,
    *,
    texto: str | None = None,
    semantica: str | None = None,
    embebedor: Callable[[list[str]], list[list[float]]] | None = None,
    filtros: dict | None = None,
    limite: int = 12,
    presupuesto: int = PRESUPUESTO_CARACTERES,
) -> dict:
    """Fragmentos relevantes para una pregunta.

    Devuelve `{"fragmentos": [...], "modo": "hibrida"|"lexica"|"vacia",
    "aviso": str|None}`. El modo no es decorativo: la web lo ensenia, porque
    responder solo con busqueda literal da respuestas peores y quien pregunta
    tiene derecho a saberlo.
    """
    filtros = dict(filtros or {})
    texto = (texto or "").strip()
    semantica = (semantica or texto).strip()
    avisos: list[str] = []

    if conn_idx is None:
        return _solo_lexica(conn_hist, texto, filtros, limite, presupuesto)

    _sin_acentos_disponible(conn_idx)
    listas: dict[str, list[int]] = {}

    if texto:
        listas["lexica"] = _rama_lexica(
            conn_hist, conn_idx, texto, filtros, CANDIDATOS_LEXICOS
        )

    if embebedor and semantica and hay_vectores(conn_idx):
        try:
            vector = embebedor([semantica])[0]
            listas["densa"] = _rama_densa(conn_idx, vector, filtros, CANDIDATOS_DENSOS)
        except Exception as exc:  # noqa: BLE001 - degradar, no caerse
            avisos.append(f"La busqueda semantica no esta disponible: {exc}")
    elif not hay_vectores(conn_idx):
        avisos.append(
            "El indice semantico no esta construido: se responde solo con "
            "busqueda literal."
        )

    fusion = _rrf(listas)
    fragmentos = _materializar(conn_idx, fusion, limite, presupuesto)
    modo = "hibrida" if "densa" in listas and listas.get("densa") else "lexica"
    if not fragmentos:
        modo = "vacia"
    return {
        "fragmentos": fragmentos,
        "modo": modo,
        "aviso": " ".join(avisos) or None,
    }


def _solo_lexica(conn_hist, texto, filtros, limite, presupuesto) -> dict:
    """Sin indice: se devuelven los segmentos de FTS5 tal cual.

    Un segmento suelto es peor contexto que un fragmento, pero es lo que hay
    cuando `indice.db` no existe, y es infinitamente mejor que un error.
    """
    consulta = memoria.consulta_fts(texto)
    if not consulta:
        return {"fragmentos": [], "modo": "vacia", "aviso": None}
    filas = memoria.buscar_segmentos(
        conn_hist,
        consulta,
        orden="relevancia",
        limite=limite,
        desde=filtros.get("desde"),
        hasta=filtros.get("hasta"),
        tipo=filtros.get("tipo"),
        uid=filtros.get("uid"),
    )
    fragmentos = []
    gastado = 0
    for fila in filas:
        texto_fila = fila["fragmento"].replace(memoria.MARCA_INICIO, "").replace(
            memoria.MARCA_FIN, ""
        )
        if gastado + len(texto_fila) > presupuesto:
            break
        gastado += len(texto_fila)
        fragmentos.append(
            {
                "uid": fila["uid"],
                "fecha": fila["fecha"],
                "titulo": fila["titulo"],
                "tipo": fila["tipo"],
                "idx_inicio": fila["idx"],
                "idx_fin": fila["idx"],
                "inicio": fila["inicio"],
                "personas": fila["persona"] or fila["etiqueta"] or "",
                "texto": texto_fila,
                "origen": ["lexica"],
            }
        )
    return {
        "fragmentos": fragmentos,
        "modo": "lexica" if fragmentos else "vacia",
        "aviso": "Sin indice semantico: se responde solo con busqueda literal.",
    }


def _materializar(conn_idx, fusion, limite: int, presupuesto: int) -> list[dict]:
    fragmentos = []
    gastado = 0
    for identificador, puntos, origen in fusion:
        if len(fragmentos) >= limite:
            break
        fila = conn_idx.execute(
            "SELECT uid, fecha, titulo, tipo, idx_inicio, idx_fin, inicio, fin, "
            "personas, texto FROM chunks WHERE id = ?",
            (identificador,),
        ).fetchone()
        if not fila:
            continue
        if gastado + len(fila["texto"]) > presupuesto and fragmentos:
            break
        gastado += len(fila["texto"])
        fragmento = dict(fila)
        fragmento["puntos"] = round(puntos, 6)
        fragmento["origen"] = origen
        fragmentos.append(fragmento)
    return fragmentos


# --------------------------------------------------------------------------
# Acciones: vectores y sugerencias de duplicados (Fase 8)
# --------------------------------------------------------------------------
# El motor **nunca fusiona solo**: propone parejas y la decision es humana. El
# vector se calcula con la descripcion, sin la reunion ni la fecha, porque el
# duplicado que se busca nace en reuniones distintas: meter la fecha en el
# texto lo volveria artificialmente dissimilar. El responsable tampoco entra:
# un `Pedro / Jose Javier` o un `SPEAKER_02` mal resuelto esconderia la pareja
# real. Las reglas que si dependen de esos campos se aplican al filtrar.


def texto_a_embeber_accion(accion: dict) -> str:
    """El texto que se embebe de una accion: su descripcion, normalizada."""
    return " ".join(str(accion.get("descripcion") or "").split())


def _huella_accion(texto: str) -> str:
    return hashlib.sha1(texto.encode("utf-8")).hexdigest()


def _lotes(lista: list, tamano: int):
    for inicio in range(0, len(lista), tamano):
        yield lista[inicio : inicio + tamano]


def _embeber_lotes(textos: list[str], embebedor: Callable[[list[str]], list[list[float]]]):
    """Embebe por lotes y devuelve tambien cuantas llamadas ha hecho."""
    vectores: list[list[float]] = []
    llamadas = 0
    for lote in _lotes(textos, TAMANO_LOTE_ACCIONES):
        respuesta = embebedor(lote)
        if len(respuesta) != len(lote):
            raise ErrorIndice(
                f"{len(respuesta)} vectores para {len(lote)} acciones."
            )
        vectores.extend(respuesta)
        llamadas += 1
    return vectores, llamadas


def _comprobar_compatible_acciones(
    conn: sqlite3.Connection, modelo: str, dimensiones: int
) -> None:
    meta = leer_meta(conn)
    previo = meta.get("acciones_modelo")
    if previo and previo != modelo:
        raise ErrorIndice(
            f"Los vectores de acciones se construyeron con el modelo '{previo}' "
            f"y ahora se pide '{modelo}'. Reconstruyelos con "
            "`python indexar_teams.py --acciones --reconstruir`."
        )
    dims_previas = meta.get("acciones_dimensiones")
    if dims_previas and int(dims_previas) != dimensiones:
        raise ErrorIndice(
            f"Los vectores de acciones tienen {dims_previas} dimensiones y el "
            f"modelo devuelve {dimensiones}. Reconstruyelos con "
            "`python indexar_teams.py --acciones --reconstruir`."
        )
    version = meta.get("version_acciones")
    if version and int(version) != VERSION_ACCIONES:
        raise ErrorIndice(
            f"Los vectores de acciones usan la version {version} y el codigo la "
            f"{VERSION_ACCIONES}. Reconstruyelos con "
            "`python indexar_teams.py --acciones --reconstruir`."
        )


def indexar_acciones(
    conn_idx: sqlite3.Connection,
    conn_hist: sqlite3.Connection,
    *,
    embebedor: Callable[[list[str]], list[list[float]]],
    modelo: str,
    forzar: bool = False,
) -> dict:
    """Pone al dia los vectores de las acciones. Incremental por huella.

    Se indexan **todas** las acciones con uid, incluidas cerradas, descartadas
    y absorbidas: las absorbidas son la verdad de referencia para calibrar el
    umbral. Las sugerencias no las miran; eso lo decide `acciones_para_sugerir`.
    """
    acciones = memoria.acciones_para_indice(conn_hist)
    textos: dict[str, str] = {}
    huellas: dict[str, str] = {}
    for accion in acciones:
        texto = texto_a_embeber_accion(accion)
        if texto:
            textos[accion["uid"]] = texto
            huellas[accion["uid"]] = _huella_accion(texto)

    existentes = {
        fila["uid"]: fila["huella"]
        for fila in conn_idx.execute("SELECT uid, huella FROM acciones_indexadas")
    }
    # Se comprueba siempre, no solo cuando hay algo que embeber: un cambio de
    # `VERSION_ACCIONES` obliga a rehacerlos aunque ninguna descripcion haya
    # cambiado. Con `forzar` (reconstruccion) se rehacen y se pisa la meta.
    if not forzar:
        meta = leer_meta(conn_idx)
        previo = meta.get("acciones_modelo")
        if previo and previo != modelo:
            raise ErrorIndice(
                f"Los vectores de acciones se construyeron con el modelo "
                f"'{previo}' y ahora se pide '{modelo}'. Reconstruyelos con "
                "`python indexar_teams.py --acciones --reconstruir`."
            )
        version = meta.get("version_acciones")
        if version and int(version) != VERSION_ACCIONES:
            raise ErrorIndice(
                f"Los vectores de acciones usan la version {version} y el codigo "
                f"la {VERSION_ACCIONES}. Reconstruyelos con "
                "`python indexar_teams.py --acciones --reconstruir`."
            )

    por_embeber = [
        uid
        for uid in textos
        if forzar or uid not in existentes or existentes[uid] != huellas[uid]
    ]
    nuevas = [uid for uid in por_embeber if uid not in existentes]
    actualizadas = [uid for uid in por_embeber if uid in existentes]
    al_dia = len(textos) - len(por_embeber)
    retiradas = [uid for uid in existentes if uid not in textos]

    vectores, llamadas = {}, 0
    if por_embeber:
        lista, llamadas = _embeber_lotes(
            [textos[uid] for uid in por_embeber], embebedor
        )
        if not lista or not lista[0]:
            raise ErrorIndice("El modelo de embeddings devolvio vectores vacios.")
        dimensiones = len(lista[0])
        if any(len(vector) != dimensiones for vector in lista):
            raise ErrorIndice(
                "El modelo devolvio vectores de dimensiones distintas en la misma "
                "llamada."
            )
        if not forzar:
            _comprobar_compatible_acciones(conn_idx, modelo, dimensiones)
        vectores = dict(zip(por_embeber, lista))

        with conn_idx:
            for uid, vector in vectores.items():
                conn_idx.execute(
                    "INSERT INTO acciones_indexadas "
                    "(uid, huella, texto, vector, dimensiones, indexado_en) "
                    "VALUES (?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(uid) DO UPDATE SET "
                    "huella = excluded.huella, texto = excluded.texto, "
                    "vector = excluded.vector, dimensiones = excluded.dimensiones, "
                    "indexado_en = excluded.indexado_en",
                    (
                        uid,
                        huellas[uid],
                        textos[uid],
                        _blob(vector),
                        dimensiones,
                        datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    ),
                )
            conn_idx.executemany(
                "DELETE FROM acciones_indexadas WHERE uid = ?",
                [(uid,) for uid in retiradas],
            )
            _escribir_meta(
                conn_idx,
                {
                    "acciones_modelo": modelo,
                    "acciones_dimensiones": dimensiones,
                    "version_acciones": VERSION_ACCIONES,
                },
            )
    elif retiradas:
        with conn_idx:
            conn_idx.executemany(
                "DELETE FROM acciones_indexadas WHERE uid = ?",
                [(uid,) for uid in retiradas],
            )

    return {
        "acciones": len(textos),
        "nuevas": len(nuevas),
        "actualizadas": len(actualizadas),
        "al dia": al_dia,
        "retiradas": len(retiradas),
        "llamadas": llamadas,
    }


def hay_vectores_acciones(conn_idx: sqlite3.Connection) -> bool:
    """Si el indice tiene algun vector de accion. Un indice viejo no lo tiene."""
    try:
        return (
            conn_idx.execute("SELECT 1 FROM acciones_indexadas LIMIT 1").fetchone()
            is not None
        )
    except sqlite3.OperationalError:
        return False


def cargar_vectores(conn_idx: sqlite3.Connection) -> dict[str, list[float]]:
    """Los vectores guardados, ya decodificados. Tolerante a filas corruptas."""
    vectores: dict[str, list[float]] = {}
    try:
        filas = conn_idx.execute(
            "SELECT uid, vector, dimensiones FROM acciones_indexadas"
        ).fetchall()
    except sqlite3.OperationalError:
        # Indice anterior a la Fase 8 sin permiso de escritura: sin vectores.
        return vectores
    for fila in filas:
        vector = array.array("f")
        vector.frombytes(fila["vector"])
        if len(vector) == fila["dimensiones"]:
            vectores[fila["uid"]] = list(vector)
    return vectores


def _coseno(a: list[float], b: list[float]) -> float:
    if len(a) != len(b) or not a:
        return 0.0
    norma_a = math.sqrt(math.fsum(x * x for x in a))
    norma_b = math.sqrt(math.fsum(x * x for x in b))
    if not norma_a or not norma_b:
        return 0.0
    return math.fsum(x * y for x, y in zip(a, b)) / (norma_a * norma_b)


def _sugerible(accion: dict) -> bool:
    """Lo que el motor de parejas puede proponer: abierta y no absorbida."""
    return (
        not accion.get("absorbida")
        and accion.get("estado") not in memoria.ESTADOS_CERRADOS
    )


def _persona_normalizada(valor) -> str:
    return memoria.sin_acentos(str(valor or "")).strip()


def _misma_reunion_distinto_responsable(a: dict, b: dict) -> bool:
    """El LLM ya separo dos acciones de la misma reunion con distinto dueno."""
    return (
        a.get("meeting_id") is not None
        and a.get("meeting_id") == b.get("meeting_id")
        and _persona_normalizada(a.get("persona"))
        != _persona_normalizada(b.get("persona"))
    )


def _pares_con_similitud(
    acciones: list[dict], vectores: dict[str, list[float]]
) -> list[dict]:
    """Todas las parejas con vector, sin filtrar. Ordenadas por similitud."""
    vistas: dict[str, dict] = {}
    for accion in acciones:
        uid = accion.get("uid")
        if uid and uid in vectores and uid not in vistas:
            vistas[uid] = accion
    lista = list(vistas.values())
    pares: list[dict] = []
    for i, a in enumerate(lista):
        for b in lista[i + 1 :]:
            similitud = _coseno(vectores[a["uid"]], vectores[b["uid"]])
            pares.append({"a": a, "b": b, "similitud": similitud})
    pares.sort(key=lambda par: (-par["similitud"], par["a"]["uid"], par["b"]["uid"]))
    return pares


def _fila_sugerencia(par: dict) -> dict:
    a, b = par["a"], par["b"]
    return {
        "a_uid": a["uid"],
        "a_descripcion": a.get("descripcion"),
        "a_persona": a.get("persona") or None,
        "a_estado": a.get("estado"),
        "a_fecha": a.get("fecha"),
        "a_meeting_uid": a.get("meeting_uid"),
        "b_uid": b["uid"],
        "b_descripcion": b.get("descripcion"),
        "b_persona": b.get("persona") or None,
        "b_estado": b.get("estado"),
        "b_fecha": b.get("fecha"),
        "b_meeting_uid": b.get("meeting_uid"),
        "similitud": round(par["similitud"], 4),
    }


def parejas_similares(
    conn_idx: sqlite3.Connection,
    acciones: list[dict],
    *,
    rechazadas: set[tuple[str, str]] | None = None,
    umbral: float = UMBRAL_SUGERENCIA,
    mejores: bool = True,
) -> list[dict]:
    """Posibles duplicados entre las acciones dadas, de mas a menos parecido.

    `acciones` viene de `memoria.acciones_para_sugerir` (vigentes y abiertas).
    Se descartan las parejas ya rechazadas por un humano y las de una misma
    reunion con distinto responsable. Con `mejores`, cada accion aparece como
    mucho en una pareja: proponerle a la misma accion tres candidatas solo
    llena la pantalla.
    """
    rechazadas = {tuple(sorted(par)) for par in (rechazadas or set())}
    pares = _pares_con_similitud(acciones, cargar_vectores(conn_idx))

    candidatas = []
    for par in pares:
        if par["similitud"] < umbral:
            continue
        a_uid, b_uid = par["a"]["uid"], par["b"]["uid"]
        if tuple(sorted((a_uid, b_uid))) in rechazadas:
            continue
        if _misma_reunion_distinto_responsable(par["a"], par["b"]):
            continue
        candidatas.append(par)

    if mejores:
        usados: set[str] = set()
        elegidas = []
        for par in candidatas:
            if par["a"]["uid"] in usados or par["b"]["uid"] in usados:
                continue
            usados.update((par["a"]["uid"], par["b"]["uid"]))
            elegidas.append(par)
        candidatas = elegidas

    return [_fila_sugerencia(par) for par in candidatas]


def calibrar_acciones(
    acciones: list[dict],
    referencias: list[dict],
    *,
    embebedor: Callable[[list[str]], list[list[float]]],
    umbral: float = UMBRAL_SUGERENCIA,
    minimo: float = 0.5,
    rechazadas: set[tuple[str, str]] | None = None,
) -> dict:
    """Calcula las parejas sin escribir nada, para medir el umbral.

    Devuelve dos listas: `parejas` (todas las que superan `minimo`, con la
    marca de si se propondrian) y `referencias` (las fusiones humanas de la
    Fase 7, aunque queden por debajo de `minimo`). Sin las segundas no se
    pueden ver los falsos negativos.
    """
    textos, por_uid = [], {}
    for accion in acciones:
        texto = texto_a_embeber_accion(accion)
        if texto and accion.get("uid") not in por_uid:
            por_uid[accion["uid"]] = accion
            textos.append(texto)
    if not textos:
        return {"parejas": [], "referencias": [], "llamadas": 0, "dimensiones": 0}

    vectores, llamadas = _embeber_lotes(textos, embebedor)
    if not vectores or not vectores[0]:
        raise ErrorIndice("El modelo de embeddings devolvio vectores vacios.")
    vector_por_uid = {
        uid: vector for uid, vector in zip(por_uid, vectores)
    }
    rechazadas = {tuple(sorted(par)) for par in (rechazadas or set())}

    def describir(par: dict, *, referencia: bool) -> dict:
        fila = _fila_sugerencia(par)
        motivos = []
        if tuple(sorted((fila["a_uid"], fila["b_uid"]))) in rechazadas:
            motivos.append("rechazada")
        if _misma_reunion_distinto_responsable(par["a"], par["b"]):
            motivos.append("misma reunion con distinto responsable")
        if not (_sugerible(par["a"]) and _sugerible(par["b"])) and not referencia:
            motivos.append("cerrada o absorbida")
        fila.update(
            {
                "propuesta": par["similitud"] >= umbral and not motivos,
                "motivo": "; ".join(motivos) or None,
                "referencia": referencia,
                "sugerida": False,
            }
        )
        return fila

    parejas = [
        describir(par, referencia=False)
        for par in _pares_con_similitud(
            list(por_uid.values()), vector_por_uid
        )
        if par["similitud"] >= minimo
        and _sugerible(par["a"])
        and _sugerible(par["b"])
    ]

    # El motor real aplica "una sugerencia por accion"; la calibracion tiene que
    # ensenar el mismo numero o el umbral se mediria contra una lista que nadie
    # va a ver. `propuesta` sigue siendo por pareja; `sugerida`, la que saldria.
    usados: set[str] = set()
    for fila in parejas:
        if not fila["propuesta"]:
            continue
        if fila["a_uid"] in usados or fila["b_uid"] in usados:
            continue
        usados.update((fila["a_uid"], fila["b_uid"]))
        fila["sugerida"] = True

    referencias_out = []
    for referencia in referencias:
        a = por_uid.get(referencia.get("principal_uid"))
        b = por_uid.get(referencia.get("duplicada_uid"))
        if not a or not b:
            continue
        similitud = _coseno(vector_por_uid[a["uid"]], vector_por_uid[b["uid"]])
        fila = _fila_sugerencia({"a": a, "b": b, "similitud": similitud})
        fila.update(
            {
                # "propuesta" en una referencia significa "llega al umbral":
                # por definicion una absorbida no se puede volver a proponer.
                "propuesta": similitud >= umbral,
                "motivo": None,
                "referencia": True,
            }
        )
        referencias_out.append(fila)
    referencias_out.sort(key=lambda f: -f["similitud"])

    return {
        "parejas": parejas,
        "referencias": referencias_out,
        "llamadas": llamadas,
        "dimensiones": len(vectores[0]),
    }


# --------------------------------------------------------------------------
# Embebedor listo para usar
# --------------------------------------------------------------------------


def embebedor_litellm(
    base: str, clave: str | None, modelo: str, prefijo: str | None = None
) -> Callable[[list[str]], list[list[float]]]:
    """El embebedor real. Se pasa como argumento para que los tests no lo usen."""
    import llm

    def embeber(textos: list[str]) -> list[list[float]]:
        return llm.embeddings(base, clave, modelo, textos, prefijo=prefijo)

    return embeber
