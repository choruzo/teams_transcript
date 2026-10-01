"""Usuarios y sesiones de la interfaz web.

Es el unico modulo del proyecto que guarda credenciales. Vive en la raiz, junto
al resto del pipeline, y es **stdlib pura**: el servidor no tiene internet y la
autenticacion no puede depender de una libreria que no se pueda empaquetar (no
hay `passlib`, no hay `bcrypt`). El hash de la clave es PBKDF2-HMAC-SHA256, que
`hashlib` trae de serie.

La base de usuarios va **aparte de `meetings.db`** (`datos/usuarios.db`): son
dos ciclos de vida distintos. El historico de reuniones es un artefacto que se
migra y reindexa; las credenciales son un fichero pequeno que no debe viajar en
la copia de seguridad de las transcripciones ni migrarse al ritmo del esquema
de reuniones. Ruta: `--db` > `TEAMS_USUARIOS_DB` > `datos/usuarios.db`.

En esta fase hay un unico usuario con todos los privilegios (rol `admin`); no
hay registro publico ni recuperacion de clave. Para crear o cambiar la clave a
mano:

    python autenticacion.py --listar
    python autenticacion.py --crear admin            # pide la clave por consola
    python autenticacion.py --cambiar-clave admin
"""

import getpass
import hashlib
import hmac
import os
import secrets
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

RAIZ = Path(__file__).resolve().parent

VARIABLE_ENTORNO = "TEAMS_USUARIOS_DB"
VARIABLE_USUARIO_INICIAL = "TEAMS_ADMIN_USUARIO"
VARIABLE_CLAVE_INICIAL = "TEAMS_ADMIN_CLAVE"
VARIABLE_HORAS_SESION = "TEAMS_SESION_HORAS"

# Coste del PBKDF2. 200k es el orden que recomienda OWASP (2023) para
# PBKDF2-HMAC-SHA256; el hash se guarda con sus iteraciones dentro, asi que
# subirlo no invalida las claves ya existentes.
ITERACIONES = 200_000
LONGITUD_CLAVE_MINIMA = 4

USUARIO_INICIAL = "admin"
CLAVE_INICIAL = "admin"
ROL_INICIAL = "admin"
HORAS_SESION = 12

# Version del esquema de `usuarios.db`, en su propio `user_version`. Separada
# de la de `meetings.db`: son ficheros independientes que no migran juntos.
ESQUEMA_VERSION = 1

_ESQUEMA = """
CREATE TABLE IF NOT EXISTS usuarios (
    id            INTEGER PRIMARY KEY,
    nombre        TEXT NOT NULL UNIQUE COLLATE NOCASE,
    clave_hash    TEXT NOT NULL,
    rol           TEXT NOT NULL DEFAULT 'admin',
    activo        INTEGER NOT NULL DEFAULT 1,
    creado_en     TEXT NOT NULL,
    actualizado_en TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sesiones (
    token      TEXT PRIMARY KEY,
    usuario_id INTEGER NOT NULL REFERENCES usuarios(id) ON DELETE CASCADE,
    creado_en  TEXT NOT NULL,
    expira_en  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sesiones_expira ON sesiones(expira_en);
"""


def ruta_bd(ruta: str | os.PathLike | None = None) -> Path:
    """Resolucion en cascada: argumento > TEAMS_USUARIOS_DB > datos/usuarios.db."""
    if ruta:
        return Path(ruta)
    del_entorno = os.environ.get(VARIABLE_ENTORNO)
    return Path(del_entorno) if del_entorno else RAIZ / "datos" / "usuarios.db"


