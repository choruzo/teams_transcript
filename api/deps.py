"""Configuracion y dependencias compartidas por las rutas."""

import os
import sqlite3
from collections.abc import Iterator
from pathlib import Path

from fastapi import HTTPException

import memoria

RAIZ = Path(__file__).resolve().parents[1]

VARIABLE_SOLO_LECTURA = "TEAMS_API_SOLO_LECTURA"
VARIABLE_WEB = "TEAMS_WEB_DIR"


def _verdadero(valor: str | None, por_defecto: bool) -> bool:
    if valor is None:
        return por_defecto
    return valor.strip().lower() in ("1", "true", "si", "sí", "yes", "on")


def solo_lectura() -> bool:
    """Si la API tiene prohibido escribir. Por defecto **si**.

    El valor seguro es el que se aplica cuando nadie ha dicho nada: en I0 no
    hay ningun endpoint de escritura, y cuando los haya (I6) tendran que
    consultarlo explicitamente.
    """
    return _verdadero(os.environ.get(VARIABLE_SOLO_LECTURA), True)


def directorio_web() -> Path:
    return Path(os.environ.get(VARIABLE_WEB) or (RAIZ / "web"))


def ruta_bd() -> Path:
    """La misma resolucion que usa el pipeline: TEAMS_DB > datos/meetings.db."""
    return memoria.ruta_bd()


def conexion() -> Iterator[sqlite3.Connection]:
    """Una conexion de solo lectura por peticion.

    Se abre y se cierra en cada peticion a proposito: sqlite3 no comparte
    conexiones entre hilos, y FastAPI ejecuta las rutas sincronas en un pool.
    Abrir una conexion a un fichero local cuesta microsegundos.
    """
    path = ruta_bd()
    try:
        conn = memoria.conectar(path, solo_lectura=solo_lectura())
    except FileNotFoundError:
        # Mensaje util en vez de un 500 opaco: en el servidor, esto casi
        # siempre significa que el volumen no esta montado donde se cree.
        raise HTTPException(
            status_code=503,
            detail=(
                f"No se encuentra la base de datos en {path}. Comprueba "
                f"TEAMS_DB y, en Docker, que el volumen este montado."
            ),
        )
    try:
        _exigir_esquema_al_dia(conn, path)
        yield conn
    finally:
        conn.close()


def _exigir_esquema_al_dia(conn: sqlite3.Connection, path: Path) -> None:
    """Falla con una explicacion en vez de con un error de SQL.

    Una API de solo lectura no puede migrar la base, asi que si esta atrasada
    las consultas se romperian con un `no such column` incomprensible. Es el
    caso normal al desplegar por primera vez sobre una base creada por una
    version anterior del pipeline.
    """
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version == memoria.ESQUEMA_VERSION:
        return
    if version > memoria.ESQUEMA_VERSION:
        raise HTTPException(
            status_code=503,
            detail=(
                f"La base usa el esquema {version} y esta API entiende el "
                f"{memoria.ESQUEMA_VERSION}. Actualiza el codigo del servidor."
            ),
        )
    raise HTTPException(
        status_code=503,
        detail=(
            f"La base esta en el esquema {version} y hace falta el "
            f"{memoria.ESQUEMA_VERSION}. Migrala con: python memoria.py "
            f"--migrar --db {path}"
        ),
    )
