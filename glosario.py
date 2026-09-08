"""
glosario.py

Carga del glosario del proyecto (vocabulario, siglas, nombres del equipo) y
preparacion de las dos formas en que se usa:

  - `prompt_whisper()`  -> cadena corta para el `initial_prompt` de Whisper,
    que sesga el reconocimiento hacia las siglas y nombres propios del
    proyecto ("Coverity" en vez de "Goverity").
  - `texto_completo()`  -> glosario entero, para inyectarlo en el system
    prompt del LLM que resume, de forma que corrija los terminos que Whisper
    haya transcrito mal.

El fichero de glosario es Markdown normal. La parte destinada a Whisper se
delimita con comentarios HTML:

    <!-- whisper -->
    Coverity, OLS, WLS, banda base, non-regression
    <!-- /whisper -->

Si no hay marcadores, se usa el documento entero como origen del prompt de
Whisper (recortado, ver LIMITE_CARACTERES_WHISPER).

Modulo compartido por transcribe_teams.py y summarize_teams.py; sin
dependencias fuera de la stdlib.
"""

import re
import sys
from pathlib import Path

# Ruta por defecto, relativa al repo. `datos/` no se versiona (contiene datos
# de reuniones); en el repo vive `datos/glosario.ejemplo.md` como plantilla.
RUTA_POR_DEFECTO = Path(__file__).resolve().parent / "datos" / "glosario.md"

# El `initial_prompt` de Whisper se recorta internamente a `n_ctx // 2 - 1`
# tokens (~224 con los modelos actuales), y el recorte se queda con los
# ULTIMOS tokens: si nos pasamos, se pierde el principio del glosario. Por eso
# truncamos aqui, de forma controlada y avisando. ~3,5 caracteres por token en
# espanol, con margen de seguridad.
LIMITE_CARACTERES_WHISPER = 700

_MARCADOR = re.compile(
    r"<!--\s*whisper\s*-->(.*?)<!--\s*/whisper\s*-->",
    re.DOTALL | re.IGNORECASE,
)


def cargar(ruta: Path | str | None = None) -> str | None:
    """Devuelve el contenido del glosario, o None si no existe.

    No es un error que falte: el glosario es opcional y todo el pipeline
    funciona sin el.
    """
    path = Path(ruta) if ruta else RUTA_POR_DEFECTO
    if not path.exists():
        return None
    texto = path.read_text(encoding="utf-8").strip()
    return texto or None


def texto_completo(glosario: str) -> str:
    """Glosario listo para el LLM: se quitan los marcadores, no el contenido."""
    return _MARCADOR.sub(lambda m: m.group(1).strip(), glosario).strip()


def _terminos_para_whisper(glosario: str) -> str:
    marcado = _MARCADOR.search(glosario)
    if marcado:
        bruto = marcado.group(1)
    else:
        # Sin marcadores: se usa el documento entero, quitando encabezados,
        # vinetas y enfasis para que quede una lista de terminos limpia.
        bruto = glosario

    lineas = []
    for linea in bruto.splitlines():
        linea = linea.strip()
        if not linea or linea.startswith("#") or linea.startswith("<!--"):
            continue
        linea = re.sub(r"^[-*+]\s+", "", linea)
        linea = linea.replace("**", "").replace("`", "")
        lineas.append(linea)
    return ", ".join(lineas)


def prompt_whisper(glosario: str, avisar: bool = True) -> str:
    """Construye el `initial_prompt` de Whisper a partir del glosario.

    Whisper responde mejor a un prompt con forma de frase en el mismo estilo
    que el audio esperado que a una lista pelada de terminos.
    """
    terminos = _terminos_para_whisper(glosario)
    prompt = f"Reunion de trabajo en espanol. Terminos que aparecen: {terminos}."

    if len(prompt) > LIMITE_CARACTERES_WHISPER:
        corte = prompt.rfind(",", 0, LIMITE_CARACTERES_WHISPER)
        if corte == -1:
            corte = LIMITE_CARACTERES_WHISPER
        prompt = prompt[:corte] + "."
        if avisar:
            print(
                f"Aviso: la seccion de glosario para Whisper excede el limite "
                f"de ~{LIMITE_CARACTERES_WHISPER} caracteres y se ha recortado. "
                f"Deja en <!-- whisper --> solo los terminos mas importantes; "
                f"el resto del glosario se sigue usando en el resumen.",
                file=sys.stderr,
            )
    return prompt
