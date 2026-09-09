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
    for tabla in ("chunks_vec", "chunks", "reuniones_indexadas", "meta"):
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
    return {
        "chunks": fila["chunks"],
        "reuniones": fila["reuniones"],
        "modelo": meta.get("modelo"),
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
