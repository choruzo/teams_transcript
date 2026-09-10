"""
procesar_teams.py

Encadena el pipeline completo de una reunion en una sola orden:

    transcribir (+diarizar)  ->  resumir con el LLM  ->  indexar (vectorizar)

Existe por una razon muy concreta del servidor con GPU: Whisper/pyannote y el
`llama-server` del chat no caben a la vez en la VRAM. El orden real es parar
el servicio del LLM, transcribir, volver a arrancarlo, esperar a que cargue el
modelo y solo entonces pedir el resumen. Ese baile es lo que este script
automatiza; hacerlo a mano es lo que se olvida.

Uso:
    python procesar_teams.py grabaciones/20260907_090437_mixed.wav --diarize
    python procesar_teams.py grabaciones/20260907_090437_mixed.txt   # solo resumen+indice
    python procesar_teams.py grabaciones/20260907_090437_mixed.wav --desde indexar
    python procesar_teams.py grabaciones/20260907_090437_mixed.wav --simular

Notas:
  - Solo stdlib: lanza los otros scripts como SUBPROCESOS, no los importa. Es
    deliberado: un proceso que termina es lo unico que garantiza que la VRAM
    de Whisper y pyannote queda libre antes de arrancar el LLM.
  - Los servicios a parar/arrancar se indican con --servicio (repetible) o con
    la variable TEAMS_SERVICIOS_LLM (separados por comas). Sin ninguno no se
    toca systemd y la espera es una pausa a secas, que es el caso de Windows.
  - Si un paso falla, se para ahi y se sale con su mismo codigo... pero los
    servicios se vuelven a arrancar igualmente.
"""

import argparse
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import memoria

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


PASOS = ("transcribir", "resumir", "indexar")

# Un minuto: lo que tarda `llama-server` en volver a mapear el modelo en la GPU
# despues de que Whisper la haya soltado. No es un numero magico, es el que ha
# ido bien; se ajusta con --espera y por encima esta el sondeo de /models.
ESPERA_POR_DEFECTO = 60

# Cuanto se sigue esperando, sondeando, si pasada la espera el modelo aun no
# responde. Cargar un 27B desde disco frio pasa del minuto.
ESPERA_MAXIMA_POR_DEFECTO = 300

VARIABLE_SERVICIOS = "TEAMS_SERVICIOS_LLM"

BASE_URL_POR_DEFECTO = "http://localhost:4000/v1"


class ErrorPaso(RuntimeError):
    """Un paso del pipeline ha terminado con codigo distinto de cero."""

    def __init__(self, nombre: str, codigo: int):
        super().__init__(f"El paso '{nombre}' fallo con codigo {codigo}")
        self.nombre = nombre
        self.codigo = codigo


# --------------------------------------------------------------------------
# Construccion de los comandos (funciones puras: es lo que prueban los tests)
# --------------------------------------------------------------------------


def servicios_configurados(explicitos: list[str] | None) -> list[str]:
    """Unidades de systemd a parar mientras se transcribe.

    `--servicio` gana; si no, la variable de entorno; si no, ninguna, que es
    el caso del portatil Windows donde no hay nada que parar.
    """
    if explicitos:
        return [s.strip() for s in explicitos if s.strip()]
    crudo = os.environ.get(VARIABLE_SERVICIOS, "")
    return [s.strip() for s in crudo.split(",") if s.strip()]


def comando_servicio(accion: str, servicio: str, sudo: bool = True) -> list[str]:
    """`systemctl start|stop <unidad>`, con sudo salvo que ya seamos root.

    `sudo -n` (no interactivo) a proposito: si hiciera falta contrasena, este
    script puede estar corriendo desde cron sin nadie que la teclee, y es
    mejor un error inmediato que un proceso colgado esperando en el vacio.
    """
    cmd = ["systemctl", accion, servicio]
    return ["sudo", "-n", *cmd] if sudo else cmd


def usar_sudo(args) -> bool:
    if args.sin_sudo:
        return False
    # geteuid no existe en Windows; alli tampoco hay systemctl, asi que da igual.
    return getattr(os, "geteuid", lambda: 1)() != 0


def comando_transcribir(args, audio: Path) -> list[str]:
    cmd = [args.python, str(args.raiz / "transcribe_teams.py"), str(audio)]
    cmd += ["--model", args.modelo_whisper]
    cmd += ["--language", args.language]
    if args.device:
        cmd += ["--device", args.device]
    if args.output_dir:
        cmd += ["--output-dir", args.output_dir]
    if args.diarize:
        cmd.append("--diarize")
    if args.hf_token:
        cmd += ["--hf-token", args.hf_token]
    if args.num_speakers:
        cmd += ["--num-speakers", str(args.num_speakers)]
    if args.glosario:
        cmd += ["--glosario", args.glosario]
    if args.sin_glosario:
        cmd.append("--sin-glosario")
    return cmd


