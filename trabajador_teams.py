"""
trabajador_teams.py

Trabajador de la cola de subidas (fase I10b). Corre **en el host**, fuera de
Docker, como servicio de systemd del usuario que ya tiene el venv de Whisper:

    /home/agent/teams_transcript/venv/bin/python trabajador_teams.py \
        --servicio llama-qwen3-8.service

Por que no va dentro del contenedor:

  - necesita la GPU y el venv con `openai-whisper`/`pyannote`, que no estan en
    la imagen de la API;
  - necesita `systemctl stop/start` del `llama-server` para liberar la VRAM;
  - el contenedor monta `grabaciones/` en solo lectura, y el trabajador es
    quien mueve alli el fichero ya validado.

Un trabajo a la vez: la GPU no se comparte. El trabajador no sabe nada de HTTP;
solo mira la cola, ejecuta `procesar_teams.py` como subproceso y deja el estado
y el log para que la web los lea.

Solo stdlib. La logica del pipeline sigue en `procesar_teams.py`: aqui no se
duplica el baile de parar/arrancar el LLM.
"""

import argparse
import json
import os
import queue
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import memoria
import rag
import trabajos

RAIZ = Path(__file__).resolve().parent

# Se pone a True con SIGTERM/SIGINT. El bucle lo mira entre lineas y en las
# esperas, y mata el grupo de procesos del pipeline. No se sale de golpe: hay
# que arrancar el LLM antes de terminar.
PARANDO = False

# Cada cuanto se comprueba la bandera de cancelacion y se escribe el latido
# mientras el pipeline esta callado (un resumen puede tardar minutos).
INTERVALO_COMPROBACION = 2.0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Procesa la cola de grabaciones subidas por la web."
    )
    parser.add_argument(
        "--db-cola",
        default=None,
        help="Base de la cola (o TEAMS_TRABAJOS_DB; por defecto datos/trabajos.db)",
    )
    parser.add_argument(
        "--db", default=None, help="meetings.db (o TEAMS_DB)"
    )
    parser.add_argument(
        "--indice", default=None, help="indice.db (o TEAMS_INDICE)"
    )
    parser.add_argument(
        "--subidas", default=None, help="Directorio de .part (o TEAMS_SUBIDAS_DIR)"
    )
    parser.add_argument(
        "--grabaciones",
        default=None,
        help="Directorio de audio final (o TEAMS_GRABACIONES_DIR)",
    )
    parser.add_argument(
        "--pipeline",
        default=str(RAIZ / "procesar_teams.py"),
        help="Script a ejecutar por cada trabajo (para tests se sustituye)",
    )
    parser.add_argument(
        "--python",
        default=sys.executable,
        help="Interprete de los pasos del pipeline (por defecto, este)",
    )
    parser.add_argument(
        "--servicio",
        action="append",
        default=None,
        metavar="UNIDAD",
        help=(
            "Unidad systemd que se para mientras se transcribe (repetible). "
            "Si no se pasa, se usa TEAMS_SERVICIOS_LLM."
        ),
    )
    parser.add_argument(
        "--sin-sudo",
        action="store_true",
        help="Llamar a systemctl sin sudo (cuando ya se ejecuta como root)",
    )
    parser.add_argument(
        "--modelo-whisper",
        default="large-v3",
        help="Modelo por defecto si el trabajo no trae uno (large-v3)",
    )
    parser.add_argument(
        "--language", default="es", help="Idioma del audio (por defecto es)"
    )
    parser.add_argument(
        "--sin-diarizar",
        action="store_true",
        help="Ignora la opcion del trabajo y no diariza nunca",
    )
    parser.add_argument(
        "--espera",
        type=int,
        default=None,
        help="Segundos de espera al LLM (por defecto, los de procesar_teams.py)",
    )
    parser.add_argument(
        "--reintentos-llm",
        type=int,
        default=2,
        help=(
            "Reintentos del resumen cuando LiteLLM responde 503 porque aun "
            "carga el modelo (0 los desactiva)"
        ),
    )
    parser.add_argument(
        "--espera-reintento",
        type=int,
        default=60,
        help="Segundos entre reintentos del resumen por modelo cargando",
    )
    parser.add_argument(
        "--intervalo",
        type=float,
        default=2.0,
        help="Segundos entre consultas a la cola cuando esta vacia",
    )
    parser.add_argument(
        "--limpiar-horas",
        type=float,
        default=24.0,
        help="Antiguedad para tirar subidas abandonadas (0 no limpia)",
    )
    parser.add_argument(
        "--una-vez",
        action="store_true",
        help="Procesa como mucho un trabajo y sale (tests y diagnosticos)",
    )
    parser.add_argument(
        "--simular",
        action="store_true",
        help="No ejecuta el pipeline; solo ensena lo que haria",
    )
    return parser


