"""
trabajos.py

Cola de trabajos de subida y transcripcion (fase I10b).

Es el unico sitio con SQL de `datos/trabajos.db`, igual que `memoria.py` lo es
del historico y `rag.py` del indice. Ni la API ni el trabajador escriben SQL
suelto.

La base de trabajos vive aparte de `meetings.db` a proposito:

  - es estado operativo (una subida a medias, un log, un codigo de salida), no
    historico de reuniones;
  - no obliga a subir `ESQUEMA_VERSION` ni a migrar el historico para algo que
    se puede tirar y volver a crear;
  - la API puede escribir en ella aunque el historico este en solo lectura,
    igual que ya hace con `datos/usuarios.db`.

Solo stdlib. El trabajador (`trabajador_teams.py`) corre en el host, fuera de
Docker, porque necesita la GPU, el venv de Whisper y systemctl.

Uso tipico desde la API:

    conn = trabajos.conectar()
    trabajo = trabajos.crear(conn, nombre_original="daily.wav", tamano=123,
                             fecha_reunion="2026-11-10", hora_reunion="09:00")
    trabajos.anotar_parte(conn, trabajo["uid"], 0, bytes)
    trabajos.finalizar(conn, trabajo["uid"])

Y desde el trabajador:

    trabajo = trabajos.reclamar(conn)
    audio = trabajos.mover_a_grabaciones(trabajo)
    trabajos.marcar(conn, trabajo["uid"], "transcribiendo")
"""

import hashlib
import json
import os
import re
import secrets
import shutil
import sqlite3
import struct
from datetime import datetime, timedelta
from pathlib import Path

import memoria  # solo por TIPOS_REUNION: los tipos son un hecho del dominio

VERSION = "1"

ESQUEMA_VERSION = 1

VARIABLE_ENTORNO = "TEAMS_TRABAJOS_DB"
VARIABLE_SUBIDAS = "TEAMS_SUBIDAS_DIR"
VARIABLE_GRABACIONES = "TEAMS_GRABACIONES_DIR"
VARIABLE_MAXIMA = "TEAMS_SUBIDA_MAXIMA_MB"
VARIABLE_TROZO = "TEAMS_SUBIDA_TROZO_MB"

RAIZ = Path(__file__).resolve().parent
RUTA_POR_DEFECTO = RAIZ / "datos" / "trabajos.db"
SUBIDAS_POR_DEFECTO = RAIZ / "subidas"
GRABACIONES_POR_DEFECTO = RAIZ / "grabaciones"

# Dos gigas dan para unas cinco horas del audio que produce `record_teams.py`
# (96 KB/s). Es un tope de cordura, no una promesa: si algun dia hace falta
# mas, se sube con TEAMS_SUBIDA_MAXIMA_MB.
TAMANO_MAXIMO_POR_DEFECTO = 2048 * 1024 * 1024
TAMANO_TROZO_POR_DEFECTO = 16 * 1024 * 1024

# El numero de trozos no puede crecer sin limite: con 2 GB y 16 MB salen 128.
# Un valor mayor solo sirve para detectar una configuracion absurda.
MAXIMO_TROZOS = 4096

# Estados. Los que estan "en curso" son los que un trabajador que se cae deja
# colgados; al arrancar se convierten en `interrumpido`.
ESTADOS = (
    "subiendo",
    "encolado",
    "preparando",
    "transcribiendo",
    "resumiendo",
    "indexando",
    "hecho",
    "error",
    "cancelado",
    "interrumpido",
)

ESTADOS_EN_CURSO = ("preparando", "transcribiendo", "resumiendo", "indexando")
ESTADOS_TERMINALES = ("hecho", "error", "cancelado", "interrumpido")
ESTADOS_EN_COLA = ("subiendo", "encolado")

# Segundos sin latido a partir de los cuales la API dice que el trabajador no
# esta. El trabajador late cada vuelta (2 s) y como mucho cada 5 s mientras
# corre un paso, asi que 45 s es margen de sobra sin mentir.
UMBRAL_LATIDO = 45

_ESQUEMA = """
CREATE TABLE IF NOT EXISTS trabajos (
  id                 INTEGER PRIMARY KEY,
  uid                TEXT NOT NULL UNIQUE,
  estado             TEXT NOT NULL,
  nombre_original    TEXT NOT NULL,
  tamano             INTEGER NOT NULL,
  trozo              INTEGER NOT NULL,
  trozos             INTEGER NOT NULL,
  bytes_confirmados  INTEGER NOT NULL DEFAULT 0,
  sha256             TEXT,
  ruta_parcial       TEXT NOT NULL,
  audio_path         TEXT,
  reunion_uid        TEXT,
  fecha_reunion      TEXT NOT NULL,
  hora_reunion       TEXT,
  tipo               TEXT NOT NULL DEFAULT 'daily',
  opciones           TEXT NOT NULL DEFAULT '{}',
  duracion_seg       REAL,
  paso               TEXT,
  progreso           REAL,
  mensaje            TEXT,
  codigo_salida      INTEGER,
  intentos           INTEGER NOT NULL DEFAULT 0,
  cancelar           INTEGER NOT NULL DEFAULT 0,
  creado_en          TEXT NOT NULL,
  actualizado_en     TEXT NOT NULL,
  inicio_en          TEXT,
  fin_en             TEXT,
  latido_en          TEXT
);

CREATE INDEX IF NOT EXISTS trabajos_estado ON trabajos(estado, id);
CREATE INDEX IF NOT EXISTS trabajos_sha ON trabajos(sha256);

CREATE TABLE IF NOT EXISTS trabajador (
  id         INTEGER PRIMARY KEY CHECK (id = 1),
  latido_en  TEXT,
  pid        INTEGER,
  version    TEXT
);
"""