def comando_resumir(args, transcripcion: Path) -> list[str]:
    """El resumen SIEMPRE se pide con --sin-indice.

    No porque el indexado de `summarize_teams.py` este mal, sino porque aqui
    es un paso propio: asi se ve si falla, se puede reintentar solo
    (--desde indexar) y no se confunde con el paso caro que ya ha terminado.
    """
    cmd = [args.python, str(args.raiz / "summarize_teams.py"), str(transcripcion)]
    cmd += ["--tipo", args.tipo, "--sin-indice"]
    if args.modelo_llm:
        cmd += ["--model", args.modelo_llm]
    if args.base_url:
        cmd += ["--base-url", args.base_url]
    if args.api_key:
        cmd += ["--api-key", args.api_key]
    if args.attendees:
        cmd += ["--attendees", args.attendees]
    if args.titulo:
        cmd += ["--titulo", args.titulo]
    if args.fecha:
        cmd += ["--fecha", args.fecha]
    if args.db:
        cmd += ["--db", args.db]
    if args.glosario:
        cmd += ["--glosario", args.glosario]
    if args.sin_glosario:
        cmd.append("--sin-glosario")
    if args.sin_bd:
        cmd.append("--sin-bd")
    if args.sin_arrastres:
        cmd.append("--sin-arrastres")
    elif args.arrastres is not None:
        cmd += ["--arrastres", str(args.arrastres)]
    # Queda anotado en la BD con que modelo de Whisper se transcribio.
    cmd += ["--modelo-whisper", args.modelo_whisper]
    return cmd


def comando_indexar(args, uid: str | None) -> list[str]:
    cmd = [args.python, str(args.raiz / "indexar_teams.py")]
    if uid:
        cmd += ["--reunion", uid]
    if args.db:
        cmd += ["--db", args.db]
    if args.indice:
        cmd += ["--indice", args.indice]
    if args.modelo_embeddings:
        cmd += ["--modelo", args.modelo_embeddings]
    if args.base_url_embeddings:
        cmd += ["--base-url", args.base_url_embeddings]
    if args.api_key:
        cmd += ["--api-key", args.api_key]
    return cmd


def ruta_transcripcion(audio: Path, output_dir: str | None) -> Path:
    """El `.txt` que dejara `transcribe_teams.py` para ese audio."""
    destino = Path(output_dir) if output_dir else audio.parent
    return destino / f"{audio.stem}.txt"


def uid_de_reunion(db: str | None, transcripcion: Path) -> str | None:
    """El `uid` con el que quedo registrada la reunion recien resumida.

    Se lee de la base y no se deriva del nombre porque `_uid_disponible`
    desempata con un hash cuando dos transcripciones se llaman igual, e
    indexar el uid equivocado dejaria la reunion fuera del chat sin decir
    nada. Si no se puede leer se devuelve None: se indexa todo lo que falte,
    que es correcto aunque tarde algo mas.
    """
    try:
        conn = memoria.conectar(memoria.ruta_bd(db), solo_lectura=True)
    except Exception:  # noqa: BLE001 - sin BD legible, se indexa sin filtro
        return None
    try:
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


# --------------------------------------------------------------------------
# Ejecucion
# --------------------------------------------------------------------------


def ejecutar(cmd: list[str], nombre: str, simular: bool = False) -> None:
    print(f"\n$ {' '.join(cmd)}", flush=True)
    if simular:
        return
    codigo = subprocess.call(cmd)
    if codigo != 0:
        raise ErrorPaso(nombre, codigo)


def mover_servicios(accion: str, servicios: list[str], sudo: bool, simular: bool) -> None:
    """Para o arranca las unidades. Un fallo aqui avisa, pero no aborta.

    Si el `stop` falla porque el servicio ya estaba parado, abortar el
    procesado seria absurdo; y si falla de verdad, la transcripcion se quedara
    sin memoria y se vera en su propio error, que es mas informativo.
    """
    if not servicios:
        return
    if shutil.which("systemctl") is None and not simular:
        print(
            f"Aviso: hay servicios configurados ({', '.join(servicios)}) pero no "
            "existe systemctl en este equipo; no se toca nada.",
            file=sys.stderr,
        )
        return
    for servicio in servicios:
        cmd = comando_servicio(accion, servicio, sudo)
        print(f"\n$ {' '.join(cmd)}", flush=True)
        if simular:
            continue
        if subprocess.call(cmd) != 0:
            print(
                f"Aviso: '{accion}' de {servicio} devolvio error; se continua.",
                file=sys.stderr,
            )