def _servicios(args) -> list[str]:
    if args.servicio:
        return [s.strip() for s in args.servicio if s.strip()]
    crudo = os.environ.get("TEAMS_SERVICIOS_LLM", "")
    return [s.strip() for s in crudo.split(",") if s.strip()]


def _opciones(trabajo: dict) -> dict:
    try:
        datos = json.loads(trabajo.get("opciones") or "{}")
    except (TypeError, ValueError):
        return {}
    return datos if isinstance(datos, dict) else {}


def modelo_de(trabajo: dict, por_defecto: str) -> str:
    modelo = str(_opciones(trabajo).get("modelo") or por_defecto)
    return modelo


def diarizar_de(trabajo: dict, sin_diarizar: bool) -> bool:
    if sin_diarizar:
        return False
    return bool(_opciones(trabajo).get("diarizar", True))


def paso_inicial(audio: Path) -> str:
    """Donde reanudar sin repetir lo caro.

    Si hay transcripcion se empieza por el resumen; si ademas hay resumen, solo
    falta indexar. Los ficheros vacios no cuentan: un fallo a mitad de escritura
    no puede hacer creer que el paso esta hecho.
    """
    txt = audio.with_suffix(".txt")
    if not txt.exists() or txt.stat().st_size == 0:
        return "transcribir"
    resumen = audio.with_name(f"{audio.stem}_resumen.md")
    if not resumen.exists() or resumen.stat().st_size == 0:
        return "resumir"
    return "indexar"


def construir_comando(
    args,
    trabajo: dict,
    audio: Path,
    desde: str,
    *,
    db: Path,
    indice: Path,
) -> list[str]:
    """La orden que se le pasa a `procesar_teams.py`. Funcion pura: se prueba."""
    opciones = _opciones(trabajo)
    cmd = [
        args.python,
        str(args.pipeline),
        str(audio),
        "--desde",
        desde,
        "--hasta",
        "indexar",
        "--language",
        args.language,
        "--modelo-whisper",
        modelo_de(trabajo, args.modelo_whisper),
        "--tipo",
        trabajo["tipo"],
        "--db",
        str(db),
        "--indice",
        str(indice),
    ]
    if trabajo.get("fecha_reunion"):
        cmd += ["--fecha", str(trabajo["fecha_reunion"])]
    titulo = opciones.get("titulo")
    if titulo:
        cmd += ["--titulo", str(titulo)]
    if diarizar_de(trabajo, args.sin_diarizar):
        cmd.append("--diarize")
    for servicio in _servicios(args):
        cmd += ["--servicio", servicio]
    if args.sin_sudo:
        cmd.append("--sin-sudo")
    if args.espera is not None:
        cmd += ["--espera", str(args.espera)]
    return cmd


def _paso_de_linea(linea: str) -> str | None:
    """`procesar_teams.py` imprime el comando de cada paso antes de lanzarlo."""
    if "transcribe_teams.py" in linea:
        return "transcribiendo"
    if "summarize_teams.py" in linea:
        return "resumiendo"
    if "indexar_teams.py" in linea:
        return "indexando"
    if "Esperando" in linea or "sondeando" in linea or "modelo responde" in linea:
        return "esperando_llm"
    return None


