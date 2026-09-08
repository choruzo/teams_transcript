"""Metricas del equipo y datos del timeline (fase I1).

Ninguna cifra de aqui la calcula un LLM: todo sale de SQL, en `memoria.py`, y
por tanto es reproducible y comprobable. Este modulo solo traduce el resultado
al contrato de la API.
"""

import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Query

import memoria
from api.deps import conexion
from api.modelos import Carril, Metricas, Timeline
from api.rutas.reuniones import _FECHA, a_modelo

router = APIRouter(tags=["metricas"])

# Tope de reuniones que se dibujan de una vez. No es paginacion: un timeline a
# medias no se entiende, asi que o cabe el periodo entero o se avisa de que hay
# que estrecharlo (`truncado`).
TOPE_REUNIONES = 500
TOPE_CARRILES = 200


def _validar_tipo(tipo: str | None) -> None:
    if tipo is not None and tipo not in memoria.TIPOS_REUNION:
        raise HTTPException(
            status_code=422,
            detail=f"Tipo desconocido: {tipo!r}. Validos: {list(memoria.TIPOS_REUNION)}",
        )


@router.get("/metricas", response_model=Metricas, summary="Salud del equipo")
def obtener_metricas(
    conn: sqlite3.Connection = Depends(conexion),
    desde: str | None = Query(None, pattern=_FECHA, description="Fecha ISO inclusive"),
    hasta: str | None = Query(None, pattern=_FECHA, description="Fecha ISO inclusive"),
) -> Metricas:
    """Los agregados de la seccion 3.2 del plan, en un solo objeto.

    Sin filtro de tipo a proposito: "cuantas acciones hay abiertas" no depende
    de en que clase de reunion se hablo de ellas, y un panel de salud filtrado
    por tipo daria cifras que nadie sabria interpretar.
    """
    return Metricas(**memoria.metricas(conn, desde=desde, hasta=hasta))


@router.get("/timeline", response_model=Timeline, summary="Reuniones y carriles")
def obtener_timeline(
    conn: sqlite3.Connection = Depends(conexion),
    desde: str | None = Query(None, pattern=_FECHA),
    hasta: str | None = Query(None, pattern=_FECHA),
    tipo: str | None = Query(None, description="daily | workshop | retro | planning"),
    solo_abiertas: bool = Query(
        True, description="Carriles solo de acciones sin cerrar"
    ),
) -> Timeline:
    _validar_tipo(tipo)
    filtros = {"desde": desde, "hasta": hasta, "tipo": tipo}
    total = memoria.contar_reuniones(conn, **filtros)
    reuniones = memoria.listar_reuniones(conn, limite=TOPE_REUNIONES, **filtros)
    carriles = memoria.carriles_acciones(
        conn,
        desde=desde,
        hasta=hasta,
        solo_abiertas=solo_abiertas,
        limite=TOPE_CARRILES + 1,
    )
    truncado = total > TOPE_REUNIONES or len(carriles) > TOPE_CARRILES
    return Timeline(
        desde=desde,
        hasta=hasta,
        # El timeline dibuja de izquierda (antiguo) a derecha (reciente);
        # `listar_reuniones` devuelve al reves, que es lo que quiere un listado.
        reuniones=[a_modelo(fila) for fila in reversed(reuniones)],
        carriles=[Carril(**dict(fila)) for fila in carriles[:TOPE_CARRILES]],
        total_reuniones=total,
        truncado=truncado,
    )