def modelo_responde(base: str, clave: str | None, timeout: int = 5) -> bool:
    """GET /models: la forma mas barata de saber si el LLM ya ha cargado."""
    peticion = urllib.request.Request(base.rstrip("/") + "/models")
    if clave:
        peticion.add_header("Authorization", f"Bearer {clave}")
    try:
        with urllib.request.urlopen(peticion, timeout=timeout) as resp:
            return 200 <= resp.status < 300
    except (urllib.error.URLError, OSError, ValueError):
        return False


def esperar_al_llm(args, simular: bool) -> None:
    """La pausa del enunciado: primero fija, luego sondeando hasta el limite.

    La pausa fija sola no basta ni sobra: `systemctl start` vuelve enseguida y
    el modelo tarda en cargar, asi que despues del minuto se pregunta. Y si
    nunca contesta se sigue igualmente: el error de verdad lo dara el resumen,
    con su mensaje, en vez de este script con uno inventado.
    """
    if simular:
        print(f"\n(simulado) esperaria {args.espera} s a que el LLM cargue")
        return
    if args.espera > 0:
        print(
            f"\nEsperando {args.espera} s a que el LLM tome la GPU que acaba de "
            "soltar Whisper...",
            flush=True,
        )
        time.sleep(args.espera)
    if args.sin_sondeo:
        return
    base = args.base_url or os.environ.get("LITELLM_BASE_URL") or BASE_URL_POR_DEFECTO
    clave = args.api_key or os.environ.get("LITELLM_API_KEY")
    limite = time.monotonic() + max(args.espera_max, 0)
    avisado = False
    while True:
        if modelo_responde(base, clave):
            print(f"El modelo responde en {base}.")
            return
        if time.monotonic() >= limite:
            print(
                f"Aviso: {base} no responde tras {args.espera_max} s de sondeo. "
                "Se intenta el resumen igualmente; si el modelo no esta, fallara ahi.",
                file=sys.stderr,
            )
            return
        if not avisado:
            print(f"El modelo aun no responde en {base}; sondeando...", flush=True)
            avisado = True
        time.sleep(5)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def construir_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Encadena transcripcion, resumen e indexado de una reunion."
    )
    parser.add_argument(
        "entrada",
        help="Audio .wav a transcribir, o un .txt ya transcrito (equivale a --desde resumir)",
    )
    parser.add_argument(
        "--desde",
        choices=PASOS,
        default=None,
        help="Empieza en este paso en vez de en el primero (para reintentar)",
    )
    parser.add_argument(
        "--hasta",
        choices=PASOS,
        default="indexar",
        help="Ultimo paso a ejecutar (por defecto: indexar)",
    )
    parser.add_argument(
        "--espera",
        type=int,
        default=ESPERA_POR_DEFECTO,
        metavar="S",
        help="Segundos entre la transcripcion y el resumen "
        f"(por defecto: {ESPERA_POR_DEFECTO})",
    )
    parser.add_argument(
        "--espera-max",
        type=int,
        default=ESPERA_MAXIMA_POR_DEFECTO,
        metavar="S",
        help="Tiempo maximo sondeando /models tras la espera "
        f"(por defecto: {ESPERA_MAXIMA_POR_DEFECTO})",
    )
    parser.add_argument(
        "--sin-sondeo",
        action="store_true",
        help="No comprobar /models tras la espera: pausa fija y adelante",
    )
    parser.add_argument(
        "--servicio",
        action="append",
        default=None,
        metavar="UNIDAD",
        help="Unidad de systemd a parar mientras se transcribe y arrancar despues "
        f"(repetible; por defecto, la variable {VARIABLE_SERVICIOS})",
    )
    parser.add_argument(
        "--sin-sudo",
        action="store_true",
        help="Llamar a systemctl sin sudo (ya se ejecuta como root)",
    )
    parser.add_argument(
        "--simular",
        action="store_true",
        help="Enseña los comandos y las pausas sin ejecutar nada",
    )
    parser.add_argument(
        "--python",
        default=sys.executable,
        help="Interprete con el que lanzar los pasos (por defecto, este mismo)",
    )

    grupo = parser.add_argument_group("transcripcion")
    grupo.add_argument(
        "--modelo-whisper", default="small", help="Modelo de Whisper (por defecto: small)"
    )
    grupo.add_argument("--language", default="es", help="Idioma del audio (por defecto: es)")
    grupo.add_argument("--device", default=None, choices=["cpu", "cuda"])
    grupo.add_argument("--output-dir", default=None)
    grupo.add_argument("--diarize", action="store_true")
    grupo.add_argument("--hf-token", default=None)
    grupo.add_argument("--num-speakers", type=int, default=None)

    grupo = parser.add_argument_group("resumen")
    grupo.add_argument("--tipo", default="daily", help="Tipo de reunion (por defecto: daily)")
    grupo.add_argument("--modelo-llm", default=None, help="Modelo de chat en LiteLLM")
    grupo.add_argument("--base-url", default=None, help="URL del LLM de chat")
    grupo.add_argument("--api-key", default=None, help="API key de LiteLLM (o LITELLM_API_KEY)")
    grupo.add_argument("--attendees", default=None)
    grupo.add_argument("--titulo", default=None)
    grupo.add_argument("--fecha", default=None)
    grupo.add_argument("--arrastres", type=int, default=None, metavar="N")
    grupo.add_argument("--sin-arrastres", action="store_true")
    grupo.add_argument("--sin-bd", action="store_true")

    grupo = parser.add_argument_group("indice semantico")
    grupo.add_argument("--db", default=None, help="Ruta de meetings.db (o TEAMS_DB)")
    grupo.add_argument("--indice", default=None, help="Ruta de indice.db (o TEAMS_INDICE)")
    grupo.add_argument("--modelo-embeddings", default=None)
    grupo.add_argument("--base-url-embeddings", default=None)

    grupo = parser.add_argument_group("glosario")
    grupo.add_argument("--glosario", default=None)
    grupo.add_argument("--sin-glosario", action="store_true")

    return parser


