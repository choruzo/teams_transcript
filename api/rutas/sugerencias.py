"""Sugerencias de duplicados (fase I11).

El motor es la Fase 8: los vectores viven en `indice.db` y las reglas en
`rag.parejas_similares`. Esta capa **no decide** ninguna pareja ni fusiona
nada: lee las sugerencias ya calculadas, las filtra por lo que se esta viendo
en el tablero o en el panel, y delega el rechazo en `memoria.py`.

La ausencia de indice no es un error: `disponible=False` y un `aviso` con la
orden que lo arregla. Es el mismo criterio que el chat y el informe: sin
vectores no hay sugerencias, y se dice en vez de fingir que no hay duplicados.
"""

import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Query

import memoria
import rag
from api.deps import conexion, conexion_escritura
from api.modelos import PaginaDeSugerencias, RechazoDeSugerencia, Sugerencia

router = APIRouter(tags=["sugerencias"])

SUGERENCIAS_POR_DEFECTO = 100
TOPE_SUGERENCIAS = 500
AVISO_SIN_INDICE = (
    "No hay indice semantico. Ejecuta `python indexar_teams.py --acciones` "
    "para poder proponer duplicados."
)
AVISO_SIN_VECTORES = (
    "El indice no tiene vectores de acciones. Ejecuta "
    "`python indexar_teams.py --acciones`."
)


def _filtrar(parejas: list[dict], persona: str | None, texto: str | None) -> list[dict]:
    """Acota lo que ya propuso el motor; no cambia la similitud ni las reglas.

    `persona` es el mismo valor que usa el tablero (nombre exacto o el
    centinela de las acciones sin responsable). Una pareja entra si **alguna**
    de las dos acciones cumple: no se esta filtrando la otra mitad.
    """
    if persona:
        if persona == memoria.SIN_RESPONSABLE:
            parejas = [
                p for p in parejas if not p["a_persona"] or not p["b_persona"]
            ]
        else:
            clave = memoria.sin_acentos(persona)
            parejas = [
                p
                for p in parejas
                if clave == memoria.sin_acentos(p["a_persona"])
                or clave == memoria.sin_acentos(p["b_persona"])
            ]
    if texto:
        clave = memoria.sin_acentos(texto)
        parejas = [
            p
            for p in parejas
            if clave in memoria.sin_acentos(p["a_descripcion"])
            or clave in memoria.sin_acentos(p["b_descripcion"])
        ]
    return parejas


def _calcular(conn: sqlite3.Connection) -> tuple[list[dict], str | None]:
    """`(parejas, aviso)`. Nunca lanza por culpa del indice."""
    acciones = memoria.acciones_para_sugerir(conn)
    if not acciones:
        return [], None
    try:
        conn_idx = rag.conectar(solo_lectura=True, entre_hilos=True)
    except FileNotFoundError:
        return [], AVISO_SIN_INDICE
    except (OSError, sqlite3.Error) as exc:
        return [], f"No se pudo abrir el indice: {exc}"
    try:
        if not rag.hay_vectores_acciones(conn_idx):
            return [], AVISO_SIN_VECTORES
        return (
            rag.parejas_similares(
                conn_idx,
                acciones,
                rechazadas=memoria.sugerencias_rechazadas(conn),
            ),
            None,
        )
    except (rag.ErrorIndice, sqlite3.Error) as exc:
        return [], f"No se pudieron calcular las sugerencias: {exc}"
    finally:
        conn_idx.close()


def _pagina(
    parejas: list[dict],
    aviso: str | None,
    *,
    limite: int,
    desplazamiento: int = 0,
) -> PaginaDeSugerencias:
    return PaginaDeSugerencias(
        disponible=aviso is None,
        aviso=aviso,
        umbral=rag.UMBRAL_SUGERENCIA,
        total=len(parejas),
        sugerencias=[
            Sugerencia(**pareja)
            for pareja in parejas[desplazamiento : desplazamiento + limite]
        ],
    )


@router.get(
    "/sugerencias",
    response_model=PaginaDeSugerencias,
    summary="Posibles duplicados entre las acciones abiertas",
)
def listar_sugerencias(
    conn: sqlite3.Connection = Depends(conexion),
    persona: str | None = Query(
        None, description="Nombre exacto o el centinela de sin responsable"
    ),
    q: str | None = Query(None, description="Texto en cualquiera de las dos descripciones"),
    limite: int = Query(SUGERENCIAS_POR_DEFECTO, ge=1, le=TOPE_SUGERENCIAS),
    desplazamiento: int = Query(0, ge=0),
) -> PaginaDeSugerencias:
    """La lista del tablero, filtrada por responsable o texto.

    El filtro no decide parejas: el motor ya las calculo entre todas las
    acciones abiertas y aqui solo se descartan las que no tocan lo que se esta
    mirando. Solo se comparan acciones abiertas; una cerrada no se fusiona.
    """
    parejas, aviso = _calcular(conn)
    if aviso:
        return _pagina([], aviso, limite=limite)
    return _pagina(
        _filtrar(parejas, persona, q),
        None,
        limite=limite,
        desplazamiento=desplazamiento,
    )


@router.get(
    "/acciones/{uid}/sugerencias",
    response_model=PaginaDeSugerencias,
    summary="Posibles duplicados de una accion concreta",
)
def sugerencias_de_accion(
    uid: str,
    conn: sqlite3.Connection = Depends(conexion),
) -> PaginaDeSugerencias:
    """Las parejas en las que aparece `uid`, para el panel de la ficha.

    Se calcula la lista global (con una sugerencia por accion) y se filtra: si
    el panel ofreciera una candidata que el tablero no ensena, la misma accion
    se leeria distinto segun por donde se llegue.
    """
    parejas, aviso = _calcular(conn)
    if aviso:
        return _pagina([], aviso, limite=SUGERENCIAS_POR_DEFECTO)
    propias = [p for p in parejas if uid in (p["a_uid"], p["b_uid"])]
    return _pagina(propias, None, limite=SUGERENCIAS_POR_DEFECTO)


@router.post(
    "/sugerencias/{a_uid}/{b_uid}/rechazar",
    response_model=RechazoDeSugerencia,
    summary="Anotar que dos acciones no son la misma",
)
def rechazar_sugerencia(
    a_uid: str,
    b_uid: str,
    conn: sqlite3.Connection = Depends(conexion_escritura),
) -> RechazoDeSugerencia:
    """La pareja no vuelve a proponerse, ni siquiera tras reconstruir el indice.

    No exige `If-Match`: no cambia la accion, solo anota una decision sobre el
    par. Es idempotente y simetrica (`memoria.rechazar_sugerencia`).
    """
    try:
        return RechazoDeSugerencia(**memoria.rechazar_sugerencia(conn, a_uid, b_uid))
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
