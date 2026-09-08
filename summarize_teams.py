"""
summarize_teams.py

Genera un resumen de una transcripcion (el .txt generado por
transcribe_teams.py, con o sin diarizacion) usando un LLM local servido a
traves de LiteLLM (API compatible con OpenAI).

Uso:
    python summarize_teams.py grabaciones\20260907_090437_mixed.txt
    python summarize_teams.py archivo.txt --model qwen3.8-27b
    python summarize_teams.py archivo.txt --base-url http://localhost:4000/v1

Genera junto a la transcripcion:
  <nombre>_resumen.md

Notas:
  - Requiere la API key de LiteLLM en la variable de entorno LITELLM_API_KEY
    (o pasarla con --api-key).
  - Si la transcripcion viene de --diarize, los hablantes apareceran como
    SPEAKER_00, SPEAKER_01, etc. (etiquetas genericas, sin nombres reales).
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

import glosario as glosario_mod

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

DEFAULT_MODEL = "qwen3.6-35b-a3b"
DEFAULT_BASE_URL = "http://localhost:4000/v1"

SYSTEM_PROMPT = (
    "Eres un asistente que analiza transcripciones de reuniones diarias "
    "(dailys) de un equipo de desarrollo de software, generadas por "
    "transcripcion automatica (Whisper) y, opcionalmente, diarizacion de "
    "hablantes (pyannote). Ten en cuenta sus limitaciones:\n\n"
    "- Los errores de transcripcion son habituales: nombres propios, siglas "
    "o terminos tecnicos mal transcritos, y a veces frases sueltas repetidas "
    "sin sentido (artefactos del reconocimiento de voz, especialmente al "
    "final del audio). Ignora esos fragmentos repetitivos o sin sentido en "
    "vez de intentar resumirlos o interpretarlos.\n"
    "- Las etiquetas de hablante (si existen) son genericas: SPEAKER_00, "
    "SPEAKER_01, etc. No corresponden a nombres reales. Un mismo hablante "
    "puede aparecer bajo dos etiquetas distintas si la diarizacion se "
    "equivoco, y una etiqueta puede mezclar a mas de una persona.\n"
    "- No inventes informacion que no este en la transcripcion. Si algo no "
    "esta claro (una tarea, un nombre, una fecha), dilo explicitamente en "
    "vez de rellenar el hueco de forma plausible.\n\n"
    "Responde siempre en espanol y en formato Markdown, con esta estructura:\n\n"
    "## Mapeo de hablantes\n"
    "Para cada etiqueta SPEAKER_XX que aparezca, una linea "
    "`SPEAKER_XX -> <nombre inferido o 'no identificado'>` con el nivel de "
    "confianza (alta/media/baja) segun cuanto contexto haya para inferirlo. "
    "Omite esta seccion por completo si la transcripcion no tiene etiquetas "
    "de hablante.\n\n"
    "## Resumen\n"
    "2-4 frases con el estado general de la reunion.\n\n"
    "## Por persona\n"
    "- **<nombre o etiqueta>**: en que esta trabajando, bloqueos, proximos "
    "pasos. Se conciso: una o dos frases por persona, sin repetir literalmente "
    "frases de la transcripcion.\n\n"
    "## Acciones y pendientes\n"
    "- Tareas o compromisos concretos mencionados, con quien los asume si se "
    "sabe. Solo incluye compromisos reales, no comentarios genericos tipo "
    "'vale, genial'.\n\n"
    "## Bloqueos / riesgos\n"
    "- Problemas o riesgos mencionados que requieran seguimiento.\n\n"
    "Si alguna seccion no tiene contenido relevante, indica '(sin novedades)' "
    "en vez de omitirla (salvo el mapeo de hablantes, que se omite entero si "
    "no aplica)."
)


GLOSARIO_PROMPT = (
    "\n\nGlosario del proyecto. Usalo para reconocer y **corregir** los "
    "terminos que la transcripcion automatica haya deformado (siglas, nombres "
    "propios, productos): escribe siempre la forma correcta del glosario, no "
    "la que aparece en la transcripcion. Si un termino de la transcripcion no "
    "esta en el glosario y no lo entiendes, no lo inventes: dilo. El glosario "
    "puede incluir una seccion de terminos dudosos, que son precisamente los "
    "que aun no estan confirmados; trata esos con cautela.\n\n"
)


def call_litellm(
    base_url: str,
    api_key: str,
    model: str,
    transcript: str,
    attendees: str | None,
    glosario: str | None = None,
) -> str:
    system_content = SYSTEM_PROMPT
    if glosario:
        system_content += GLOSARIO_PROMPT + glosario

    user_content = ""
    if attendees:
        user_content += (
            f"Participantes habituales de esta reunion (puede que no esten "
            f"todos presentes hoy, usalo solo como pista para el mapeo de "
            f"hablantes): {attendees}\n\n"
        )
    user_content += f"Transcripcion de la reunion:\n\n{transcript}"

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_content},
            {"role": "user", "content": user_content},
        ],
        "temperature": 0.3,
    }
    request = urllib.request.Request(
        url=f"{base_url.rstrip('/')}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=600) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"LiteLLM devolvio HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(
            f"No se pudo conectar con LiteLLM en '{base_url}': {exc.reason}"
        ) from exc

    return body["choices"][0]["message"]["content"]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Resume una transcripcion de reunion usando un LLM local via LiteLLM."
    )
    parser.add_argument("transcript", help="Ruta al fichero .txt de transcripcion")
    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help=f"Nombre del modelo en LiteLLM (por defecto: {DEFAULT_MODEL})",
    )
    parser.add_argument(
        "--base-url",
        default=DEFAULT_BASE_URL,
        help=f"URL base de la API de LiteLLM (por defecto: {DEFAULT_BASE_URL})",
    )
    parser.add_argument(
        "--api-key",
        default=None,
        help="API key de LiteLLM (o usa la variable de entorno LITELLM_API_KEY)",
    )
    parser.add_argument(
        "--attendees",
        default=None,
        help="Lista de nombres habituales de la reunion, separados por comas "
        "(ej. 'Pablo Gil, Javi, Jose Javier'), para ayudar a mapear las "
        "etiquetas SPEAKER_XX a nombres reales",
    )
    parser.add_argument(
        "--glosario",
        default=None,
        help="Ruta al glosario del proyecto, para corregir siglas y nombres mal "
        "transcritos (por defecto: datos/glosario.md si existe)",
    )
    parser.add_argument(
        "--sin-glosario",
        action="store_true",
        help="No usar el glosario aunque exista datos/glosario.md",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Ruta del .md de salida (por defecto: <nombre>_resumen.md junto a la transcripcion)",
    )
    args = parser.parse_args()

    transcript_path = Path(args.transcript)
    if not transcript_path.exists():
        print(f"Error: no se encuentra el archivo '{transcript_path}'", file=sys.stderr)
        sys.exit(1)

    api_key = args.api_key or os.environ.get("LITELLM_API_KEY")
    if not api_key:
        print(
            "Error: falta la API key de LiteLLM. Pasa --api-key o define la "
            "variable de entorno LITELLM_API_KEY.",
            file=sys.stderr,
        )
        sys.exit(1)

    transcript = transcript_path.read_text(encoding="utf-8")

    glosario = None
    if not args.sin_glosario:
        if args.glosario and not Path(args.glosario).exists():
            print(
                f"Error: no se encuentra el glosario '{args.glosario}'",
                file=sys.stderr,
            )
            sys.exit(1)
        contenido = glosario_mod.cargar(args.glosario)
        if contenido:
            glosario = glosario_mod.texto_completo(contenido)

    print(f"Modelo:    {args.model}")
    print(f"LiteLLM:   {args.base_url}")
    print(f"Glosario:  {'si' if glosario else 'no'}")
    print(f"Resumiendo '{transcript_path.name}'...")

    summary = call_litellm(
        args.base_url, api_key, args.model, transcript, args.attendees, glosario
    )

    output_path = Path(args.output) if args.output else transcript_path.with_name(
        f"{transcript_path.stem}_resumen.md"
    )
    output_path.write_text(summary.strip() + "\n", encoding="utf-8")

    print(f"\nGuardado: {output_path}")
    print("\n--- Resumen ---\n")
    print(summary.strip())


if __name__ == "__main__":
    main()
