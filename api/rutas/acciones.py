"""Tablero de acciones (fase I3) y su correccion desde la web (fase I6a).

La lectura (`GET /acciones`) es el tablero de la seccion 3.4. La escritura
son las rutas de la seccion 4 «Escritura de acciones»: cada una llama a la
operacion correspondiente de la Fase 7 de `memoria.py`, que es donde vive toda
la logica, reconciliacion al reprocesar incluida. Si esa logica estuviera
aqui, procesar por SSH seguiria borrando las correcciones.

Ninguna cifra la calcula esta capa. Todas las escrituras:

- usan `conexion_escritura` (403 en solo lectura, copia del dia antes);
- exigen la cabecera `If-Match: "<uid>:<version>"` con la ficha que vio el
  panel, y responden 409 si ha cambiado (`memoria.comprobando_version`). El
  uid va dentro porque el panel de una accion tambien opera sobre otras
  (separar una absorbida, quitar un «bloquea a»);
- traducen los errores del motor: `AccionNoEncontrada` a 404 y
  `CorreccionInvalida` a 422 con su mensaje, que el panel ensena tal cual.
"""

import re
import sqlite3
from collections.abc import Callable

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response

import memoria
from api.deps import conexion, conexion_escritura
from api.modelos import (
    AccionTablero,
    CambiosDeAccion,
    Descarte,
    FichaAccion,
    Fusion,
    PaginaDeAcciones,
    Responsable,
)
from api.rutas.reuniones import _FECHA

router = APIRouter(tags=["acciones"])

# Una accion es una linea corta: caben muchas mas por pagina que reuniones, y
# el tablero se lee de un vistazo o no sirve. El tope existe para que un
# historico de dos anos no llegue en una sola respuesta.
ACCIONES_POR_PAGINA = 100
TOPE_ACCIONES = 500

# Un ano largo. Mas alla de eso el filtro no discrimina nada -- ninguna accion
# viva lleva tres anos sin tocarse -- y solo sirve para pedir tablas absurdas.
TOPE_DIAS = 1000


@router.get("/acciones", response_model=PaginaDeAcciones, summary="Tablero de acciones")
def listar_acciones(
    conn: sqlite3.Connection = Depends(conexion),
    estado: list[str] | None = Query(
        None, description="Repetible: ?estado=abierta&estado=bloqueada"
    ),
    persona: str | None = Query(
        None,
        description=(
            "Nombre exacto (sin distinguir mayusculas), o el centinela "
            "`sin_responsable` de la respuesta para las que no tienen dueño"
        ),
    ),
    q: str | None = Query(None, description="Texto en la descripcion, sin acentos"),
    estancadas: bool | None = Query(
        None, description="Solo las estancadas, solo las que no, o todas"
    ),
    desde: str | None = Query(None, pattern=_FECHA, description="Fecha ISO inclusive"),
    hasta: str | None = Query(None, pattern=_FECHA, description="Fecha ISO inclusive"),
    dias_sin_tocar: int | None = Query(None, ge=1, le=TOPE_DIAS),
    orden: str = Query("prioridad", description="prioridad | antiguedad | reciente | persona"),
    limite: int = Query(ACCIONES_POR_PAGINA, ge=1, le=TOPE_ACCIONES),
    desplazamiento: int = Query(0, ge=0),
) -> PaginaDeAcciones:
    """Las acciones del historico completo, filtradas y paginadas.

    El periodo (`desde`/`hasta`) se aplica igual que en el timeline: entra lo
    que **solapa** el rango y no solo lo nacido dentro, porque una accion de
    hace dos meses que sigue viva es justo la que hay que ver al mirar esta
    semana.

    `por_estado` y `responsables` vienen calculados sin su propio filtro: un
    control tiene que decir a donde lleva cambiarlo, no repetir lo ya elegido.
    """
    estados = _validar_estados(estado)
    if orden not in memoria.ORDENES_ACCIONES:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Orden desconocido: {orden!r}. Validos: "
                f"{list(memoria.ORDENES_ACCIONES)}"
            ),
        )
    filtros = {
        "estados": estados,
        "persona": persona,
        "texto": q,
        "estancadas": estancadas,
        "desde": desde,
        "hasta": hasta,
        "dias_sin_tocar": dias_sin_tocar,
    }
    filas = memoria.listar_acciones(
        conn, orden=orden, limite=limite, desplazamiento=desplazamiento, **filtros
    )
    return PaginaDeAcciones(
        total=memoria.contar_acciones(conn, **filtros),
        limite=limite,
        desplazamiento=desplazamiento,
        orden=orden,
        # El mismo numero que usa el `.md` del pipeline: vive en memoria.py
        # justo para que la web y el fichero no puedan contradecirse.
        umbral_estancamiento=memoria.UMBRAL_ESTANCAMIENTO,
        sin_responsable=memoria.SIN_RESPONSABLE,
        por_estado=memoria.acciones_por_estado(conn, **filtros),
        responsables=[
            Responsable(**dict(f)) for f in memoria.responsables_de_acciones(conn, **filtros)
        ],
        acciones=[AccionTablero(**dict(f)) for f in filas],
    )


