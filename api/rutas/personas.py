"""Personas del equipo (I6a: el selector de responsable del panel).

En I6a solo se leen. El panel crea una persona nueva al guardar un responsable
que no existe, a traves de `memoria.corregir_accion`; editar nombres, alias y
altas es I6b.
"""

import sqlite3

from fastapi import APIRouter, Depends

import memoria
from api.deps import conexion
from api.modelos import Persona

router = APIRouter(tags=["personas"])


@router.get("/personas", response_model=list[Persona], summary="Personas del equipo")
def listar_personas(conn: sqlite3.Connection = Depends(conexion)) -> list[Persona]:
    return [Persona(**p) for p in memoria.listar_personas(conn)]
