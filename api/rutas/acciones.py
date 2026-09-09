"""Tablero de acciones (seccion 3.4 del plan, fase I3).

**Solo lectura.** Cambiar el estado de una accion, reasignarla o fusionar
duplicados es la fase I6, y no se adelanta aqui: para escribir hacen falta la
tabla `overrides` y su reaplicacion tras cada pasada del LLM, o la siguiente
ejecucion de `summarize_teams.py` se llevaria la correccion por delante.

Ninguna cifra la calcula esta capa: todo sale de las consultas de `memoria.py`,
compartidas con el pipeline, y aqui solo se traducen al contrato de la API.
"""

import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Query

import memoria
from api.deps import conexion
from api.modelos import AccionTablero, PaginaDeAcciones, Responsable
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
