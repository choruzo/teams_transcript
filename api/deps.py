"""Configuracion y dependencias compartidas por las rutas."""

import os
import sqlite3
from collections.abc import Iterator
from pathlib import Path

from fastapi import HTTPException, Request

import autenticacion
import memoria

RAIZ = Path(__file__).resolve().parents[1]

VARIABLE_SOLO_LECTURA = "TEAMS_API_SOLO_LECTURA"
VARIABLE_WEB = "TEAMS_WEB_DIR"
VARIABLE_AUTENTICACION = "TEAMS_AUTENTICACION"

# Nombre de la cookie de sesion. En un solo sitio para que la ponga la ruta de
# login y la lea el middleware sin que se puedan desincronizar.
COOKIE_SESION = "teams_sesion"


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


def autenticacion_activa() -> bool:
    """Si la interfaz exige sesion. Por defecto **si**.

    Existe el apagon para poder desplegar y probar, y porque la app puede ir
    detras de otra capa que ya autentique; no se lee una vez al importar sino
    en cada peticion, para que un test o un reinicio de configuracion no
    queden atados al primer valor.
    """
    return _verdadero(os.environ.get(VARIABLE_AUTENTICACION), True)


def cookie_segura() -> bool:
    """Marca `Secure` la cookie. Apagado por defecto: en local va por HTTP.

    Detras del proxy con TLS conviene `TEAMS_COOKIE_SEGURA=1`, pero forzarlo por
    defecto dejaria la cookie sin guardar en `http://localhost:8080`, que es el
    modo normal de desarrollo y del tunel SSH.
    """
    return _verdadero(os.environ.get("TEAMS_COOKIE_SEGURA"), False)


def conexion_usuarios() -> Iterator[sqlite3.Connection]:
    """Una conexion a `datos/usuarios.db` por peticion.

    `entre_hilos=True` por el mismo motivo que en `conexion`: FastAPI ejecuta
    las rutas sincronas en un pool y el `finally` puede caer en otro hilo.
    """
    path = autenticacion.ruta_bd()
    try:
        conn = autenticacion.conectar(path, entre_hilos=True)
    except (OSError, sqlite3.Error) as exc:
        raise HTTPException(
            status_code=503,
            detail=(
                f"No se puede abrir la base de usuarios en {path}: {exc}. "
                f"Comprueba TEAMS_USUARIOS_DB y que el volumen este montado."
            ),
        )
    try:
        yield conn
    finally:
        conn.close()


def usuario_de_peticion(request: Request) -> dict | None:
    """El usuario de la cookie de la peticion, o None.

    Sin `Depends` a proposito: lo llama el middleware, que corre fuera del
    sistema de dependencias de FastAPI. Abre su propia conexion y la cierra.
    """
    token = request.cookies.get(COOKIE_SESION)
    if not token:
        return None
    try:
        conn = autenticacion.conectar(autenticacion.ruta_bd(), entre_hilos=True)
    except (OSError, sqlite3.Error):
        return None
    try:
        fila = autenticacion.resolver_sesion(conn, token)
    finally:
        conn.close()
    if fila is None:
        return None
    return {"nombre": fila["nombre"], "rol": fila["rol"]}


def usuario_actual(request: Request) -> dict:
    """Dependencia de las rutas que necesitan saber quien entra.

    El middleware ya ha dejado pasar solo peticiones con sesion valida, asi que
    aqui solo queda traducir la cookie; aun asi se responde 401 en vez de
    confiar en el orden, porque una ruta incluida mas adelante podria quedar
    fuera del middleware por error.
    """
    usuario = usuario_de_peticion(request)
    if usuario is None:
        raise HTTPException(status_code=401, detail="Sesion no valida o caducada.")
    return usuario



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