def plan(desde: str, hasta: str) -> list[str]:
    """Pasos a ejecutar, ya recortados por --desde/--hasta."""
    return list(PASOS[PASOS.index(desde) : PASOS.index(hasta) + 1])


def main() -> None:
    args = construir_parser().parse_args()
    args.raiz = Path(__file__).resolve().parent

    entrada = Path(args.entrada)
    es_txt = entrada.suffix.lower() == ".txt"
    if args.desde is None:
        # Un .txt no se transcribe: es el resultado de haberlo hecho ya.
        args.desde = "resumir" if es_txt else "transcribir"
    if PASOS.index(args.desde) > PASOS.index(args.hasta):
        print("Error: --desde va despues de --hasta.", file=sys.stderr)
        sys.exit(1)
    if es_txt and args.desde == "transcribir":
        print(
            "Error: la entrada es un .txt, no hay nada que transcribir. Pasa el "
            ".wav o usa --desde resumir.",
            file=sys.stderr,
        )
        sys.exit(1)
    if not entrada.exists():
        print(f"Error: no se encuentra '{entrada}'", file=sys.stderr)
        sys.exit(1)

    audio = None if es_txt else entrada
    transcripcion = entrada if es_txt else ruta_transcripcion(entrada, args.output_dir)

    pasos = plan(args.desde, args.hasta)
    servicios = servicios_configurados(args.servicio)

    print(f"Pasos:     {' -> '.join(pasos)}")
    print(f"Audio:     {audio or '-'}")
    print(f"Transcrip: {transcripcion}")
    print(f"Servicios: {', '.join(servicios) or '-'}")
    print(
        f"Espera:    {args.espera} s"
        + ("" if args.sin_sondeo else f" (+ sondeo hasta {args.espera_max} s)")
    )

    try:
        if "transcribir" in pasos:
            # Parar el LLM ANTES de que Whisper pida la VRAM, no despues.
            mover_servicios("stop", servicios, usar_sudo(args), args.simular)
            try:
                ejecutar(comando_transcribir(args, audio), "transcribir", args.simular)
            finally:
                mover_servicios("start", servicios, usar_sudo(args), args.simular)
            if not args.simular and not transcripcion.exists():
                print(
                    f"Error: la transcripcion termino pero no hay '{transcripcion}'.",
                    file=sys.stderr,
                )
                sys.exit(1)
            if "resumir" in pasos:
                esperar_al_llm(args, args.simular)

        if "resumir" in pasos:
            if not args.simular and not transcripcion.exists():
                print(f"Error: no se encuentra '{transcripcion}'", file=sys.stderr)
                sys.exit(1)
            ejecutar(comando_resumir(args, transcripcion), "resumir", args.simular)

        if "indexar" in pasos:
            uid = None if args.simular else uid_de_reunion(args.db, transcripcion)
            ejecutar(comando_indexar(args, uid), "indexar", args.simular)
    except ErrorPaso as exc:
        print(f"\n{exc}. No se continua.", file=sys.stderr)
        sys.exit(exc.codigo)

    print("\nListo.")


if __name__ == "__main__":
    main()
