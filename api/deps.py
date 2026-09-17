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

    El valor seguro es el que se aplica cuando nadie ha dicho nada. Las rutas
    de escritura de I6a lo consultan a traves de `conexion_escritura`.
    """
    return _verdadero(os.environ.get(VARIABLE_SOLO_LECTURA), True)


def directorio_web() -> Path:
    return Path(os.environ.get(VARIABLE_WEB) or (RAIZ / "web"))


def ruta_bd() -> Path:
    """La misma resolucion que usa el pipeline: TEAMS_DB > datos/meetings.db."""
    return memoria.ruta_bd()


def conexion() -> Iterator[sqlite3.Connection]:
    """Una conexion de solo lectura por peticion.

    Se abre y se cierra en cada peticion a proposito: abrir un fichero local
    cuesta microsegundos y asi ninguna conexion sobrevive a la peticion que la
    creo. `entre_hilos` es obligatorio: FastAPI ejecuta las rutas sincronas en
    un pool y el `finally` de esta dependencia puede caer en un hilo distinto
    del que abrio la conexion. Solo se nota con varias peticiones a la vez
    -- la pagina lanza tres en paralelo --, que es como se descubrio.
    """
    path = ruta_bd()
    try:
        conn = memoria.conectar(
            path, solo_lectura=solo_lectura(), entre_hilos=True
        )
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


def conexion_escritura() -> Iterator[sqlite3.Connection]:
    """La conexion de las rutas que corrigen acciones (I6a).

    Aparte de la de lectura, y con tres diferencias deliberadas:

    - Con `TEAMS_API_SOLO_LECTURA` activo responde **403 con la explicacion**
      antes de abrir nada: el panel se abre igual, pero quien intente escribir
      tiene que saber que variable cambiar, no ver un 500.
    - No migra (`migrar=False`): una base atrasada da el mismo 503 que en
      lectura. Migrar el historico es una decision de quien administra el
      servidor, no el efecto secundario de pulsar *Guardar*.
    - Espera hasta 15 s si la base esta bloqueada: `summarize_teams.py` puede
      estar guardando una reunion justo en ese momento.

    Antes de la primera escritura del dia se hace la copia de seguridad
    (`memoria.copia_de_seguridad_diaria`). Si la copia falla no se escribe:
    corregir sin red es justo lo que la copia existe para evitar.
    """
    if solo_lectura():
        raise HTTPException(
            status_code=403,
            detail=(
                "La API esta en solo lectura. Para corregir acciones desde la "
                f"web, arranca el servicio con {VARIABLE_SOLO_LECTURA}=0."
            ),
        )
    path = ruta_bd()
    try:
        conn = memoria.conectar(path, entre_hilos=True, migrar=False, espera=15.0)
    except FileNotFoundError:
        raise HTTPException(
            status_code=503,
            detail=(
                f"No se encuentra la base de datos en {path}. Comprueba "
                f"TEAMS_DB y, en Docker, que el volumen este montado."
            ),
        )
    try:
        _exigir_esquema_al_dia(conn, path)
        try:
            memoria.copia_de_seguridad_diaria(path)
        except (OSError, sqlite3.Error) as exc:
            raise HTTPException(
                status_code=503,
                detail=(
                    f"No se ha podido hacer la copia de seguridad del dia en "
                    f"{path.parent / 'copias'} ({exc}); no se escribe sin ella."
                ),
            )
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