def _validar_estados(estados: list[str] | None) -> list[str] | None:
    """Un estado inventado devuelve 422, no una lista vacia.

    Una lista vacia se lee como "no hay nada que hacer", que es exactamente la
    conclusion equivocada cuando lo que pasa es que el filtro esta mal escrito.
    """
    if not estados:
        return None
    desconocidos = [e for e in estados if e not in memoria.ESTADOS_ACCION]
    if desconocidos:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Estados desconocidos: {desconocidos}. Validos: "
                f"{list(memoria.ESTADOS_ACCION)}"
            ),
        )
    return estados


# --------------------------------------------------------------------------
# I6a: ficha y correccion
# --------------------------------------------------------------------------

_IF_MATCH = re.compile(r'^\s*(?:W/)?"?([a-z0-9_-]+):(\d+)"?\s*$')


def etiqueta(ficha: dict) -> str:
    """El ETag de una ficha, y lo que el panel devuelve en `If-Match`."""
    return f'"{ficha["uid"]}:{ficha["version"]}"'


def _con_etag(respuesta: Response, ficha: dict) -> FichaAccion:
    respuesta.headers["ETag"] = etiqueta(ficha)
    return FichaAccion(**ficha)


@router.get("/acciones/{uid}", response_model=FichaAccion, summary="Ficha de una accion")
def ficha_accion(
    uid: str, respuesta: Response, conn: sqlite3.Connection = Depends(conexion)
) -> FichaAccion:
    """Menciones, dependencias, absorbidas e historial. Tambien si esta descartada."""
    try:
        return _con_etag(respuesta, memoria.detalle_accion(conn, uid))
    except memoria.AccionNoEncontrada as exc:
        raise HTTPException(status_code=404, detail=str(exc))


def _escribir(
    conn: sqlite3.Connection,
    si_coincide: str | None,
    implicadas: tuple[str, ...],
    operacion: Callable[[], dict],
    respuesta: Response,
) -> FichaAccion:
    """Comprueba la version, ejecuta la operacion del motor y traduce errores."""
    if not si_coincide:
        raise HTTPException(
            status_code=428,
            detail=(
                'Falta la cabecera If-Match con la ficha que se esta editando '
                '("<uid>:<version>", el ETag de GET /api/acciones/{uid}). Sin '
                "ella se podria pisar un cambio ajeno sin enterarse."
            ),
        )
    encaje = _IF_MATCH.match(si_coincide)
    if not encaje:
        raise HTTPException(
            status_code=422,
            detail=f'If-Match no valido: {si_coincide!r}; se espera "<uid>:<version>".',
        )
    uid_visto, version = encaje.group(1), int(encaje.group(2))
    if uid_visto not in implicadas:
        raise HTTPException(
            status_code=422,
            detail=(
                f"If-Match se refiere a {uid_visto}, que no participa en esta "
                f"operacion ({', '.join(implicadas)})."
            ),
        )
    try:
        with memoria.comprobando_version(conn, uid_visto, version):
            ficha = operacion()
    except memoria.VersionObsoleta as exc:
        raise HTTPException(
            status_code=409,
            detail={"mensaje": str(exc), "uid": exc.uid, "version": exc.actual},
        )
    except memoria.AccionNoEncontrada as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except memoria.CorreccionInvalida as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except sqlite3.OperationalError as exc:
        if "locked" in str(exc) or "busy" in str(exc):
            raise HTTPException(
                status_code=503,
                detail=(
                    "La base esta ocupada (seguramente el pipeline guardando una "
                    "reunion). Vuelve a intentarlo en unos segundos."
                ),
            )
        raise
    return _con_etag(respuesta, ficha)


@router.patch("/acciones/{uid}", response_model=FichaAccion, summary="Corregir una accion")
def corregir(
    uid: str,
    cambios: CambiosDeAccion,
    respuesta: Response,
    if_match: str | None = Header(None),
    conn: sqlite3.Connection = Depends(conexion_escritura),
) -> FichaAccion:
    """Descripcion, responsable y/o estado; solo cambia lo que se envia.

    Cerrar una accion con dependencias abiertas se permite y vuelve en `avisos`.
    """
    enviados = cambios.model_dump(exclude_unset=True)
    argumentos = {}
    if enviados.get("descripcion") is not None:
        argumentos["descripcion"] = enviados["descripcion"]
    if "persona" in enviados:
        argumentos["persona"] = enviados["persona"] or None
    if enviados.get("estado") is not None:
        argumentos["estado"] = enviados["estado"]
    return _escribir(
        conn,
        if_match,
        (uid,),
        lambda: memoria.corregir_accion(conn, uid, **argumentos),
        respuesta,
    )