def conectar(
    ruta: str | os.PathLike | None = None, entre_hilos: bool = False
) -> sqlite3.Connection:
    """Abre la base de usuarios y se asegura de que el esquema existe.

    Crea el directorio padre si hace falta: en un despliegue nuevo `datos/`
    puede no existir todavia y lo primero que hace la app es intentar leer.
    """
    path = ruta_bd(ruta)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(
        path, check_same_thread=not entre_hilos, timeout=10.0
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # WAL como en `meetings.db`: la sesion se escribe en cada login/logout
    # mientras otra peticion puede estar leyendo la misma base.
    conn.execute("PRAGMA journal_mode = WAL")
    _asegurar_esquema(conn)
    return conn


def _asegurar_esquema(conn: sqlite3.Connection) -> None:
    """Crea el esquema solo la primera vez.

    Se mira `user_version` en vez de ejecutar el `CREATE TABLE IF NOT EXISTS`
    en cada conexion: ese script abre una transaccion de escritura y tomarla en
    cada peticion autenticada serializaria sin motivo. `executescript` confirma
    por su cuenta, asi que no hace falta un `commit` aparte.
    """
    if conn.execute("PRAGMA user_version").fetchone()[0] == ESQUEMA_VERSION:
        return
    conn.executescript(_ESQUEMA)
    conn.execute(f"PRAGMA user_version = {ESQUEMA_VERSION}")
    conn.commit()


# --------------------------------------------------------------------------
# Claves
# --------------------------------------------------------------------------


def hash_clave(clave: str, sal: bytes | None = None, iteraciones: int = ITERACIONES) -> str:
    """PBKDF2-HMAC-SHA256 a un formato de una linea.

    `pbkdf2_sha256$<iteraciones>$<sal_hex>$<hash_hex>`. Guardar las iteraciones
    dentro permite subir el coste en el futuro sin invalidar lo ya guardado.
    """
    if sal is None:
        sal = secrets.token_bytes(16)
    derivado = hashlib.pbkdf2_hmac(
        "sha256", clave.encode("utf-8"), sal, iteraciones
    )
    return f"pbkdf2_sha256${iteraciones}${sal.hex()}${derivado.hex()}"


def verificar_clave(clave: str, guardado: str) -> bool:
    """Compara en tiempo constante, para no filtrar la clave por el reloj."""
    try:
        algoritmo, iteraciones, sal_hex, hash_hex = guardado.split("$")
    except (AttributeError, ValueError):
        return False
    if algoritmo != "pbkdf2_sha256":
        return False
    try:
        sal = bytes.fromhex(sal_hex)
        esperado = bytes.fromhex(hash_hex)
        costo = int(iteraciones)
    except ValueError:
        return False
    if not esperado or costo <= 0:
        return False
    derivado = hashlib.pbkdf2_hmac(
        "sha256", clave.encode("utf-8"), sal, costo
    )
    return hmac.compare_digest(derivado, esperado)


# --------------------------------------------------------------------------
# Usuarios
# --------------------------------------------------------------------------


def _ahora() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def usuario_por_nombre(conn: sqlite3.Connection, nombre: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM usuarios WHERE nombre = ? COLLATE NOCASE", (nombre,)
    ).fetchone()


def listar_usuarios(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT id, nombre, rol, activo, creado_en, actualizado_en "
        "FROM usuarios ORDER BY nombre"
    ).fetchall()


def hay_usuarios(conn: sqlite3.Connection) -> bool:
    return conn.execute("SELECT 1 FROM usuarios LIMIT 1").fetchone() is not None


def crear_usuario(
    conn: sqlite3.Connection,
    nombre: str,
    clave: str,
    rol: str = ROL_INICIAL,
    activo: bool = True,
) -> int:
    """Alta o actualizacion de la clave de un usuario existente.

    Un `INSERT ... ON CONFLICT` en vez de dos ramas: el nombre es UNIQUE
    COLLATE NOCASE, asi que dar de alta otra vez es cambiarle la clave, que es
    justo lo que se quiere en una instalacion de un solo usuario.
    """
    nombre = (nombre or "").strip()
    if not nombre:
        raise ValueError("El nombre de usuario no puede estar vacio.")
    if len(clave or "") < LONGITUD_CLAVE_MINIMA:
        raise ValueError(
            f"La clave debe tener al menos {LONGITUD_CLAVE_MINIMA} caracteres."
        )
    conn.execute(
        """
        INSERT INTO usuarios (nombre, clave_hash, rol, activo, creado_en, actualizado_en)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(nombre) DO UPDATE SET
            clave_hash = excluded.clave_hash,
            rol = excluded.rol,
            activo = excluded.activo,
            actualizado_en = excluded.actualizado_en
        """,
        (nombre, hash_clave(clave), rol, int(activo), _ahora(), _ahora()),
    )
    conn.commit()
    fila = usuario_por_nombre(conn, nombre)
    return fila["id"]


def autenticar(conn: sqlite3.Connection, nombre: str, clave: str) -> sqlite3.Row | None:
    """Devuelve la fila del usuario si la clave es correcta y esta activo."""
    fila = usuario_por_nombre(conn, nombre or "")
    if fila is None or not fila["activo"]:
        return None
    if not verificar_clave(clave or "", fila["clave_hash"]):
        return None
    return fila


def sembrar_usuario_inicial(
    conn: sqlite3.Connection | None = None,
) -> tuple[str, str] | None:
    """Crea el usuario inicial si aun no hay ninguno.

    La clave sale de `TEAMS_ADMIN_CLAVE`; si no esta definida se usa `admin` y
    se avisa por stderr. Es comodo en una instalacion recien hecha y un riesgo
    en una abierta, por eso el aviso es explicito y `autenticacion.py
    --cambiar-clave` existe. Devuelve `(usuario, clave)` solo cuando lo creo.
    """
    propio = conn is None
    if propio:
        conn = conectar()
    try:
        if hay_usuarios(conn):
            return None
        # Una variable definida pero vacia (lo que deja `docker-compose` con
        # `${TEAMS_ADMIN_CLAVE:-}`) cuenta como no definida: si no, la clave por
        # defecto no se aplicaria y el alta fallaria por corta.
        nombre = (os.environ.get(VARIABLE_USUARIO_INICIAL) or "").strip() or USUARIO_INICIAL
        clave = (os.environ.get(VARIABLE_CLAVE_INICIAL) or "").strip() or CLAVE_INICIAL
        crear_usuario(conn, nombre, clave, rol=ROL_INICIAL)
        if clave == CLAVE_INICIAL:
            print(
                f"[autenticacion] Creado el usuario inicial {nombre!r} con la "
                f"clave por defecto. Cambiala con: python autenticacion.py "
                f"--cambiar-clave {nombre}",
                file=sys.stderr,
            )
        return nombre, clave
    finally:
        if propio:
            conn.close()


# --------------------------------------------------------------------------
# Sesiones
# --------------------------------------------------------------------------


def _horas_sesion() -> float:
    try:
        return float(os.environ.get(VARIABLE_HORAS_SESION, HORAS_SESION))
    except (TypeError, ValueError):
        return HORAS_SESION


def crear_sesion(
    conn: sqlite3.Connection, usuario_id: int, horas: float | None = None
) -> tuple[str, str]:
    """Genera un token opaco y lo guarda con su caducidad.

    El token no lleva datos firmados: es un valor aleatorio de 256 bits que
    solo existe en la tabla. Asi cerrar sesion lo invalida de verdad (borrando
    la fila) en vez de depender de que un HMAC no se pueda revocar.
    """
    if horas is None:
        horas = _horas_sesion()
    token = secrets.token_urlsafe(32)
    expira = datetime.now(timezone.utc) + timedelta(hours=horas)
    ahora = _ahora()
    conn.execute(
        "INSERT INTO sesiones (token, usuario_id, creado_en, expira_en) "
        "VALUES (?, ?, ?, ?)",
        (token, usuario_id, ahora, expira.isoformat(timespec="seconds")),
    )
    conn.commit()
    return token, expira.isoformat(timespec="seconds")


def resolver_sesion(conn: sqlite3.Connection, token: str) -> sqlite3.Row | None:
    """La fila de usuario de una sesion valida, o None.

    Purga de paso las sesiones caducadas: la tabla es diminuta y asi no hace
    falta un trabajo programado que limpie.
    """
    if not token:
        return None
    fila = conn.execute(
        """
        SELECT u.id, u.nombre, u.rol, u.activo, s.expira_en
        FROM sesiones s JOIN usuarios u ON u.id = s.usuario_id
        WHERE s.token = ?
        """,
        (token,),
    ).fetchone()
    if fila is None:
        return None
    try:
        expira = datetime.fromisoformat(fila["expira_en"])
    except ValueError:
        expira = datetime.now(timezone.utc)
    if expira <= datetime.now(timezone.utc) or not fila["activo"]:
        conn.execute("DELETE FROM sesiones WHERE token = ?", (token,))
        conn.commit()
        return None
    return fila


def cerrar_sesion(conn: sqlite3.Connection, token: str) -> None:
    if not token:
        return
    conn.execute("DELETE FROM sesiones WHERE token = ?", (token,))
    conn.commit()


def limpiar_sesiones(conn: sqlite3.Connection) -> int:
    """Borra las caducadas. Devuelve cuantas. Para el CLI de mantenimiento."""
    cur = conn.execute(
        "DELETE FROM sesiones WHERE expira_en <= ?", (_ahora(),)
    )
    conn.commit()
    return cur.rowcount


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _pedir_clave(nombre: str) -> str:
    primera = getpass.getpass(f"Clave para {nombre}: ")
    segunda = getpass.getpass("Repite la clave: ")
    if primera != segunda:
        raise SystemExit("Las claves no coinciden.")
    return primera


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="Usuarios y sesiones de la interfaz web."
    )
    parser.add_argument("--db", help="Ruta de la base de usuarios")
    grupo = parser.add_mutually_exclusive_group(required=True)
    grupo.add_argument("--listar", action="store_true", help="Muestra los usuarios")
    grupo.add_argument("--crear", metavar="NOMBRE", help="Crea un usuario (o le cambia la clave)")
    grupo.add_argument(
        "--cambiar-clave", metavar="NOMBRE", help="Cambia la clave de un usuario"
    )
    grupo.add_argument(
        "--limpiar-sesiones", action="store_true", help="Borra las sesiones caducadas"
    )
    args = parser.parse_args(argv)

    conn = conectar(args.db)
    try:
        if args.listar:
            filas = listar_usuarios(conn)
            if not filas:
                print("No hay usuarios. Crea uno con --crear.")
            for fila in filas:
                print(
                    f"{fila['nombre']}\t{fila['rol']}\t"
                    f"{'activo' if fila['activo'] else 'inactivo'}\t{fila['creado_en']}"
                )
        elif args.crear:
            clave = _pedir_clave(args.crear)
            crear_usuario(conn, args.crear, clave)
            print(f"Usuario {args.crear!r} creado.")
        elif args.cambiar_clave:
            if usuario_por_nombre(conn, args.cambiar_clave) is None:
                print(
                    f"No existe el usuario {args.cambiar_clave!r}. "
                    f"Usa --crear para darlo de alta.",
                    file=sys.stderr,
                )
                return 1
            clave = _pedir_clave(args.cambiar_clave)
            crear_usuario(conn, args.cambiar_clave, clave)
            print(f"Clave de {args.cambiar_clave!r} actualizada.")
        elif args.limpiar_sesiones:
            print(f"Sesiones caducadas borradas: {limpiar_sesiones(conn)}.")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