_COLUMNAS_ESCRIBIBLES = {
    "estado",
    "paso",
    "progreso",
    "mensaje",
    "codigo_salida",
    "audio_path",
    "reunion_uid",
    "sha256",
    "bytes_confirmados",
    "duracion_seg",
    "cancelar",
    "inicio_en",
    "fin_en",
    "latido_en",
    "actualizado_en",
}


class TrabajoNoEncontrado(LookupError):
    """No hay ningun trabajo con ese uid."""


class TrabajoInvalido(ValueError):
    """Lo que se ha pedido no tiene sentido (tamano, tipo, estado...)."""


class SubidaIncompleta(TrabajoInvalido):
    """Se intenta finalizar una subida a la que le faltan bytes."""


class ParteFueraDeOrden(TrabajoInvalido):
    """El trozo no es el siguiente. Lleva el que se esperaba."""

    def __init__(self, esperado: int):
        super().__init__(
            f"Trozo fuera de orden: se esperaba el {esperado}."
        )
        self.esperado = esperado


class TrabajoDuplicado(TrabajoInvalido):
    """Ese mismo audio ya se subio (mismo sha256)."""

    def __init__(self, uid: str, reunion_uid: str | None, estado: str):
        super().__init__(
            "Ese audio ya se subio antes; no se vuelve a procesar sin "
            "confirmarlo."
        )
        self.uid = uid
        self.reunion_uid = reunion_uid
        self.estado = estado


class SinEspacio(TrabajoInvalido):
    """No hay disco para aceptar la subida."""


# --------------------------------------------------------------------------
# Rutas y conexion
# --------------------------------------------------------------------------


def ruta_bd(explicita: Path | str | None = None) -> Path:
    """Argumento > TEAMS_TRABAJOS_DB > `datos/trabajos.db`."""
    if explicita:
        return Path(explicita)
    variable = os.environ.get(VARIABLE_ENTORNO)
    return Path(variable) if variable else RUTA_POR_DEFECTO


def ruta_subidas(explicita: Path | str | None = None) -> Path:
    """Donde aterrizan los `.part` mientras se suben.

    Es un directorio aparte de `grabaciones/` porque el contenedor lo monta
    escribible y `grabaciones/` se queda de solo lectura. El trabajador mueve
    el fichero ya validado a su sitio definitivo.
    """
    if explicita:
        return Path(explicita)
    variable = os.environ.get(VARIABLE_SUBIDAS)
    return Path(variable) if variable else SUBIDAS_POR_DEFECTO


def ruta_grabaciones(explicita: Path | str | None = None) -> Path:
    if explicita:
        return Path(explicita)
    variable = os.environ.get(VARIABLE_GRABACIONES)
    return Path(variable) if variable else GRABACIONES_POR_DEFECTO


def directorio_logs() -> Path:
    """Los logs viven junto a la base de trabajos, no dentro de ella.

    Un `.log` de una transcripcion son cientos de lineas de tqdm; guardarlo en
    SQLite engordaria la cola y no aportaria nada.
    """
    return ruta_bd().parent / "trabajos"


def ruta_log(uid: str) -> Path:
    return directorio_logs() / f"{uid}.log"