def _bombear(flujo, cola: queue.Queue) -> None:
    """Hilo lector: `for linea in stdout` bloquea y no dejaria mirar la cola."""
    try:
        for linea in flujo:
            cola.put(linea.rstrip("\r\n"))
    finally:
        cola.put(None)


def _matar(proceso: subprocess.Popen) -> None:
    """Mata el grupo entero: `procesar_teams` lanza a su vez Whisper/pyannote."""
    if proceso.poll() is not None:
        return
    if not hasattr(os, "killpg"):  # Windows: el trabajador real corre en Linux
        proceso.terminate()
        try:
            proceso.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proceso.kill()
        return
    try:
        os.killpg(os.getpgid(proceso.pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    try:
        proceso.wait(timeout=10)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proceso.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


def _cancelado(conn, uid: str) -> bool:
    try:
        return bool(trabajos.detalle(conn, uid)["cancelar"])
    except trabajos.TrabajoNoEncontrado:
        return True


def _uid_de_reunion(db: Path, audio: Path) -> str | None:
    """El uid lo decide la base, no el nombre (puede llevar un hash de desempate)."""
    try:
        conn = memoria.conectar(db, solo_lectura=True)
    except Exception:  # noqa: BLE001 - sin historico legible, se indexa sin filtro
        return None
    try:
        # La base guarda la ruta del `.txt`, no la del audio: `summarize_teams`
        # crea la transcripcion al lado y es la que identifica la reunion.
        transcripcion = audio.with_suffix(".txt")
        meeting_id = memoria.id_reunion_por_transcripcion(
            conn, str(transcripcion.resolve())
        )
        if meeting_id is None:
            return None
        fila = conn.execute(
            "SELECT uid FROM meetings WHERE id = ?", (meeting_id,)
        ).fetchone()
        return fila["uid"] if fila else None
    except Exception:  # noqa: BLE001
        return None
    finally:
        conn.close()


def asegurar_servicios(servicios: list[str], sin_sudo: bool) -> None:
    """Deja el LLM arrancado pase lo que pase.

    Se llama con `start` a secas y no se pregunta antes si esta activo: la
    regla de sudo del servidor permite `start`/`stop` pero no `is-active`, y
    arrancar un servicio ya arrancado es inofensivo. Si el pipeline murio de
    golpe, este es el unico sitio que lo levanta.
    """
    for servicio in servicios:
        cmd = ["systemctl", "start", servicio]
        if not sin_sudo and getattr(os, "geteuid", lambda: 1)() != 0:
            cmd = ["sudo", "-n", *cmd]
        try:
            resultado = subprocess.run(
                cmd, capture_output=True, text=True, timeout=90
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            print(f"[trabajador] No se pudo arrancar {servicio}: {exc}", file=sys.stderr)
            continue
        if resultado.returncode != 0:
            print(
                f"[trabajador] {servicio} devolvio {resultado.returncode}: "
                f"{resultado.stderr.strip()}",
                file=sys.stderr,
            )


def _ultimas_lineas(uid: str, maximo: int = 12) -> str:
    texto, _ = trabajos.leer_log(uid, maximo=8192)
    lineas = [linea for linea in texto.splitlines() if linea.strip()]
    return "\n".join(lineas[-maximo:])[-2000:]


def _fallo_transitorio_de_llm(uid: str) -> bool:
    """Si el ultimo intento murio porque LiteLLM aun cargaba el modelo.

    `procesar_teams.py` espera y sondea `/models`, pero con LiteLLM delante ese
    endpoint puede responder 200 mientras el backend sigue cargando. El primer
    resumen se come un 503 *Loading model*; unos segundos despues funciona. Se
    mira solo el tramo del ultimo intento para no reintentar por un 503 viejo.
    """
    texto, _ = trabajos.leer_log(uid, maximo=16384)
    ultimo = texto.rsplit(f"== {uid} ==", 1)[-1]
    marcadores = (
        "Loading model",
        "ServiceUnavailable",
        "HTTP 503",
        "Connection refused",
    )
    return any(marcador in ultimo for marcador in marcadores)


def _lanzar_pipeline(conn, uid, trabajo, cmd, estado, args):
    """Un intento del pipeline. Devuelve `(codigo_salida, cancelado)`."""
    log_path = trabajos.ruta_log(uid)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as log:
        log.write(f"\n== {uid} ==\n$ {' '.join(cmd)}\n")
        log.flush()
        if args.simular:
            log.write("(simulado: no se ejecuta)\n")
            log.flush()
            return 0, False
        proceso = subprocess.Popen(
            cmd,
            cwd=str(RAIZ),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            start_new_session=True,
        )
        cola: queue.Queue = queue.Queue()
        hilo = threading.Thread(
            target=_bombear, args=(proceso.stdout, cola), daemon=True
        )
        hilo.start()
        estado_actual = estado
        ultima_comprobacion = time.monotonic()
        cancelado = False
        while True:
            try:
                linea = cola.get(timeout=INTERVALO_COMPROBACION)
            except queue.Empty:
                linea = None
            if linea is not None:
                log.write(linea + "\n")
                log.flush()
                paso = _paso_de_linea(linea)
                if paso and paso != trabajo.get("paso"):
                    if paso in ("transcribiendo", "resumiendo", "indexando"):
                        estado_actual = paso
                    trabajo = trabajos.marcar(
                        conn, uid, estado_actual, paso=paso
                    )
            ahora_monotonic = time.monotonic()
            if ahora_monotonic - ultima_comprobacion >= INTERVALO_COMPROBACION:
                ultima_comprobacion = ahora_monotonic
                trabajos.latido(conn, uid)
                if PARANDO or _cancelado(conn, uid):
                    cancelado = True
                    _matar(proceso)
                    break
            if linea is None and proceso.poll() is not None:
                break
        codigo = proceso.wait()
        if proceso.stdout is not None:
            proceso.stdout.close()
        return codigo, cancelado


def ejecutar(conn, trabajo: dict, args) -> dict:
    """Procesa un trabajo ya reclamado. Devuelve la fila final."""
    uid = trabajo["uid"]
    servicios = _servicios(args)
    db = memoria.ruta_bd(args.db)
    indice = rag.ruta_indice(args.indice)

    if not trabajo.get("audio_path"):
        try:
            audio = trabajos.mover_a_grabaciones(trabajo, args.grabaciones)
        except Exception as exc:  # noqa: BLE001 - queda como error del trabajo
            return trabajos.marcar(
                conn,
                uid,
                "error",
                mensaje=f"No se pudo preparar el audio: {exc}",
                fin_en=trabajos.ahora(),
            )
        trabajo = trabajos.marcar(
            conn, uid, "preparando", audio_path=str(audio), paso="preparando"
        )
    audio = Path(trabajo["audio_path"])
    if not audio.exists():
        return trabajos.marcar(
            conn,
            uid,
            "error",
            mensaje=(
                f"No esta el audio en {audio}; el trabajo no se puede "
                "reanudar sin volver a subirlo."
            ),
            fin_en=trabajos.ahora(),
        )

    desde = paso_inicial(audio)
    intentos_llm = 0
    codigo = None
    cancelado = False
    while True:
        estado = {
            "transcribir": "transcribiendo",
            "resumir": "resumiendo",
            "indexar": "indexando",
        }[desde]
        trabajo = trabajos.marcar(conn, uid, estado, paso=estado)
        cmd = construir_comando(args, trabajo, audio, desde, db=db, indice=indice)
        try:
            codigo, cancelado = _lanzar_pipeline(
                conn, uid, trabajo, cmd, estado, args
            )
        except Exception as exc:  # noqa: BLE001 - el trabajo no tumba el bucle
            return trabajos.marcar(
                conn,
                uid,
                "error",
                mensaje=f"Fallo del trabajador: {exc}",
                fin_en=trabajos.ahora(),
            )
        finally:
            if servicios:
                asegurar_servicios(servicios, args.sin_sudo)

        if cancelado or codigo == 0:
            break
        if (
            _fallo_transitorio_de_llm(uid)
            and intentos_llm < args.reintentos_llm
            and audio.with_suffix(".txt").exists()
        ):
            intentos_llm += 1
            trabajos.marcar(
                conn,
                uid,
                "resumiendo",
                paso="esperando_llm",
                mensaje=(
                    f"El LLM aun estaba cargando el modelo; reintento "
                    f"{intentos_llm}/{args.reintentos_llm} en "
                    f"{args.espera_reintento} s."
                ),
            )
            time.sleep(args.espera_reintento)
            desde = "resumir"
            continue
        break

    if cancelado:
        mensaje = (
            "El trabajador se detuvo; el trabajo queda interrumpido."
            if PARANDO
            else "Cancelado por el usuario."
        )
        return trabajos.marcar(
            conn,
            uid,
            "interrumpido" if PARANDO else "cancelado",
            mensaje=mensaje,
            fin_en=trabajos.ahora(),
        )
    if codigo == 0:
        reunion_uid = _uid_de_reunion(db, audio)
        return trabajos.marcar(
            conn,
            uid,
            "hecho",
            paso="hecho",
            progreso=1,
            codigo_salida=0,
            reunion_uid=reunion_uid,
            mensaje=None,
            fin_en=trabajos.ahora(),
        )
    return trabajos.marcar(
        conn,
        uid,
        "error",
        mensaje=_ultimas_lineas(uid),
        codigo_salida=codigo,
        fin_en=trabajos.ahora(),
    )


def _senal(signum, frame) -> None:  # noqa: ARG001 - firma de signal
    global PARANDO
    PARANDO = True


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGTERM, _senal)
        signal.signal(signal.SIGINT, _senal)

    conn = trabajos.conectar(args.db_cola)
    try:
        trabajos.latido(conn)
        interrumpidos = trabajos.marcar_interrumpidos(conn)
        if interrumpidos:
            print(
                f"[trabajador] {len(interrumpidos)} trabajo(s) quedaron "
                "interrumpidos; no se reintentan solos.",
                file=sys.stderr,
            )
            # Si se cayo a mitad de una transcripcion, el LLM puede haber
            # quedado parado. Se arranca sin preguntar: es idempotente.
            asegurar_servicios(_servicios(args), args.sin_sudo)

        while not PARANDO:
            trabajos.latido(conn)
            if args.limpiar_horas > 0:
                trabajos.limpiar_parciales(conn, horas=args.limpiar_horas)
            trabajo = trabajos.reclamar(conn)
            if trabajo is None:
                if args.una_vez:
                    break
                time.sleep(args.intervalo)
                continue
            print(f"[trabajador] Procesando {trabajo['uid']}", flush=True)
            final = ejecutar(conn, trabajo, args)
            print(
                f"[trabajador] {trabajo['uid']} -> {final['estado']}", flush=True
            )
            if args.una_vez:
                break
    finally:
        try:
            trabajos.latido(conn)
        except Exception:  # noqa: BLE001 - cerrando
            pass
        conn.close()
        # Red de seguridad: si se para el servicio con un trabajo en curso, el
        # `finally` de `ejecutar` ya lo ha hecho, pero un SIGTERM en la espera
        # de la cola no pasaria por ahi.
        if _servicios(args):
            asegurar_servicios(_servicios(args), args.sin_sudo)
    return 0


if __name__ == "__main__":
    sys.exit(main())
