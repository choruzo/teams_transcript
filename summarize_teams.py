"""
summarize_teams.py

Genera un resumen de una transcripcion (el .txt generado por
transcribe_teams.py, con o sin diarizacion) usando un LLM local servido a
traves de LiteLLM (API compatible con OpenAI), y lo guarda tanto en Markdown
como en el almacen SQLite del historico (`datos/meetings.db`, ver memoria.py).

Uso:
    python summarize_teams.py grabaciones\\20260907_090437_mixed.txt
    python summarize_teams.py archivo.txt --tipo retro
    python summarize_teams.py archivo.txt --model qwen3.8-27b --sin-bd

Genera junto a la transcripcion:
  <nombre>_resumen.md

Notas:
  - Requiere la API key de LiteLLM en la variable de entorno LITELLM_API_KEY
    (o pasarla con --api-key).
  - El LLM devuelve JSON estructurado; el Markdown se renderiza en Python a
    partir de ese JSON, de modo que la misma llamada alimenta el .md y la BD.
    Si el modelo no consigue devolver JSON valido (dos intentos), se guarda su
    respuesta cruda como .md y no se toca la BD: nunca se pierde el trabajo.
  - Con --sin-bd se reproduce el comportamiento anterior de solo Markdown.
  - Antes de resumir se leen de la BD las acciones que quedaron abiertas en
    las ultimas reuniones y se le pasan al modelo con su identificador, para
    que diga en que estado quedan (--arrastres N, --sin-arrastres).
  - Si la transcripcion viene de --diarize, los hablantes apareceran como
    SPEAKER_00, SPEAKER_01, etc. (etiquetas genericas, sin nombres reales) y
    el modelo intenta mapearlos a personas por contexto.
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import glosario as glosario_mod
import llm
import memoria
import rag

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

DEFAULT_MODEL = "qwen3.6-35b-a3b"
DEFAULT_BASE_URL = "http://localhost:4000/v1"

# La llamada estructurada quiere obediencia, no creatividad.
TEMPERATURA_JSON = 0.1


BASE_PROMPT = (
    "Eres un asistente que analiza transcripciones de reuniones de un equipo "
    "de desarrollo de software, generadas por transcripcion automatica "
    "(Whisper) y, opcionalmente, diarizacion de hablantes (pyannote). Ten en "
    "cuenta sus limitaciones:\n\n"
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
    "esta claro (una tarea, un nombre, una fecha), dilo explicitamente en vez "
    "de rellenar el hueco de forma plausible.\n\n"
    "Escribe siempre en espanol."
)


ESQUEMA_COMUN = """
Campos comunes del objeto JSON:

  "resumen": string. 2-4 frases con el estado general de la reunion.
  "hablantes": lista de objetos {"etiqueta": "SPEAKER_XX", "nombre": "<nombre
      inferido o 'no identificado'>", "confianza": "alta"|"media"|"baja"}.
      Una entrada por cada etiqueta SPEAKER_XX que aparezca en la
      transcripcion. Lista vacia si la transcripcion no tiene etiquetas.
  "por_persona": lista de objetos {"persona": "<nombre o etiqueta>",
      "trabajo": "...", "bloqueos": "...", "proximos_pasos": "..."}. Se
      conciso: una o dos frases por campo, sin copiar literalmente la
      transcripcion. Usa "" en los campos sin contenido.
  "acciones": lista de objetos {"persona": "<nombre o etiqueta o null>",
      "descripcion": "<compromiso concreto>", "estado":
      "abierta"|"en_progreso"|"completada"|"bloqueada"}. Solo compromisos
      reales, no comentarios genericos tipo "vale, genial".
  "arrastres": lista de objetos {"action_id": <numero>, "estado":
      "completada"|"en_progreso"|"bloqueada"|"abierta"|"abandonada"|
      "sin_mencion", "comentario": "..."}. Dejala vacia salvo que se te haya
      dado una lista de acciones abiertas anteriores con sus identificadores.
  "riesgos": lista de objetos {"descripcion": "...", "area": "<componente o
      null>", "severidad": "alta"|"media"|"baja"}.
"""


# Cada tipo de reunion aporta su seccion especifica; el resto del esquema es
# comun para que la BD y los informes no tengan que saber de tipos.
TIPOS = {
    "daily": {
        "contexto": "una reunion diaria de seguimiento (daily)",
        "enfoque": (
            "Centrate en el estado de cada persona: en que trabaja, que la "
            "bloquea y que hara a continuacion, y en los compromisos "
            "concretos que se adquieren."
        ),
        "clave": None,
        "esquema": "",
        "secciones": (),
    },
    "retro": {
        "contexto": "una retrospectiva de equipo",
        "enfoque": (
            "Centrate en la valoracion del periodo: que ha funcionado, que no, "
            "y que acuerdos de mejora se toman. Las acciones de mejora "
            "acordadas van tambien en \"acciones\"."
        ),
        "clave": "retro",
        "esquema": (
            '  "retro": {"bien": ["..."], "mal": ["..."], "mejoras": ["..."]}\n'
        ),
        "secciones": (
            ("bien", "Que fue bien"),
            ("mal", "Que no fue bien"),
            ("mejoras", "Acciones de mejora acordadas"),
        ),
    },
    "planning": {
        "contexto": "una reunion de planificacion (planning)",
        "enfoque": (
            "Centrate en el alcance que el equipo se compromete a abordar, las "
            "estimaciones que se mencionan y las dudas que quedan sin "
            "resolver."
        ),
        "clave": "planning",
        "esquema": (
            '  "planning": {"alcance": ["..."], "estimaciones": ["..."], '
            '"dudas": ["..."]}\n'
        ),
        "secciones": (
            ("alcance", "Alcance comprometido"),
            ("estimaciones", "Estimaciones"),
            ("dudas", "Dudas abiertas"),
        ),
    },
    "workshop": {
        "contexto": "un workshop o sesion de trabajo tecnica",
        "enfoque": (
            "Centrate en los temas tratados, las decisiones tomadas (con su "
            "motivo, si se dice) y las preguntas que quedan sin resolver."
        ),
        "clave": "workshop",
        "esquema": (
            '  "workshop": {"temas": ["..."], "decisiones": ["..."], '
            '"preguntas": ["..."]}\n'
        ),
        "secciones": (
            ("temas", "Temas tratados"),
            ("decisiones", "Decisiones"),
            ("preguntas", "Preguntas sin resolver"),
        ),
    },
}


ARRASTRES_PROMPT = (
    "\n\nEn el mensaje del usuario recibiras una lista de acciones que "
    "quedaron abiertas en reuniones anteriores, cada una con su identificador "
    "entre corchetes. Para cada una, decide a partir de lo que se diga en esta "
    "transcripcion en que estado queda y devuelvelo en \"arrastres\" usando "
    "**exactamente** ese identificador:\n"
    "  - \"completada\": se dice que ya esta hecha.\n"
    "  - \"en_progreso\": se esta trabajando en ella.\n"
    "  - \"bloqueada\": se menciona que algo la impide avanzar.\n"
    "  - \"abierta\": se menciona pero sigue igual, sin avance.\n"
    "  - \"abandonada\": se decide no hacerla.\n"
    "  - \"sin_mencion\": no se habla de ella en esta reunion.\n"
    "Incluye una entrada por cada accion de la lista, tambien las que no se "
    "mencionan, y anade en \"comentario\" lo que se haya dicho al respecto (o "
    "\"\" si no se ha dicho nada). No uses identificadores que no esten en la "
    "lista, y no repitas en \"acciones\" una accion que ya venga con "
    "identificador: \"acciones\" es solo para compromisos nuevos de esta "
    "reunion.\n"
    "Ojo: que alguien hable del mismo tema no significa que la accion haya "
    "avanzado. Si no queda claro, deja el estado que ya tenia."
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


# --------------------------------------------------------------------------
# Prompts
# --------------------------------------------------------------------------


def construir_system_prompt(
    tipo: str, glosario: str | None, con_arrastres: bool = False
) -> str:
    conf = TIPOS[tipo]
    partes = [
        BASE_PROMPT,
        f"\n\nLa transcripcion corresponde a {conf['contexto']}. {conf['enfoque']}",
        "\n\nResponde UNICAMENTE con un objeto JSON valido, sin texto antes ni "
        "despues y sin bloque de codigo. Usa exactamente estas claves; no "
        "anadas otras.\n",
        ESQUEMA_COMUN,
    ]
    if conf["esquema"]:
        partes.append("\nCampo especifico de este tipo de reunion:\n\n")
        partes.append(conf["esquema"])
    partes.append(
        "\nSi una lista no tiene contenido relevante, devuelvela vacia; no "
        "inventes elementos para rellenar."
    )
    if con_arrastres:
        partes.append(ARRASTRES_PROMPT)
    if glosario:
        partes.append(GLOSARIO_PROMPT + glosario)
    return "".join(partes)


def construir_user_prompt(
    transcript: str, attendees: str | None, arrastres: str | None = None
) -> str:
    partes = []
    if attendees:
        partes.append(
            f"Participantes habituales de esta reunion (puede que no esten "
            f"todos presentes hoy, usalo solo como pista para el mapeo de "
            f"hablantes): {attendees}\n\n"
        )
    if arrastres:
        partes.append(arrastres + "\n\n")
    partes.append(f"Transcripcion de la reunion:\n\n{transcript}")
    return "".join(partes)


def formatear_acciones_abiertas(filas) -> str:
    """Lista de acciones abiertas anteriores, tal como la ve el modelo.

    El identificador entre corchetes es lo que convierte el emparejamiento en
    una eleccion entre opciones cerradas, en vez de en generacion libre:
    comparar descripciones entre reuniones ("reinstalar WLS" vs "la
    instalacion de WLS") es mucho menos fiable.
    """
    lineas = ["Acciones abiertas de reuniones anteriores:"]
    for fila in filas:
        quien = fila["persona"] or "sin asignar"
        menciones = fila["menciones"]
        plural = "menciones" if menciones != 1 else "mencion"
        visto = (
            f", vista por ultima vez el {fila['ultima_fecha']}"
            if fila["ultima_fecha"]
            else ""
        )
        lineas.append(
            f"  [{fila['id']}] {quien} - {fila['descripcion']} "
            f"({fila['estado']}, {menciones} {plural}{visto})"
        )
    return "\n".join(lineas)


# --------------------------------------------------------------------------
# Llamada al LLM
# --------------------------------------------------------------------------


def call_litellm(
    base_url: str,
    api_key: str,
    model: str,
    messages: list[dict],
    temperature: float = 0.3,
) -> str:
    """Envoltorio de `llm.chat`, que es donde vive ahora el cliente HTTP.

    Se conserva el nombre y la firma porque este modulo es lo que se ejecuta a
    mano en el servidor: mover la implementacion no deberia cambiar nada de lo
    que ya funciona. `llm.ErrorLLM` hereda de RuntimeError, asi que quien
    capturaba RuntimeError sigue capturandolo.
    """
    return llm.chat(base_url, api_key, model, messages, temperature)


_BLOQUE_CODIGO = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def extraer_json(respuesta: str) -> dict:
    """Interpreta la respuesta del modelo como objeto JSON.

    Los modelos locales tienden a envolver el JSON en un bloque de codigo o a
    acompanarlo de una frase; se acepta ambas cosas antes de darlo por malo.
    Lanza ValueError si no hay JSON utilizable.
    """
    texto = respuesta.strip()

    candidatos = [m.group(1).strip() for m in _BLOQUE_CODIGO.finditer(texto)]
    candidatos.append(texto)
    inicio, fin = texto.find("{"), texto.rfind("}")
    if inicio != -1 and fin > inicio:
        candidatos.append(texto[inicio : fin + 1])

    for candidato in candidatos:
        if not candidato:
            continue
        try:
            datos = json.loads(candidato)
        except json.JSONDecodeError:
            continue
        if isinstance(datos, dict):
            return datos

    raise ValueError("la respuesta no contiene un objeto JSON valido")


def _lista_de_dicts(datos: dict, clave: str) -> list[dict]:
    valor = datos.get(clave)
    if not isinstance(valor, list):
        return []
    return [item for item in valor if isinstance(item, dict)]


def _lista_de_textos(valor) -> list[str]:
    if not isinstance(valor, list):
        return []
    return [str(item).strip() for item in valor if str(item).strip()]


# El umbral vive en memoria.py: la interfaz web lo necesita para marcar las
# mismas acciones que marca la seccion "Arrastres" del .md.
UMBRAL_ESTANCAMIENTO = memoria.UMBRAL_ESTANCAMIENTO


def normalizar(
    datos: dict, tipo: str, acciones_previas: dict[int, dict] | None = None
) -> dict:
    """Valida y normaliza el JSON del modelo antes de usarlo.

    Se toleran claves ausentes o con el tipo equivocado (se sustituyen por
    vacio); lo que no se tolera es meter en la BD algo que no sea lo esperado.

    `acciones_previas` es el {action_id: fila} que se le ofrecio al modelo.
    Los arrastres se validan contra esa lista: sin ella no se acepta ninguno
    (el modelo se los estaria inventando), y con ella se descartan los ids
    ajenos y se enriquecen con la descripcion real para el Markdown.
    """
    limpio = {
        "resumen": str(datos.get("resumen") or "").strip(),
        "hablantes": [],
        "por_persona": [],
        "acciones": [],
        "arrastres": [],
        "riesgos": [],
    }

    for hablante in _lista_de_dicts(datos, "hablantes"):
        etiqueta = str(hablante.get("etiqueta") or "").strip()
        if not etiqueta:
            continue
        limpio["hablantes"].append(
            {
                "etiqueta": etiqueta,
                "nombre": str(hablante.get("nombre") or "").strip() or "no identificado",
                "confianza": str(hablante.get("confianza") or "").strip().lower(),
            }
        )

    for upd in _lista_de_dicts(datos, "por_persona"):
        persona = str(upd.get("persona") or "").strip()
        if not persona:
            continue
        limpio["por_persona"].append(
            {
                "persona": persona,
                "trabajo": str(upd.get("trabajo") or "").strip(),
                "bloqueos": str(upd.get("bloqueos") or "").strip(),
                "proximos_pasos": str(upd.get("proximos_pasos") or "").strip(),
            }
        )

    for accion in _lista_de_dicts(datos, "acciones"):
        descripcion = str(accion.get("descripcion") or "").strip()
        if not descripcion:
            continue
        estado = str(accion.get("estado") or "abierta").strip().lower()
        if estado not in memoria.ESTADOS_ACCION:
            estado = "abierta"
        limpio["acciones"].append(
            {
                "persona": str(accion.get("persona") or "").strip(),
                "descripcion": descripcion,
                "estado": estado,
            }
        )

    previas = acciones_previas or {}
    vistos: set[int] = set()
    for arrastre in _lista_de_dicts(datos, "arrastres"):
        try:
            action_id = int(arrastre.get("action_id"))
        except (TypeError, ValueError):
            continue
        if action_id not in previas or action_id in vistos:
            continue
        estado = str(arrastre.get("estado") or "").strip().lower()
        if estado not in memoria.ESTADOS_ACCION:
            # "sin_mencion" y cualquier invencion del modelo se quedan fuera:
            # no cambian nada en la BD y solo ensuciarian el Markdown.
            continue
        vistos.add(action_id)
        fila = previas[action_id]
        menciones = (fila["menciones"] or 0) + 1
        limpio["arrastres"].append(
            {
                "action_id": action_id,
                "estado": estado,
                "comentario": str(arrastre.get("comentario") or "").strip(),
                "descripcion": fila["descripcion"],
                "persona": fila["persona"] or "",
                "menciones": menciones,
                "estancada": (
                    estado not in ("completada", "abandonada")
                    and menciones >= UMBRAL_ESTANCAMIENTO
                ),
            }
        )

    for riesgo in _lista_de_dicts(datos, "riesgos"):
        descripcion = str(riesgo.get("descripcion") or "").strip()
        if not descripcion:
            continue
        limpio["riesgos"].append(
            {
                "descripcion": descripcion,
                "area": str(riesgo.get("area") or "").strip(),
                "severidad": str(riesgo.get("severidad") or "").strip().lower(),
            }
        )

    clave = TIPOS[tipo]["clave"]
    if clave:
        bloque = datos.get(clave)
        bloque = bloque if isinstance(bloque, dict) else {}
        limpio[clave] = {
            campo: _lista_de_textos(bloque.get(campo))
            for campo, _ in TIPOS[tipo]["secciones"]
        }

    return limpio


def pedir_resumen(
    base_url: str,
    api_key: str,
    model: str,
    system_prompt: str,
    user_prompt: str,
) -> tuple[dict | None, str]:
    """Pide el resumen estructurado. Devuelve (json_normalizado|None, crudo).

    Un unico reintento si el JSON no es valido: se le devuelve al modelo su
    propia respuesta y el error, que es mas efectivo que repetir la peticion
    entera. Si tambien falla, se devuelve None y el texto crudo para que el
    llamante lo guarde tal cual.
    """
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    respuesta = call_litellm(base_url, api_key, model, messages, TEMPERATURA_JSON)
    try:
        return extraer_json(respuesta), respuesta
    except ValueError as exc:
        print(
            f"Aviso: {exc}. Reintentando pidiendo solo el JSON...",
            file=sys.stderr,
        )

    messages.append({"role": "assistant", "content": respuesta})
    messages.append(
        {
            "role": "user",
            "content": (
                "Tu respuesta anterior no era un objeto JSON valido. Devuelve "
                "unicamente el objeto JSON corregido, con las claves indicadas, "
                "sin explicaciones ni bloque de codigo."
            ),
        }
    )
    reintento = call_litellm(base_url, api_key, model, messages, TEMPERATURA_JSON)
    try:
        return extraer_json(reintento), reintento
    except ValueError:
        return None, reintento


# --------------------------------------------------------------------------
# Renderizado del Markdown
# --------------------------------------------------------------------------


def _vinetas(items: list[str]) -> list[str]:
    return [f"- {item}" for item in items] if items else ["(sin novedades)"]


def renderizar_markdown(
    datos: dict, tipo: str, fecha: str, titulo: str | None
) -> str:
    lineas: list[str] = []
    encabezado = titulo or f"Resumen de la reunion ({tipo})"
    lineas.append(f"# {encabezado}")
    lineas.append("")
    lineas.append(f"*Fecha: {fecha} · Tipo: {tipo}*")
    lineas.append("")

    if datos["hablantes"]:
        lineas.append("## Mapeo de hablantes")
        lineas.append("")
        for hablante in datos["hablantes"]:
            confianza = hablante["confianza"]
            sufijo = f" (confianza {confianza})" if confianza else ""
            lineas.append(f"- {hablante['etiqueta']} -> {hablante['nombre']}{sufijo}")
        lineas.append("")

    lineas.append("## Resumen")
    lineas.append("")
    lineas.append(datos["resumen"] or "(sin novedades)")
    lineas.append("")

    lineas.append("## Por persona")
    lineas.append("")
    if datos["por_persona"]:
        for upd in datos["por_persona"]:
            detalle = []
            if upd["trabajo"]:
                detalle.append(upd["trabajo"])
            if upd["bloqueos"]:
                detalle.append(f"Bloqueos: {upd['bloqueos']}")
            if upd["proximos_pasos"]:
                detalle.append(f"Proximos pasos: {upd['proximos_pasos']}")
            cuerpo = " ".join(detalle) if detalle else "(sin novedades)"
            lineas.append(f"- **{upd['persona']}**: {cuerpo}")
    else:
        lineas.append("(sin novedades)")
    lineas.append("")

    lineas.append("## Acciones y pendientes")
    lineas.append("")
    if datos["acciones"]:
        for accion in datos["acciones"]:
            quien = f"**{accion['persona']}** — " if accion["persona"] else ""
            lineas.append(f"- {quien}{accion['descripcion']} ({accion['estado']})")
    else:
        lineas.append("(sin novedades)")
    lineas.append("")

    if datos["arrastres"]:
        lineas.append("## Arrastres")
        lineas.append("")
        for arrastre in datos["arrastres"]:
            quien = f"**{arrastre['persona']}** — " if arrastre.get("persona") else ""
            descripcion = arrastre.get("descripcion") or ""
            cuerpo = f"{descripcion} " if descripcion else ""
            marcas = [arrastre["estado"]]
            if arrastre.get("menciones"):
                marcas.append(f"{arrastre['menciones']} reuniones")
            if arrastre.get("estancada"):
                marcas.append("ESTANCADA")
            comentario = (
                f" — {arrastre['comentario']}" if arrastre["comentario"] else ""
            )
            lineas.append(
                f"- [{arrastre['action_id']}] {quien}{cuerpo}"
                f"({', '.join(marcas)}){comentario}"
            )
        lineas.append("")

    lineas.append("## Bloqueos / riesgos")
    lineas.append("")
    if datos["riesgos"]:
        for riesgo in datos["riesgos"]:
            marcas = [m for m in (riesgo["area"], riesgo["severidad"]) if m]
            sufijo = f" ({', '.join(marcas)})" if marcas else ""
            lineas.append(f"- {riesgo['descripcion']}{sufijo}")
    else:
        lineas.append("(sin novedades)")
    lineas.append("")

    clave = TIPOS[tipo]["clave"]
    if clave and clave in datos:
        for campo, titulo_seccion in TIPOS[tipo]["secciones"]:
            lineas.append(f"## {titulo_seccion}")
            lineas.append("")
            lineas.extend(_vinetas(datos[clave].get(campo, [])))
            lineas.append("")

    return "\n".join(lineas).rstrip() + "\n"


# --------------------------------------------------------------------------
# Lectura de la transcripcion
# --------------------------------------------------------------------------

_LINEA_TXT = re.compile(r"^\[(?P<etiqueta>[^\]]+)\]\s*(?P<texto>.*)$")
_TIEMPO_SRT = re.compile(
    r"(\d{2}):(\d{2}):(\d{2}),(\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2}),(\d{3})"
)


def _a_segundos(h: str, m: str, s: str, ms: str) -> float:
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000.0


def leer_segmentos(transcript_path: Path) -> list[dict]:
    """Segmentos de la transcripcion, con marcas de tiempo si hay .srt.

    El .txt no lleva tiempos, asi que se prefiere el .srt hermano (mismo
    nombre, extension distinta) que si los tiene. Si no existe, se cae al .txt
    y los segmentos quedan sin inicio/fin.
    """
    srt_path = transcript_path.with_suffix(".srt")
    if srt_path.exists():
        return _leer_srt(srt_path)
    return _leer_txt(transcript_path)


def _leer_srt(path: Path) -> list[dict]:
    segmentos: list[dict] = []
    inicio = fin = None
    texto: list[str] = []

    def cerrar() -> None:
        if texto:
            contenido = " ".join(texto).strip()
            if contenido:
                etiqueta = None
                match = _LINEA_TXT.match(contenido)
                if match:
                    etiqueta = match.group("etiqueta")
                    contenido = match.group("texto").strip()
                segmentos.append(
                    {
                        "inicio": inicio,
                        "fin": fin,
                        "etiqueta": etiqueta,
                        "texto": contenido,
                    }
                )
        texto.clear()

    for linea in path.read_text(encoding="utf-8").splitlines():
        linea = linea.strip()
        tiempos = _TIEMPO_SRT.match(linea)
        if tiempos:
            cerrar()
            inicio = _a_segundos(*tiempos.groups()[:4])
            fin = _a_segundos(*tiempos.groups()[4:])
        elif not linea or linea.isdigit():
            if not linea:
                cerrar()
        else:
            texto.append(linea)
    cerrar()
    return segmentos


def _leer_txt(path: Path) -> list[dict]:
    segmentos = []
    for linea in path.read_text(encoding="utf-8").splitlines():
        linea = linea.strip()
        if not linea:
            continue
        match = _LINEA_TXT.match(linea)
        if match:
            segmentos.append(
                {
                    "inicio": None,
                    "fin": None,
                    "etiqueta": match.group("etiqueta"),
                    "texto": match.group("texto").strip(),
                }
            )
        else:
            segmentos.append(
                {"inicio": None, "fin": None, "etiqueta": None, "texto": linea}
            )
    return segmentos


_FECHA_EN_NOMBRE = re.compile(r"(?P<a>\d{4})(?P<m>\d{2})(?P<d>\d{2})[_-]?(?:\d{6})?")


def inferir_fecha(transcript_path: Path) -> str:
    """Fecha ISO de la reunion, del nombre del fichero o de su mtime."""
    match = _FECHA_EN_NOMBRE.search(transcript_path.stem)
    if match:
        try:
            return datetime(
                int(match.group("a")), int(match.group("m")), int(match.group("d"))
            ).strftime("%Y-%m-%d")
        except ValueError:
            pass
    mtime = datetime.fromtimestamp(transcript_path.stat().st_mtime, timezone.utc)
    return mtime.strftime("%Y-%m-%d")


# --------------------------------------------------------------------------
# Persistencia
# --------------------------------------------------------------------------


def cargar_acciones_abiertas(
    ruta_db: str | None, transcript_path: Path, ultimas_reuniones: int
) -> dict[int, dict]:
    """Acciones abiertas de reuniones anteriores, indexadas por su id.

    Si esta transcripcion ya se proceso antes, su reunion se excluye: sus
    acciones propias desapareceran al reprocesar y las ajenas se devuelven
    como estaban antes de aquella pasada.
    """
    conn = memoria.conectar(ruta_db)
    try:
        previa = memoria.id_reunion_por_transcripcion(
            conn, str(transcript_path.resolve())
        )
        filas = memoria.acciones_abiertas(
            conn, ultimas_reuniones, excluir_meeting_id=previa
        )
    finally:
        conn.close()
    return {fila["id"]: dict(fila) for fila in filas}


def guardar_en_bd(
    ruta_db: str | None,
    datos: dict,
    *,
    tipo: str,
    fecha: str,
    titulo: str | None,
    transcript_path: Path,
    segmentos: list[dict],
    modelo_llm: str,
    modelo_whisper: str | None,
) -> tuple[int, dict]:
    """Vuelca el resumen y la transcripcion en SQLite. Devuelve (id, recuentos)."""
    duracion = None
    finales = [s["fin"] for s in segmentos if s.get("fin") is not None]
    if finales:
        duracion = max(finales)

    audio_path = None
    for extension in (".wav", ".mp3", ".m4a"):
        candidato = transcript_path.with_suffix(extension)
        if candidato.exists():
            audio_path = str(candidato.resolve())
            break

    conn = memoria.conectar(ruta_db)
    try:
        meeting_id = memoria.crear_reunion(
            conn,
            fecha=fecha,
            titulo=titulo,
            tipo=tipo,
            audio_path=audio_path,
            transcript_path=str(transcript_path.resolve()),
            duracion_seg=duracion,
            modelo_whisper=modelo_whisper,
            modelo_llm=modelo_llm,
            resumen=datos["resumen"],
            datos_json=datos,
        )
        mapa = memoria.registrar_hablantes(conn, meeting_id, datos["hablantes"])
        recuentos = {
            "segmentos": memoria.insertar_segmentos(conn, meeting_id, segmentos, mapa),
            "updates": memoria.insertar_updates(conn, meeting_id, datos["por_persona"]),
            "acciones": memoria.insertar_acciones(conn, meeting_id, datos["acciones"]),
            "riesgos": memoria.insertar_riesgos(conn, meeting_id, datos["riesgos"]),
            "arrastres": memoria.aplicar_arrastres(conn, meeting_id, datos["arrastres"]),
        }
        conn.commit()
    finally:
        conn.close()
    return meeting_id, recuentos


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def indexar_reunion_en_segundo_plano(args, transcript_path: Path) -> None:
    """Pone al dia el indice semantico de esta reunion.

    Es un extra: si falla -- no esta `sqlite-vec`, el modelo de embeddings no
    responde, el indice se construyo con otro modelo --, se avisa por stderr y
    ya esta. El resumen y la base de datos, que es lo que importa, ya estan
    guardados; y `python indexar_teams.py` lo arregla luego.
    """
    if not rag.hay_extension():
        return
    try:
        conn_hist = memoria.conectar(memoria.ruta_bd(args.db), solo_lectura=True)
        conn_idx = rag.conectar(args.indice)
        try:
            meeting_id = memoria.id_reunion_por_transcripcion(
                conn_hist, str(transcript_path)
            )
            fila = conn_hist.execute(
                "SELECT * FROM meetings WHERE id = ?", (meeting_id,)
            ).fetchone()
            modelo = llm.modelo_embeddings(args.modelo_embeddings)
            resultado = rag.indexar_reunion(
                conn_idx,
                conn_hist,
                fila,
                embebedor=rag.embebedor_litellm(
                    llm.base_url_embeddings(args.base_url_embeddings),
                    llm.api_key(args.api_key),
                    modelo,
                    llm.prefijo_embeddings(),
                ),
                modelo=modelo,
            )
        finally:
            conn_hist.close()
            conn_idx.close()
    except Exception as exc:  # noqa: BLE001 - el indice es accesorio
        print(
            f"Aviso: no se pudo actualizar el indice semantico: {exc}",
            file=sys.stderr,
        )
        return
    print(
        f"Indice semantico: {resultado['accion']} "
        f"({resultado.get('chunks', 0)} fragmentos)"
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Resume una transcripcion de reunion usando un LLM local via LiteLLM."
    )
    parser.add_argument("transcript", help="Ruta al fichero .txt de transcripcion")
    parser.add_argument(
        "--tipo",
        choices=sorted(TIPOS),
        default="daily",
        help="Tipo de reunion, determina el prompt y las secciones del resumen "
        "(por defecto: daily)",
    )
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
        "--db",
        default=None,
        help="Ruta de la base de datos del historico (por defecto: la variable "
        f"de entorno {memoria.VARIABLE_ENTORNO}, o datos/meetings.db)",
    )
    parser.add_argument(
        "--indice",
        default=None,
        help="Ruta del indice semantico (por defecto, la variable TEAMS_INDICE)",
    )
    parser.add_argument(
        "--sin-indice",
        action="store_true",
        help="No actualizar el indice semantico al terminar",
    )
    parser.add_argument(
        "--modelo-embeddings",
        default=None,
        help="Modelo de embeddings del indice (o TEAMS_EMBED_MODELO)",
    )
    parser.add_argument(
        "--base-url-embeddings",
        default=None,
        help="URL del modelo de embeddings (o TEAMS_EMBED_BASE_URL)",
    )
    parser.add_argument(
        "--sin-bd",
        action="store_true",
        help="No escribir en la base de datos, solo generar el .md",
    )
    parser.add_argument(
        "--arrastres",
        type=int,
        default=5,
        metavar="N",
        help="Numero de reuniones anteriores de las que traer acciones "
        "abiertas para que el modelo actualice su estado (por defecto: 5)",
    )
    parser.add_argument(
        "--sin-arrastres",
        action="store_true",
        help="No consultar las acciones abiertas de reuniones anteriores",
    )
    parser.add_argument(
        "--fecha",
        default=None,
        help="Fecha de la reunion en ISO (por defecto se deduce del nombre del "
        "fichero, y si no de su fecha de modificacion)",
    )
    parser.add_argument(
        "--titulo",
        default=None,
        help="Titulo de la reunion, para el .md y la BD",
    )
    parser.add_argument(
        "--modelo-whisper",
        default=None,
        help="Modelo de Whisper con el que se transcribio, solo para dejarlo "
        "registrado en la BD",
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

    fecha = args.fecha or inferir_fecha(transcript_path)

    output_path = (
        Path(args.output)
        if args.output
        else transcript_path.with_name(f"{transcript_path.stem}_resumen.md")
    )

    acciones_previas: dict[int, dict] = {}
    bloque_arrastres = None
    if not args.sin_bd and not args.sin_arrastres and args.arrastres > 0:
        try:
            acciones_previas = cargar_acciones_abiertas(
                args.db, transcript_path, args.arrastres
            )
        except Exception as exc:  # sin arrastres se resume igual, solo peor
            print(
                f"Aviso: no se pudieron leer las acciones abiertas: {exc}",
                file=sys.stderr,
            )
        if acciones_previas:
            bloque_arrastres = formatear_acciones_abiertas(acciones_previas.values())

    print(f"Modelo:    {args.model}")
    print(f"LiteLLM:   {args.base_url}")
    print(f"Tipo:      {args.tipo}")
    print(f"Fecha:     {fecha}")
    print(f"Glosario:  {'si' if glosario else 'no'}")
    print(f"BD:        {'no' if args.sin_bd else memoria.ruta_bd(args.db)}")
    print(
        f"Arrastres: "
        + (
            f"{len(acciones_previas)} acciones abiertas de las ultimas "
            f"{args.arrastres} reuniones"
            if acciones_previas
            else "no"
        )
    )
    print(f"Resumiendo '{transcript_path.name}'...")

    datos, crudo = pedir_resumen(
        args.base_url,
        api_key,
        args.model,
        construir_system_prompt(args.tipo, glosario, bool(bloque_arrastres)),
        construir_user_prompt(transcript, args.attendees, bloque_arrastres),
    )

    if datos is None:
        # El modelo no ha sido capaz de devolver JSON ni tras el reintento. Se
        # guarda su respuesta tal cual: peor que el Markdown renderizado, pero
        # infinitamente mejor que perder la llamada.
        output_path.write_text(crudo.strip() + "\n", encoding="utf-8")
        print(
            "Aviso: el modelo no devolvio JSON valido tras el reintento. Se ha "
            "guardado su respuesta sin procesar y no se ha tocado la base de "
            "datos.",
            file=sys.stderr,
        )
        print(f"\nGuardado: {output_path}")
        sys.exit(2)

    datos = normalizar(datos, args.tipo, acciones_previas)
    markdown = renderizar_markdown(datos, args.tipo, fecha, args.titulo)
    output_path.write_text(markdown, encoding="utf-8")
    print(f"\nGuardado: {output_path}")

    if not args.sin_bd:
        segmentos = leer_segmentos(transcript_path)
        try:
            meeting_id, recuentos = guardar_en_bd(
                args.db,
                datos,
                tipo=args.tipo,
                fecha=fecha,
                titulo=args.titulo,
                transcript_path=transcript_path,
                segmentos=segmentos,
                modelo_llm=args.model,
                modelo_whisper=args.modelo_whisper,
            )
        except Exception as exc:  # el .md ya esta guardado; la BD es lo accesorio
            print(
                f"Aviso: no se pudo escribir en la base de datos: {exc}",
                file=sys.stderr,
            )
        else:
            print(
                f"Base de datos: reunion #{meeting_id} "
                f"({recuentos['segmentos']} segmentos, {recuentos['updates']} updates, "
                f"{recuentos['acciones']} acciones, {recuentos['riesgos']} riesgos, "
                f"{recuentos['arrastres']} arrastres)"
            )
            if not args.sin_indice:
                indexar_reunion_en_segundo_plano(args, transcript_path)

    print("\n--- Resumen ---\n")
    print(markdown)


if __name__ == "__main__":
    main()