def ahora() -> str:
    """Hora local del servidor, sin zona.

    El resto del historico usa fechas locales sin zona (`meetings.fecha`), y
    mezclar UTC aqui haria que la cola y la web ensenaran horas distintas de
    la misma reunion.
    """
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def conectar(
    ruta: Path | str | None = None,
    *,
    entre_hilos: bool = False,
    crear: bool = True,
    espera: float = 5.0,
) -> sqlite3.Connection:
    """Abre (y crea si hace falta) la base de trabajos.

    WAL como en `meetings.db`: la API y el trabajador escriben a la vez, aunque
    sea de forma intermitente, y queremos que una lectura no bloquee una
    escritura.
    """
    path = ruta_bd(ruta)
    if not crear and not path.exists():
        raise FileNotFoundError(f"No existe la base de trabajos: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(
        path, check_same_thread=not entre_hilos, timeout=espera
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute(f"PRAGMA busy_timeout = {max(int(espera * 1000), 1000)}")
    if crear:
        conn.executescript(_ESQUEMA)
        conn.execute(f"PRAGMA user_version = {ESQUEMA_VERSION}")
        conn.commit()
    return conn


def _ahora_datetime() -> datetime:
    return datetime.now()


def _fila(conn: sqlite3.Connection, uid: str) -> dict:
    fila = conn.execute("SELECT * FROM trabajos WHERE uid = ?", (uid,)).fetchone()
    if fila is None:
        raise TrabajoNoEncontrado(f"No existe el trabajo {uid!r}.")
    return dict(fila)


def _entero_env(nombre: str, defecto: int, minimo: int = 1) -> int:
    crudo = os.environ.get(nombre)
    if not crudo:
        return defecto
    try:
        valor = int(float(crudo))
    except ValueError:
        return defecto
    return max(valor, minimo)


def tamano_maximo() -> int:
    return _entero_env(VARIABLE_MAXIMA, TAMANO_MAXIMO_POR_DEFECTO, 1024)


def tamano_trozo() -> int:
    """MB configurable para trocear; los tests la bajan para no escribir 32 MB.

    El tamano se guarda en cada trabajo al crearlo, asi que cambiarlo despues
    no rompe las subidas a medias.
    """
    return _entero_env(VARIABLE_TROZO, TAMANO_TROZO_POR_DEFECTO, 512)


def comprobar_espacio(
    tamano: int, directorio: Path | str | None = None
) -> int:
    """Devuelve el espacio libre o lanza `SinEspacio`.

    Se pide un 20 % de margen sobre el tamano declarado mas 2 GB fijos: durante
    el pipeline conviven el audio, el `.srt`, el `.txt`, el `.md` y el temporal
    que escribe Whisper, y quedarse sin disco a mitad es peor que rechazar la
    subida antes de empezar.
    """
    directorio = Path(directorio) if directorio else ruta_subidas()
    libre = shutil.disk_usage(directorio).free
    necesario = int(tamano * 1.2) + 2 * 1024**3
    if libre < necesario:
        raise SinEspacio(
            f"No hay espacio para {tamano / 1024**3:.1f} GB: quedan "
            f"{libre / 1024**3:.1f} GB libres y hacen falta "
            f"{necesario / 1024**3:.1f} GB (20 % de margen + 2 GB)."
        )
    return libre


# --------------------------------------------------------------------------
# Creacion y subida por trozos
# --------------------------------------------------------------------------


def _uid_nuevo(conn: sqlite3.Connection) -> str:
    """`AAAAMMDD_HHMMSS_xxxxxx`: ordenable, URL-safe y sin colisiones."""
    base = datetime.now().strftime("%Y%m%d_%H%M%S")
    for _ in range(20):
        uid = f"{base}_{secrets.token_hex(3)}"
        if not conn.execute("SELECT 1 FROM trabajos WHERE uid = ?", (uid,)).fetchone():
            return uid
    raise RuntimeError("No se ha podido generar un uid de trabajo unico.")


def crear(
    conn: sqlite3.Connection,
    *,
    nombre_original: str,
    tamano: int,
    fecha_reunion: str,
    hora_reunion: str | None = None,
    tipo: str = "daily",
    opciones: dict | None = None,
    trozo: int | None = None,
    ruta_subidas_directorio: Path | str | None = None,
    ahora_momento: str | None = None,
) -> dict:
    """Reserva un trabajo y crea su `.part` vacio.

    No se acepta cualquier nombre: solo `.wav`. La validacion del contenido se
    hace al finalizar, cuando ya estan todos los bytes, pero el tamano y el
    tipo se rechazan aqui para no reservar disco en balde.
    """
    nombre = Path(str(nombre_original)).name.strip()
    if not nombre:
        raise TrabajoInvalido("Falta el nombre del fichero.")
    if Path(nombre).suffix.lower() != ".wav":
        raise TrabajoInvalido(
            "Solo se aceptan ficheros .wav. Convierte el audio antes de subirlo."
        )
    if tamano <= 0:
        raise TrabajoInvalido("El fichero esta vacio.")
    maximo = tamano_maximo()
    if tamano > maximo:
        raise TrabajoInvalido(
            f"El fichero ocupa {tamano / 1024**3:.1f} GB y el maximo es "
            f"{maximo / 1024**3:.1f} GB."
        )
    if tipo not in memoria.TIPOS_REUNION:
        raise TrabajoInvalido(
            f"Tipo de reunion desconocido: {tipo!r}. "
            f"Validos: {list(memoria.TIPOS_REUNION)}."
        )
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", fecha_reunion or ""):
        raise TrabajoInvalido("La fecha de la reunion debe ser AAAAMMDD (ISO).")
    try:
        datetime.strptime(fecha_reunion, "%Y-%m-%d")
    except ValueError:
        raise TrabajoInvalido(
            f"La fecha de la reunion no existe: {fecha_reunion!r}."
        )
    if hora_reunion:
        if not re.fullmatch(r"\d{2}:\d{2}", hora_reunion):
            raise TrabajoInvalido("La hora debe ser HH:MM.")
        try:
            datetime.strptime(hora_reunion, "%H:%M")
        except ValueError:
            raise TrabajoInvalido(f"La hora no existe: {hora_reunion!r}.")

    trozo = trozo or tamano_trozo()
    if trozo <= 0:
        raise TrabajoInvalido("El tamano de trozo no puede ser cero.")
    trozos = (tamano + trozo - 1) // trozo
    if trozos > MAXIMO_TROZOS:
        raise TrabajoInvalido(
            f"El fichero necesita {trozos} trozos y el maximo es {MAXIMO_TROZOS}."
        )

    directorio = ruta_subidas(ruta_subidas_directorio)
    directorio.mkdir(parents=True, exist_ok=True)
    comprobar_espacio(tamano, directorio)

    uid = _uid_nuevo(conn)
    parcial = directorio / f"{uid}.part"
    parcial.touch()
    momento = ahora_momento or ahora()
    with conn:
        conn.execute(
            """
            INSERT INTO trabajos (
              uid, estado, nombre_original, tamano, trozo, trozos,
              bytes_confirmados, ruta_parcial, fecha_reunion, hora_reunion,
              tipo, opciones, creado_en, actualizado_en
            ) VALUES (?, 'subiendo', ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                uid,
                nombre,
                tamano,
                trozo,
                trozos,
                parcial.name,
                fecha_reunion,
                hora_reunion,
                tipo,
                json.dumps(opciones or {}, ensure_ascii=False, sort_keys=True),
                momento,
                momento,
            ),
        )
    return _fila(conn, uid)


def ruta_parcial(
    trabajo: dict, directorio: Path | str | None = None
) -> Path:
    """Resuelve el `.part` de un trabajo.

    En la base se guarda **solo el nombre**, no la ruta absoluta: la API lo ve
    en `/app/subidas` y el trabajador en `~/teams_transcript/subidas`, y una
    ruta absoluta ataria la cola a uno de los dos. Cada uno lo resuelve con su
    `TEAMS_SUBIDAS_DIR` (o el directorio explicito de los tests).

    Si la fila es de antes de este cambio y la ruta absoluta existe, se usa:
    asi un trabajo creado por una version anterior se puede reanudar en vez de
    quedarse huerfano.
    """
    guardada = Path(str(trabajo["ruta_parcial"]))
    if guardada.is_absolute() and guardada.exists():
        return guardada
    return ruta_subidas(directorio) / guardada.name


def estado_subida(conn: sqlite3.Connection, uid: str) -> dict:
    """Lo que necesita el navegador para reanudar."""
    trabajo = _fila(conn, uid)
    recibidos = int(trabajo["bytes_confirmados"])
    trozo = int(trabajo["trozo"])
    return {
        "uid": uid,
        "estado": trabajo["estado"],
        "tamano": int(trabajo["tamano"]),
        "trozo": trozo,
        "trozos": int(trabajo["trozos"]),
        "recibidos": recibidos,
        "siguiente": recibidos // trozo,
        "completo": recibidos >= int(trabajo["tamano"]),
    }


def _sha256_de(datos: bytes) -> str:
    return hashlib.sha256(datos).hexdigest()


def anotar_parte(
    conn: sqlite3.Connection,
    uid: str,
    indice: int,
    datos: bytes,
    *,
    sha256: str | None = None,
) -> dict:
    """Escribe el trozo `indice` y confirma sus bytes.

    Reglas que sostienen la reanudacion:

      - solo se acepta el trozo siguiente (`bytes_confirmados // trozo`);
      - reenviar uno ya confirmado es idempotente y no reescribe nada (si trae
        hash, se comprueba que sea el mismo);
      - si el fichero `.part` es mas largo que los bytes confirmados (un corte
        a mitad de escritura), se trunca antes de seguir;
      - si es mas corto, algo lo ha tocado por fuera y se rechaza en vez de
        inventar los bytes que faltan.

    La escritura va antes que la actualizacion de la base. Si el proceso muere
    entre las dos, el siguiente intento trunca el sobrante y reescribe.
    """
    trabajo = _fila(conn, uid)
    if trabajo["estado"] != "subiendo":
        raise TrabajoInvalido(
            f"El trabajo {uid} esta en estado {trabajo['estado']!r}; "
            "no admite mas trozos."
        )
    trozo = int(trabajo["trozo"])
    trozos = int(trabajo["trozos"])
    confirmados = int(trabajo["bytes_confirmados"])
    total = int(trabajo["tamano"])
    if indice < 0 or indice >= trozos:
        raise TrabajoInvalido(
            f"Trozo {indice} fuera de rango: el trabajo tiene {trozos}."
        )
    esperado = confirmados // trozo
    if indice < esperado:
        # Reenviar un trozo ya confirmado tiene que ser inofensivo, pero no a
        # ciegas: si el contenido no es el mismo, la subida esta mezclada y
        # seguir escribiria un fichero que no es el que se elegio. Se compara
        # siempre, aunque el cliente no mande el hash del trozo.
        previo = _leer_trozo(ruta_parcial(trabajo), indice, trozo, total)
        if _sha256_de(previo) != _sha256_de(datos):
            raise TrabajoInvalido(
                "El trozo ya estaba subido con otro contenido; "
                "empieza la subida de nuevo."
            )
        if sha256 and _sha256_de(datos) != sha256.lower():
            raise TrabajoInvalido("El trozo no coincide con el sha256 anunciado.")
        return estado_subida(conn, uid)
    if indice > esperado:
        raise ParteFueraDeOrden(esperado)

    restante = total - confirmados
    maximo_parte = min(trozo, restante)
    if len(datos) != maximo_parte:
        raise TrabajoInvalido(
            f"El trozo {indice} ocupa {len(datos)} bytes y se esperaban "
            f"{maximo_parte}."
        )
    if sha256 and _sha256_de(datos) != sha256.lower():
        raise TrabajoInvalido("El trozo no coincide con el sha256 anunciado.")

    parcial = ruta_parcial(trabajo)
    if not parcial.exists():
        raise TrabajoInvalido(
            "Ha desaparecido el fichero temporal de la subida; empieza de nuevo."
        )
    with parcial.open("r+b") as f:
        f.seek(0, os.SEEK_END)
        actual = f.tell()
        if actual > confirmados:
            f.truncate(confirmados)
        elif actual < confirmados:
            raise TrabajoInvalido(
                f"El fichero temporal tiene {actual} bytes y la base da por "
                f"buenos {confirmados}; no se puede continuar sin inventar datos."
            )
        f.seek(confirmados)
        f.write(datos)
        f.flush()
        os.fsync(f.fileno())

    with conn:
        cursor = conn.execute(
            """
            UPDATE trabajos
               SET bytes_confirmados = ?, actualizado_en = ?
             WHERE uid = ? AND bytes_confirmados = ?
            """,
            (confirmados + len(datos), ahora(), uid, confirmados),
        )
        if cursor.rowcount != 1:
            raise TrabajoInvalido(
                "Otro trozo se ha confirmado a la vez; vuelve a preguntar por "
                "el estado de la subida."
            )
    return estado_subida(conn, uid)


def _leer_trozo(parcial: Path, indice: int, trozo: int, total: int) -> bytes:
    inicio = indice * trozo
    fin = min(inicio + trozo, total)
    with parcial.open("rb") as f:
        f.seek(inicio)
        return f.read(fin - inicio)


def _sha256_archivo(ruta: Path) -> str:
    digest = hashlib.sha256()
    with ruta.open("rb") as f:
        for bloque in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(bloque)
    return digest.hexdigest()


def validar_wav(ruta: Path) -> dict:
    """Comprueba la estructura RIFF/WAVE sin decodificar el audio.

    No se usa `wave.open` porque el modulo de la stdlib rechaza los WAV
    `WAVE_FORMAT_EXTENSIBLE` (0xFFFE), que son los que escriben muchas
    grabadoras. Aqui solo hace falta saber que los trozos `fmt ` y `data`
    existen y que el tamano declarado cabe en el fichero: eso es lo que
    distingue una subida completa de una cortada. Que el audio se pueda
    decodificar lo confirma el trabajador con ffmpeg antes de transcribir.
    """
    tamano = ruta.stat().st_size
    if tamano < 44:
        raise TrabajoInvalido("El fichero es demasiado pequeno para ser un WAV.")
    with ruta.open("rb") as f:
        cabecera = f.read(12)
        if len(cabecera) != 12 or cabecera[:4] != b"RIFF" or cabecera[8:12] != b"WAVE":
            raise TrabajoInvalido(
                "No parece un WAV: falta la cabecera RIFF/WAVE."
            )
        datos = None
        fmt = None
        while True:
            cab = f.read(8)
            if len(cab) < 8:
                break
            ident, n = struct.unpack("<4sI", cab)
            inicio_cuerpo = f.tell()
            if ident == b"fmt ":
                if n < 16:
                    raise TrabajoInvalido("El bloque fmt del WAV esta truncado.")
                cuerpo = f.read(16)
                if len(cuerpo) < 16:
                    raise TrabajoInvalido("El bloque fmt del WAV esta truncado.")
                formato, canales, frecuencia, _, block_align, bits = struct.unpack(
                    "<HHIIHH", cuerpo
                )
                if formato not in (1, 3, 0xFFFE):
                    raise TrabajoInvalido(
                        f"Formato WAV no soportado ({formato}); se espera PCM."
                    )
                if canales < 1 or frecuencia < 8000 or block_align < 1:
                    raise TrabajoInvalido(
                        "El WAV declara canales o frecuencia imposibles."
                    )
                fmt = {
                    "canales": canales,
                    "frecuencia": frecuencia,
                    "bits": bits,
                    "block_align": block_align,
                }
            elif ident == b"data":
                datos = n
                inicio_datos = inicio_cuerpo
                if inicio_datos + n > tamano:
                    raise TrabajoInvalido(
                        "El WAV esta cortado: el bloque de audio declara "
                        f"{n} bytes y solo hay {tamano - inicio_datos}."
                    )
                break
            f.seek(inicio_cuerpo + n + (n & 1))
            if f.tell() > tamano:
                break

    if fmt is None:
        raise TrabajoInvalido("El WAV no tiene bloque fmt.")
    if not datos:
        raise TrabajoInvalido("El WAV no tiene audio (bloque data vacio).")
    segundos = datos / (fmt["frecuencia"] * fmt["block_align"])
    if segundos <= 0:
        raise TrabajoInvalido("El WAV dura cero segundos.")
    return {
        "duracion_seg": round(segundos, 3),
        "canales": fmt["canales"],
        "frecuencia": fmt["frecuencia"],
        "bits": fmt["bits"],
    }


def _buscar_duplicado(
    conn: sqlite3.Connection, huella: str, uid: str
) -> dict | None:
    fila = conn.execute(
        """
        SELECT uid, reunion_uid, estado
          FROM trabajos
         WHERE sha256 = ? AND uid <> ?
           AND estado NOT IN ('cancelado', 'interrumpido', 'error')
         ORDER BY id
         LIMIT 1
        """,
        (huella, uid),
    ).fetchone()
    return dict(fila) if fila else None


def finalizar(
    conn: sqlite3.Connection,
    uid: str,
    *,
    forzar: bool = False,
) -> dict:
    """Valida el fichero completo y lo pone en la cola.

    Idempotente: si ya estaba encolado (o mas alla), devuelve el trabajo tal
    cual. Eso cubre la respuesta perdida de un `finalizar` que si llego.
    """
    trabajo = _fila(conn, uid)
    if trabajo["estado"] != "subiendo":
        if trabajo["bytes_confirmados"] >= trabajo["tamano"]:
            return trabajo
        raise TrabajoInvalido(
            f"El trabajo esta en estado {trabajo['estado']!r} y no se puede "
            "finalizar."
        )
    if int(trabajo["bytes_confirmados"]) != int(trabajo["tamano"]):
        faltan = int(trabajo["tamano"]) - int(trabajo["bytes_confirmados"])
        raise SubidaIncompleta(
            f"Faltan {faltan} bytes ({faltan / 1024**2:.1f} MB) para terminar "
            "la subida."
        )

    parcial = ruta_parcial(trabajo)
    if not parcial.exists():
        raise TrabajoInvalido(
            "Ha desaparecido el fichero temporal de la subida; empieza de nuevo."
        )
    if parcial.stat().st_size != int(trabajo["tamano"]):
        raise SubidaIncompleta(
            "El fichero temporal no tiene el tamano declarado; la subida esta "
            "cortada."
        )

    info = validar_wav(parcial)
    huella = _sha256_archivo(parcial)
    duplicado = _buscar_duplicado(conn, huella, uid)
    if duplicado and not forzar:
        raise TrabajoDuplicado(
            duplicado["uid"], duplicado["reunion_uid"], duplicado["estado"]
        )

    with conn:
        conn.execute(
            """
            UPDATE trabajos
               SET estado = 'encolado', sha256 = ?, duracion_seg = ?,
                   actualizado_en = ?
             WHERE uid = ? AND estado = 'subiendo'
            """,
            (huella, info["duracion_seg"], ahora(), uid),
        )
    return _fila(conn, uid)


# --------------------------------------------------------------------------
# Lectura
# --------------------------------------------------------------------------


def detalle(conn: sqlite3.Connection, uid: str) -> dict:
    return _fila(conn, uid)


def listar(
    conn: sqlite3.Connection,
    *,
    limite: int = 50,
    estados: list[str] | None = None,
) -> list[dict]:
    limite = max(1, min(int(limite), 200))
    if estados:
        marcadores = ", ".join("?" for _ in estados)
        filas = conn.execute(
            f"SELECT * FROM trabajos WHERE estado IN ({marcadores}) "
            "ORDER BY id DESC LIMIT ?",
            (*estados, limite),
        ).fetchall()
    else:
        filas = conn.execute(
            "SELECT * FROM trabajos ORDER BY id DESC LIMIT ?", (limite,)
        ).fetchall()
    return [dict(f) for f in filas]


def contar_en_cola(conn: sqlite3.Connection) -> int:
    fila = conn.execute(
        "SELECT COUNT(*) AS n FROM trabajos WHERE estado = 'encolado'"
    ).fetchone()
    return int(fila["n"])


def contar_en_curso(conn: sqlite3.Connection) -> int:
    fila = conn.execute(
        f"SELECT COUNT(*) AS n FROM trabajos WHERE estado IN "
        f"({', '.join('?' for _ in ESTADOS_EN_CURSO)})",
        ESTADOS_EN_CURSO,
    ).fetchone()
    return int(fila["n"])


def leer_log(uid: str, maximo: int = 8192) -> tuple[str, int]:
    """Las ultimas lineas del log y su tamano total.

    Se lee del final para que una transcripcion de una hora no haga que cada
    evento SSE cargue el fichero entero.
    """
    ruta = ruta_log(uid)
    try:
        tamano = ruta.stat().st_size
        with ruta.open("rb") as f:
            if tamano > maximo:
                f.seek(tamano - maximo)
            datos = f.read()
    except OSError:
        return "", 0
    return datos.decode("utf-8", errors="replace"), tamano


# --------------------------------------------------------------------------
# Cola y trabajador
# --------------------------------------------------------------------------


def reclamar(conn: sqlite3.Connection) -> dict | None:
    """Toma el trabajo encolado mas antiguo. Uno a la vez.

    El `UPDATE ... WHERE estado = 'encolado'` es la barrera: si dos
    trabajadores despertaran a la vez, solo uno cambiaria la fila.
    """
    with conn:
        fila = conn.execute(
            "SELECT uid FROM trabajos WHERE estado = 'encolado' "
            "ORDER BY id LIMIT 1"
        ).fetchone()
        if fila is None:
            return None
        momento = ahora()
        cursor = conn.execute(
            """
            UPDATE trabajos
               SET estado = 'preparando', inicio_en = ?, latido_en = ?,
                   actualizado_en = ?, intentos = intentos + 1,
                   mensaje = NULL, codigo_salida = NULL, fin_en = NULL
             WHERE uid = ? AND estado = 'encolado'
            """,
            (momento, momento, momento, fila["uid"]),
        )
        if cursor.rowcount != 1:
            return None
        uid = fila["uid"]
    return _fila(conn, uid)


def marcar(
    conn: sqlite3.Connection,
    uid: str,
    estado: str,
    **campos,
) -> dict:
    """Cambia el estado y los campos permitidos, en una sola escritura."""
    if estado not in ESTADOS:
        raise TrabajoInvalido(f"Estado desconocido: {estado!r}.")
    desconocidos = set(campos) - _COLUMNAS_ESCRIBIBLES
    if desconocidos:
        raise TrabajoInvalido(
            f"Campos no escribibles: {sorted(desconocidos)}."
        )
    _fila(conn, uid)  # 404 antes de construir el SQL
    asignaciones = ["estado = ?", "actualizado_en = ?"]
    valores: list = [estado, ahora()]
    for clave, valor in campos.items():
        asignaciones.append(f"{clave} = ?")
        valores.append(valor)
    valores.append(uid)
    with conn:
        conn.execute(
            f"UPDATE trabajos SET {', '.join(asignaciones)} WHERE uid = ?",
            valores,
        )
    return _fila(conn, uid)


def latido(conn: sqlite3.Connection, uid: str | None = None) -> None:
    """Marca que el trabajador sigue vivo, y opcionalmente que un trabajo no se
    ha quedado colgado."""
    momento = ahora()
    with conn:
        conn.execute(
            """
            INSERT INTO trabajador (id, latido_en, pid, version)
            VALUES (1, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
              latido_en = excluded.latido_en,
              pid = excluded.pid,
              version = excluded.version
            """,
            (momento, os.getpid(), VERSION),
        )
        if uid:
            conn.execute(
                "UPDATE trabajos SET latido_en = ?, actualizado_en = ? WHERE uid = ?",
                (momento, momento, uid),
            )


def estado_trabajador(conn: sqlite3.Connection) -> dict | None:
    fila = conn.execute("SELECT * FROM trabajador WHERE id = 1").fetchone()
    return dict(fila) if fila else None


def trabajador_vivo(conn: sqlite3.Connection) -> tuple[bool, dict | None]:
    fila = estado_trabajador(conn)
    if not fila or not fila.get("latido_en"):
        return False, fila
    try:
        visto = datetime.strptime(fila["latido_en"], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return False, fila
    return (_ahora_datetime() - visto).total_seconds() <= UMBRAL_LATIDO, fila


def marcar_interrumpidos(conn: sqlite3.Connection) -> list[str]:
    """Lo que dejo colgado un trabajador que se cayo.

    Se llama al arrancar, antes de reclamar nada. No se reintenta solo: un
    fallo del pipeline puede necesitar que alguien mire el log, y volver a
    lanzar 40 minutos de GPU en bucle no es una recuperacion.
    """
    marcadores = ", ".join("?" for _ in ESTADOS_EN_CURSO)
    with conn:
        filas = conn.execute(
            f"SELECT uid FROM trabajos WHERE estado IN ({marcadores})",
            ESTADOS_EN_CURSO,
        ).fetchall()
        uids = [f["uid"] for f in filas]
        if uids:
            momento = ahora()
            conn.execute(
                f"""
                UPDATE trabajos
                   SET estado = 'interrumpido',
                       mensaje = COALESCE(mensaje, ?),
                       fin_en = ?, actualizado_en = ?
                 WHERE estado IN ({marcadores})
                """,
                (
                    "El trabajador se detuvo mientras procesaba este trabajo.",
                    momento,
                    momento,
                    *ESTADOS_EN_CURSO,
                ),
            )
    return uids


def cancelar(conn: sqlite3.Connection, uid: str) -> dict:
    """Cancela una subida o un trabajo en cola; pide parar uno en curso.

    Un trabajo en ejecucion no se puede matar desde la API: el trabajador es
    el unico que conoce el PID del pipeline, y es quien levanta el LLM en su
    `finally`. Aqui solo se pone la bandera `cancelar`.
    """
    trabajo = _fila(conn, uid)
    if trabajo["estado"] == "cancelado":
        return trabajo
    if trabajo["estado"] in ("hecho", "error", "interrumpido"):
        raise TrabajoInvalido(
            f"El trabajo ya ha terminado ({trabajo['estado']}); no se cancela."
        )
    momento = ahora()
    if trabajo["estado"] in ESTADOS_EN_COLA:
        parcial = ruta_parcial(trabajo)
        with conn:
            conn.execute(
                """
                UPDATE trabajos
                   SET estado = 'cancelado', cancelar = 0, fin_en = ?,
                       actualizado_en = ?
                 WHERE uid = ?
                """,
                (momento, momento, uid),
            )
        if trabajo["estado"] == "subiendo":
            try:
                parcial.unlink(missing_ok=True)
            except OSError:
                pass
        return _fila(conn, uid)
    with conn:
        conn.execute(
            "UPDATE trabajos SET cancelar = 1, actualizado_en = ? WHERE uid = ?",
            (momento, uid),
        )
    return _fila(conn, uid)


def reintentar(conn: sqlite3.Connection, uid: str) -> dict:
    trabajo = _fila(conn, uid)
    if trabajo["estado"] not in ("error", "interrumpido", "cancelado"):
        raise TrabajoInvalido(
            f"El trabajo esta en estado {trabajo['estado']!r}; solo se "
            "reintenta lo que fallo, se interrumpio o se cancelo."
        )
    audio = trabajo.get("audio_path")
    if not audio and not ruta_parcial(trabajo).exists():
        # Ni movido ni en `subidas/`: no hay nada que procesar. Si `audio_path`
        # viene puesto no se comprueba aqui: la API puede no ver la ruta del
        # host (vive en otro contenedor) y es el trabajador quien lo sabe.
        raise TrabajoInvalido(
            "No queda el audio de este trabajo; hay que volver a subirlo."
        )
    momento = ahora()
    with conn:
        conn.execute(
            """
            UPDATE trabajos
               SET estado = 'encolado', cancelar = 0, mensaje = NULL,
                   codigo_salida = NULL, paso = NULL, progreso = 0,
                   inicio_en = NULL, fin_en = NULL, actualizado_en = ?
             WHERE uid = ?
            """,
            (momento, uid),
        )
    return _fila(conn, uid)


# --------------------------------------------------------------------------
# Ficheros
# --------------------------------------------------------------------------


def nombre_destino(trabajo: dict, directorio: Path | str) -> Path:
    """Nombre definitivo del audio, con el patron del que depende el pipeline.

    `inferir_fecha` lee `AAAAMMDD` del nombre, y `uid_transcripcion` usa el
    stem, asi que el nombre no es cosmetico. Si el original ya lo cumple se
    respeta; si no, se genera con la fecha y la hora elegidas. Un nombre ya
    ocupado se sufija: dos reuniones distintas no pueden compartir ruta, o el
    reproceso reconciliaria una con la otra.
    """
    directorio = Path(directorio)
    original = Path(str(trabajo["nombre_original"])).stem
    encaje = re.fullmatch(r"(\d{8})_(\d{6})(?:_mixed)?", original)
    if encaje:
        base = f"{encaje.group(1)}_{encaje.group(2)}_mixed"
    else:
        fecha = str(trabajo["fecha_reunion"]).replace("-", "")
        hora = (trabajo.get("hora_reunion") or "00:00").replace(":", "")
        base = f"{fecha}_{hora}00_mixed"
    candidato = directorio / f"{base}.wav"
    if not candidato.exists():
        return candidato
    for numero in range(2, 1000):
        candidato = directorio / f"{base}-{numero}.wav"
        if not candidato.exists():
            return candidato
    return directorio / f"{base}-{trabajo['uid']}.wav"


def mover_a_grabaciones(
    trabajo: dict, directorio: Path | str | None = None
) -> Path:
    """Mueve el temporal validado a `grabaciones/` con su nombre definitivo.

    `os.replace` es atomico en el mismo sistema de ficheros, que es lo normal
    porque `subidas/` y `grabaciones/` cuelgan del mismo repo. Si algun dia se
    separan, se cae a `shutil.move`, que no es atomico pero no pierde el
    fichero.
    """
    destino_dir = Path(directorio) if directorio else ruta_grabaciones()
    destino_dir.mkdir(parents=True, exist_ok=True)
    origen = ruta_parcial(trabajo)
    if not origen.exists():
        raise TrabajoInvalido(
            "No esta el fichero temporal de la subida; hay que volver a subirlo."
        )
    destino = nombre_destino(trabajo, destino_dir)
    try:
        os.replace(origen, destino)
    except OSError:
        shutil.move(str(origen), str(destino))
    return destino


def limpiar_parciales(
    conn: sqlite3.Connection, *, horas: int = 24, maximo: int | None = None
) -> int:
    """Tira subidas abandonadas (pestana cerrada, navegador apagado).

    `maximo` existe para los tests: con 0 limpia todo lo que ya este fuera de
    plazo, sin tener que esperar 24 horas.
    """
    limite = _ahora_datetime() - timedelta(hours=horas)
    with conn:
        filas = conn.execute(
            "SELECT uid, ruta_parcial, actualizado_en FROM trabajos "
            "WHERE estado = 'subiendo'"
        ).fetchall()
        borrados = 0
        for fila in filas:
            if maximo is not None and borrados >= maximo:
                break
            try:
                visto = datetime.strptime(fila["actualizado_en"], "%Y-%m-%d %H:%M:%S")
            except (TypeError, ValueError):
                continue
            if visto >= limite:
                continue
            conn.execute("DELETE FROM trabajos WHERE uid = ?", (fila["uid"],))
            try:
                ruta_parcial(fila).unlink(missing_ok=True)
            except OSError:
                pass
            borrados += 1
    return borrados