@router.post("/acciones/{uid}/descartar", response_model=FichaAccion, summary="Descartar")
def descartar(
    uid: str,
    cuerpo: Descarte,
    respuesta: Response,
    if_match: str | None = Header(None),
    conn: sqlite3.Connection = Depends(conexion_escritura),
) -> FichaAccion:
    return _escribir(
        conn,
        if_match,
        (uid,),
        lambda: memoria.descartar_accion(conn, uid, cuerpo.motivo),
        respuesta,
    )


@router.post("/acciones/{uid}/restaurar", response_model=FichaAccion, summary="Restaurar")
def restaurar(
    uid: str,
    respuesta: Response,
    if_match: str | None = Header(None),
    conn: sqlite3.Connection = Depends(conexion_escritura),
) -> FichaAccion:
    return _escribir(
        conn, if_match, (uid,), lambda: memoria.restaurar_accion(conn, uid), respuesta
    )


@router.post(
    "/acciones/{uid}/fusionar", response_model=FichaAccion, summary="Absorber una duplicada"
)
def fusionar(
    uid: str,
    cuerpo: Fusion,
    respuesta: Response,
    if_match: str | None = Header(None),
    conn: sqlite3.Connection = Depends(conexion_escritura),
) -> FichaAccion:
    """`{uid}` es la principal y `duplicada_uid` la que queda absorbida.

    Devuelve la ficha de la principal. `If-Match` puede ser de cualquiera de
    las dos: a veces se fusiona desde el panel de la duplicada.
    """
    return _escribir(
        conn,
        if_match,
        (uid, cuerpo.duplicada_uid),
        lambda: memoria.fusionar_acciones(conn, uid, cuerpo.duplicada_uid),
        respuesta,
    )


@router.post(
    "/acciones/{uid}/separar", response_model=FichaAccion, summary="Deshacer una fusion"
)
def separar(
    uid: str,
    respuesta: Response,
    if_match: str | None = Header(None),
    conn: sqlite3.Connection = Depends(conexion_escritura),
) -> FichaAccion:
    """`{uid}` es la duplicada. `If-Match` puede ser de ella o de su principal."""
    try:
        principal = memoria.detalle_accion(conn, uid)["absorbida_por"]
    except memoria.AccionNoEncontrada as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    implicadas = (uid, principal) if principal else (uid,)
    return _escribir(
        conn, if_match, implicadas, lambda: memoria.separar_acciones(conn, uid), respuesta
    )


@router.put(
    "/acciones/{uid}/dependencias/{otra_uid}",
    response_model=FichaAccion,
    summary="`uid` depende de `otra_uid`",
)
def anadir_dependencia(
    uid: str,
    otra_uid: str,
    respuesta: Response,
    if_match: str | None = Header(None),
    conn: sqlite3.Connection = Depends(conexion_escritura),
) -> FichaAccion:
    """Idempotente. Un ciclo lo rechaza el motor con un 422."""
    return _escribir(
        conn,
        if_match,
        (uid, otra_uid),
        lambda: memoria.anadir_dependencia(conn, uid, otra_uid),
        respuesta,
    )


@router.delete(
    "/acciones/{uid}/dependencias/{otra_uid}",
    response_model=FichaAccion,
    summary="Quitar «`uid` depende de `otra_uid`»",
)
def quitar_dependencia(
    uid: str,
    otra_uid: str,
    respuesta: Response,
    if_match: str | None = Header(None),
    conn: sqlite3.Connection = Depends(conexion_escritura),
) -> FichaAccion:
    return _escribir(
        conn,
        if_match,
        (uid, otra_uid),
        lambda: memoria.quitar_dependencia(conn, uid, otra_uid),
        respuesta,
    )


@router.post(
    "/acciones/{uid}/correcciones/{correccion_id}/deshacer",
    response_model=FichaAccion,
    summary="Deshacer una correccion de descripcion, responsable o estado",
)
def deshacer(
    uid: str,
    correccion_id: int,
    respuesta: Response,
    if_match: str | None = Header(None),
    conn: sqlite3.Connection = Depends(conexion_escritura),
) -> FichaAccion:
    """Solo la ultima vigente de su campo (`deshacible` en la ficha).

    Se comprueba que la correccion sea de `{uid}`: deshacer por un id suelto
    permitiria tocar otra accion con la version de esta.
    """
    try:
        ficha = memoria.detalle_accion(conn, uid)
    except memoria.AccionNoEncontrada as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    if not any(c["id"] == correccion_id for c in ficha["correcciones"]):
        raise HTTPException(
            status_code=404,
            detail=f"La accion {uid} no tiene ninguna correccion {correccion_id}.",
        )
    return _escribir(
        conn,
        if_match,
        (uid,),
        lambda: memoria.deshacer_correccion(conn, correccion_id),
        respuesta,
    )
