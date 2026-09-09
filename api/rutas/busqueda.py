"""Busqueda de texto completo sobre las transcripciones (fase I4).

Un solo endpoint, `GET /api/buscar`, sobre la tabla FTS5 `segments_fts`. Lo
que devuelve son **segmentos**, no reuniones: la pregunta que responde esta
vista es "donde se dijo esto", y la respuesta util es la frase con su momento
y su enlace, no una lista de reuniones que hay que abrir una por una.

Lo unico que esta capa decide es que hacer con una consulta que no contiene
nada buscable; todo lo demas sale de `memoria.py`.
"""

import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Query

import memoria
from api.deps import conexion
from api.modelos import Coincidencia, PaginaDeBusqueda, ReunionConCoincidencias
from api.rutas.reuniones import _FECHA

router = APIRouter(tags=["busqueda"])

# Un resultado es una linea de transcripcion: caben muchos por pagina, pero el
# front los pinta todos de golpe, asi que el tope evita que una palabra muy
# comun ("vale") devuelva media base en una sola respuesta.
RESULTADOS_POR_PAGINA = 50
TOPE_RESULTADOS = 200

# Cuantas reuniones se listan en el desglose lateral. Mas alla de eso deja de
# ser un indice y pasa a ser otra lista que hay que recorrer.
TOPE_REUNIONES = 50


@router.get("/buscar", response_model=PaginaDeBusqueda, summary="Buscar en las transcripciones")
def buscar(
    conn: sqlite3.Connection = Depends(conexion),
    q: str = Query(..., min_length=1, description="Texto libre; admite \"frases\" y prefijo*"),
    uid: str | None = Query(None, description="Acota a una reunion concreta"),
    tipo: str | None = Query(None, description="daily | workshop | retro | planning"),
    desde: str | None = Query(None, pattern=_FECHA, description="Fecha ISO inclusive"),
    hasta: str | None = Query(None, pattern=_FECHA, description="Fecha ISO inclusive"),
    orden: str = Query("relevancia", description="relevancia | reciente | antiguo"),
    limite: int = Query(RESULTADOS_POR_PAGINA, ge=1, le=TOPE_RESULTADOS),
    desplazamiento: int = Query(0, ge=0),
) -> PaginaDeBusqueda:
    """Los segmentos donde aparece el termino, resaltados y con su reunion.

    Los acentos son indiferentes en los dos sentidos, porque la consulta pasa
    por el mismo tokenizador con el que se indexo. No hay stemming: "reunion"
    no encuentra "reuniones"; para eso esta el prefijo `reunion*`.

    `reuniones` viene calculado **sin** el filtro `uid`, para que acotar a una
    reunion no borre la lista desde la que se acota.
    """
    if tipo is not None and tipo not in memoria.TIPOS_REUNION:
        raise HTTPException(
            status_code=422,
            detail=f"Tipo desconocido: {tipo!r}. Validos: {list(memoria.TIPOS_REUNION)}",
        )
    if orden not in memoria.ORDENES_BUSQUEDA:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Orden desconocido: {orden!r}. Validos: "
                f"{list(memoria.ORDENES_BUSQUEDA)}"
            ),
        )
    consulta = memoria.consulta_fts(q)
    if consulta is None:
        # Una busqueda de solo signos ("***", "?!") no es un error del
        # servidor ni una busqueda sin resultados: no hay nada que buscar, y
        # decirlo asi es lo unico que permite al front distinguirlo.
        raise HTTPException(
            status_code=422,
            detail=(
                f"La busqueda {q!r} no tiene ninguna palabra que buscar. "
                f"Escribe al menos una."
            ),
        )

    filtros = {"uid": uid, "tipo": tipo, "desde": desde, "hasta": hasta}
    filas = memoria.buscar_segmentos(
        conn,
        consulta,
        orden=orden,
        limite=limite,
        desplazamiento=desplazamiento,
        **filtros,
    )
    return PaginaDeBusqueda(
        q=q,
        consulta_fts=consulta,
        total=memoria.contar_busqueda(conn, consulta, **filtros),
        limite=limite,
        desplazamiento=desplazamiento,
        orden=orden,
        marca_inicio=memoria.MARCA_INICIO,
        marca_fin=memoria.MARCA_FIN,
        reuniones=[
            ReunionConCoincidencias(**dict(f))
            for f in memoria.reuniones_de_busqueda(
                conn, consulta, limite=TOPE_REUNIONES, **filtros
            )
        ],
        coincidencias=[Coincidencia(**dict(f)) for f in filas],
    )
