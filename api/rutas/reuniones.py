"""Listado y consulta de reuniones."""

import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Query

import memoria
from api.deps import conexion
from api.modelos import PaginaDeReuniones, Reunion

router = APIRouter(prefix="/reuniones", tags=["reuniones"])

# Formato ISO de fecha. Se valida en la entrada para que un filtro mal escrito
# devuelva 422 con explicacion, y no una lista vacia que parece un fallo de
# datos.
_FECHA = r"^\d{4}-\d{2}-\d{2}$"


def _a_modelo(fila: sqlite3.Row) -> Reunion:
    return Reunion(
        uid=fila["uid"],
        fecha=fila["fecha"],
        titulo=fila["titulo"],
        tipo=fila["tipo"],
        duracion_seg=fila["duracion_seg"],
        resumen=fila["resumen"],
        # Solo si la fila tiene ruta; que el fichero exista de verdad es cosa
        # de I7, que es quien va a servirlo.
        tiene_audio=bool(fila["audio_path"]),
        n_segmentos=fila["n_segmentos"],
        n_acciones=fila["n_acciones"],
        n_riesgos=fila["n_riesgos"],
        n_updates=fila["n_updates"],
    )


@router.get("", response_model=PaginaDeReuniones, summary="Lista de reuniones")
def listar(
    conn: sqlite3.Connection = Depends(conexion),
    desde: str | None = Query(None, pattern=_FECHA, description="Fecha ISO inclusive"),
    hasta: str | None = Query(None, pattern=_FECHA, description="Fecha ISO inclusive"),
    tipo: str | None = Query(None, description="daily | workshop | retro | planning"),
    limite: int = Query(50, ge=1, le=200),
    desplazamiento: int = Query(0, ge=0),
) -> PaginaDeReuniones:
    if tipo is not None and tipo not in memoria.TIPOS_REUNION:
        raise HTTPException(
            status_code=422,
            detail=f"Tipo desconocido: {tipo!r}. Validos: {list(memoria.TIPOS_REUNION)}",
        )
    filtros = {"desde": desde, "hasta": hasta, "tipo": tipo}
    filas = memoria.listar_reuniones(
        conn, limite=limite, desplazamiento=desplazamiento, **filtros
    )
    return PaginaDeReuniones(
        total=memoria.contar_reuniones(conn, **filtros),
        limite=limite,
        desplazamiento=desplazamiento,
        reuniones=[_a_modelo(f) for f in filas],
    )


@router.get("/{uid}", response_model=Reunion, summary="Una reunion por su uid")
def obtener(uid: str, conn: sqlite3.Connection = Depends(conexion)) -> Reunion:
    """Se direcciona por `uid` y nunca por `id`.

    El `id` cambia si la reunion se reprocesa; el `uid` no. Un enlace guardado
    o una cita del chat tienen que seguir funcionando despues de reprocesar
    (deuda D1 del plan, resuelta en D0).
    """
    filas = memoria.listar_reuniones(conn, uid=uid, limite=1)
    if not filas:
        raise HTTPException(
            status_code=404, detail=f"No hay ninguna reunion con uid {uid!r}"
        )
    return _a_modelo(filas[0])
