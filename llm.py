"""
llm.py

Cliente del proxy LiteLLM (API compatible con OpenAI), compartido por todo el
proyecto: `summarize_teams.py` para resumir, `indexar_teams.py` para embeber y
`ask_teams.py` (y con el la web) para responder preguntas.

Solo stdlib, como el resto del motor: `urllib` y nada mas. Meter `openai` o
`requests` obligaria a llevarlos al servidor sin internet a cambio de nada.

Tres funciones:

    chat(...)        una respuesta completa, como hasta ahora
    chat_stream(...) la misma respuesta, trozo a trozo, para el SSE de la web
    embeddings(...)  vectores para el indice semantico (ver rag.py)

Las tres traducen los fallos de red a RuntimeError con un mensaje que se pueda
enseniar tal cual: "LiteLLM no responde" es informacion, "URLError" no.
"""

import json
import os
import sys
import urllib.error
import urllib.request
from collections.abc import Iterator

# El proxy corre en la misma maquina que el resto del pipeline (ver
# PLAN_MEJORAS.md, seccion 0). En el contenedor de la API no, y por eso las
# tres se pueden fijar por entorno.
DEFAULT_BASE_URL = "http://localhost:4000/v1"
DEFAULT_MODEL = "qwen3.6-35b-a3b"
DEFAULT_MODELO_EMBEDDINGS = "bge-m3"

VARIABLE_BASE_URL = "LITELLM_BASE_URL"
VARIABLE_BASE_URL_EMBEDDINGS = "TEAMS_EMBED_BASE_URL"
VARIABLE_API_KEY = "LITELLM_API_KEY"
VARIABLE_MODELO = "TEAMS_LLM_MODELO"
VARIABLE_MODELO_EMBEDDINGS = "TEAMS_EMBED_MODELO"
VARIABLE_PREFIJO_EMBEDDINGS = "TEAMS_EMBED_PREFIJO"
VARIABLE_PREFIJO_CONSULTA = "TEAMS_EMBED_PREFIJO_CONSULTA"
VARIABLE_TIMEOUT_CHAT = "TEAMS_LLM_TIMEOUT"
VARIABLE_TIMEOUT_EMBEDDINGS = "TEAMS_EMBED_TIMEOUT"

# Generar un resumen largo con un modelo local puede pasar de la media hora
# --medido: una daily de 44 KB con arrastres se comio los 600 s que habia
# aqui antes-- y el tope tiene que cubrir el caso peor de la maquina mas lenta,
# no el habitual: pasarse solo cuesta esperar, quedarse corto tira una
# generacion entera ya pagada. Embeber un lote, no: un embebido colgado dos
# minutos es un fallo que conviene ver pronto. Por eso son dos topes, y los
# dos se pueden subir por entorno sin tocar el codigo (ver `timeout_chat`).
TIMEOUT_CHAT = 1800
TIMEOUT_EMBEDDINGS = 120

# Cuantos textos por peticion. Suficiente para que el servidor amortice la
# llamada y no tanto como para que un lote perdido cueste caro reintentarlo.
LOTE_EMBEDDINGS = 32

# Como se le pide a un modelo de razonamiento que no razone. `reasoning_effort`
# es el campo estandar de OpenAI y lo entienden llama.cpp, vLLM y LiteLLM; si
# el backend no lo conoce devuelve un 400 y se repite la llamada sin el.
#
# Solo se usa donde el razonamiento es desperdicio: sacarle un JSON de
# filtros. Medido contra qwen3.8-27b, 3,8 s -> 0,7 s. En la respuesta al
# usuario NO se toca: ahi pensar puede mejorarla, y esa decision es del modelo.
CLAVE_SIN_RAZONAMIENTO = "reasoning_effort"


class ErrorLLM(RuntimeError):
    """Fallo al hablar con LiteLLM, con un mensaje presentable al usuario."""


# --------------------------------------------------------------------------
# Configuracion
# --------------------------------------------------------------------------


def base_url(explicita: str | None = None) -> str:
    """Orden: argumento > LITELLM_BASE_URL > localhost:4000."""
    return explicita or os.environ.get(VARIABLE_BASE_URL) or DEFAULT_BASE_URL


def timeout_chat(explicito: int | None = None) -> int:
    """Orden: argumento > TEAMS_LLM_TIMEOUT > TIMEOUT_CHAT, en segundos.

    Se resuelve en cada llamada, no al importar, porque un valor por defecto
    en la firma se congela al definir la funcion y dejaria la variable de
    entorno sin efecto para quien importe el modulo.
    """
    return _segundos(explicito, VARIABLE_TIMEOUT_CHAT, TIMEOUT_CHAT)


def timeout_embeddings(explicito: int | None = None) -> int:
    """Igual que `timeout_chat`, con TEAMS_EMBED_TIMEOUT."""
    return _segundos(explicito, VARIABLE_TIMEOUT_EMBEDDINGS, TIMEOUT_EMBEDDINGS)


def _segundos(explicito: int | None, variable: str, defecto: int) -> int:
    """Un entorno con basura no debe tumbar el pipeline: se ignora y se avisa."""
    if explicito is not None:
        return explicito
    crudo = os.environ.get(variable)
    if not crudo:
        return defecto
    try:
        valor = int(crudo)
    except ValueError:
        valor = 0
    if valor <= 0:
        print(
            f"Aviso: {variable}='{crudo}' no es un numero de segundos valido; "
            f"se usa {defecto}.",
            file=sys.stderr,
        )
        return defecto
    return valor


def base_url_embeddings(explicita: str | None = None) -> str:
    """Donde vive el modelo de embeddings, que **no tiene por que ser el mismo**.

    Con un proxy LiteLLM delante, chat y embeddings salen de la misma URL y
    esto no hace falta. Sin proxy -- dos `llama-server` sueltos, que es como
    esta montado el servidor de GPU -- cada modelo escucha en su puerto, y sin
    poder separarlos habria que elegir cual de los dos funciona.
    """
    return (
        explicita
        or os.environ.get(VARIABLE_BASE_URL_EMBEDDINGS)
        or base_url()
    )


def api_key(explicita: str | None = None) -> str | None:
    """La clave, o None.

    Devolver None es un caso normal, no un fallo: un `llama-server` directo no
    pide ninguna, y entonces la peticion sale sin cabecera `Authorization`.
    """
    return explicita or os.environ.get(VARIABLE_API_KEY)


def modelo(explicito: str | None = None) -> str:
    return explicito or os.environ.get(VARIABLE_MODELO) or DEFAULT_MODEL


def modelo_embeddings(explicito: str | None = None) -> str:
    return (
        explicito
        or os.environ.get(VARIABLE_MODELO_EMBEDDINGS)
        or DEFAULT_MODELO_EMBEDDINGS
    )


def prefijo_embeddings(explicito: str | None = None) -> str | None:
    """Prefijo de tarea que exigen algunos modelos.

    bge-m3, el elegido, no lleva ninguno. Otros si: nomic quiere
    `search_document: ` / `search_query: ` y e5 quiere `passage: ` / `query: `,
    y omitirlo degrada la recuperacion sin dar ningun error. Se configura por
    entorno para poder cambiar de modelo sin tocar codigo, pero el prefijo
    queda anotado en el indice: si cambia, hay que reconstruirlo.
    """
    if explicito is not None:
        return explicito or None
    valor = os.environ.get(VARIABLE_PREFIJO_EMBEDDINGS)
    return valor or None


def prefijo_consulta(explicito: str | None = None) -> str | None:
    """El prefijo de la **pregunta**, que en esos modelos es otro.

    nomic quiere `search_query: ` frente al `search_document: ` de los
    fragmentos, y e5 `query: ` frente a `passage: `. Usar el mismo en los dos
    lados es un error silencioso: la busqueda funciona, solo que peor.
    """
    if explicito is not None:
        return explicito or None
    valor = os.environ.get(VARIABLE_PREFIJO_CONSULTA)
    return valor or None


# --------------------------------------------------------------------------
# Transporte
# --------------------------------------------------------------------------


def _peticion(url: str, clave: str | None, payload: dict) -> urllib.request.Request:
    cabeceras = {"Content-Type": "application/json"}
    if clave:
        cabeceras["Authorization"] = f"Bearer {clave}"
    return urllib.request.Request(
        url=url,
        data=json.dumps(payload).encode("utf-8"),
        headers=cabeceras,
        method="POST",
    )


def _abrir(peticion: urllib.request.Request, url_base: str, timeout: int):
    """`urlopen` con los dos fallos de red traducidos a algo legible."""
    try:
        return urllib.request.urlopen(peticion, timeout=timeout)
    except urllib.error.HTTPError as exc:
        detalle = exc.read().decode("utf-8", errors="replace")
        raise ErrorLLM(f"LiteLLM devolvio HTTP {exc.code}: {detalle}") from exc
    except urllib.error.URLError as exc:
        raise ErrorLLM(
            f"No se pudo conectar con LiteLLM en '{url_base}': {exc.reason}"
        ) from exc
    except TimeoutError as exc:
        raise ErrorLLM(
            f"LiteLLM no respondio en {timeout} s ('{url_base}')."
        ) from exc


# --------------------------------------------------------------------------
# Chat
# --------------------------------------------------------------------------


def chat(
    base: str,
    clave: str | None,
    model: str,
    messages: list[dict],
    temperature: float = 0.3,
    timeout: int | None = None,
    sin_razonamiento: bool = False,
) -> str:
    """Una respuesta completa. Es la llamada que usa el pipeline de resumen."""
    timeout = timeout_chat(timeout)
    payload = {"model": model, "messages": messages, "temperature": temperature}
    if sin_razonamiento:
        payload[CLAVE_SIN_RAZONAMIENTO] = "none"
    url = f"{base.rstrip('/')}/chat/completions"
    try:
        with _abrir(_peticion(url, clave, payload), base, timeout) as respuesta:
            cuerpo = json.loads(respuesta.read().decode("utf-8"))
    except ErrorLLM:
        if not sin_razonamiento:
            raise
        # El backend no conoce `reasoning_effort` y ha devuelto un 400. Es una
        # optimizacion, no un requisito: se repite sin ella.
        payload.pop(CLAVE_SIN_RAZONAMIENTO, None)
        with _abrir(_peticion(url, clave, payload), base, timeout) as respuesta:
            cuerpo = json.loads(respuesta.read().decode("utf-8"))
    try:
        return cuerpo["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ErrorLLM(
            f"Respuesta inesperada de LiteLLM (sin 'choices'): {cuerpo!r:.300}"
        ) from exc


def chat_stream(
    base: str,
    clave: str | None,
    model: str,
    messages: list[dict],
    temperature: float = 0.3,
    timeout: int | None = None,
) -> Iterator[str]:
    """La misma respuesta, trozo a trozo.

    Degrada a `chat()` si el proxy o el modelo no soportan `stream`: ver la
    respuesta escribirse es una comodidad, no un requisito, y quedarse sin
    respuesta por eso seria absurdo. El fallback tambien cubre al modelo que
    acepta `stream` y devuelve un unico trozo con todo.
    """
    timeout = timeout_chat(timeout)
    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "stream": True,
    }
    peticion = _peticion(f"{base.rstrip('/')}/chat/completions", clave, payload)
    try:
        respuesta = _abrir(peticion, base, timeout)
    except ErrorLLM:
        # Un 400 aqui suele ser "este modelo no hace streaming". Un fallo de
        # red volvera a fallar abajo, con el mismo mensaje.
        yield chat(base, clave, model, messages, temperature, timeout)
        return

    trozos = 0
    with respuesta:
        for linea in respuesta:
            trozos += _cuenta(linea)
            texto = _trozo_sse(linea)
            if texto:
                yield texto
    if not trozos:
        # Stream aceptado pero sin un solo evento: no dejar al usuario mirando
        # una burbuja en blanco cuando una llamada normal si habria
        # respondido. Se cuentan **todos** los eventos y no solo los de texto
        # a proposito: un modelo de razonamiento emite decenas de trozos de
        # `reasoning_content` y uno solo de `content`, y si se quedara sin
        # espacio a mitad del razonamiento, contar solo el texto dispararia
        # una segunda generacion entera de 27B para nada.
        yield chat(base, clave, model, messages, temperature, timeout)


def _cuenta(linea: bytes) -> int:
    """1 si la linea es un evento del stream (aunque no traiga texto)."""
    texto = linea.decode("utf-8", errors="replace").strip()
    return 1 if texto.startswith("data:") and texto[5:].strip() not in ("", "[DONE]") else 0


def _trozo_sse(linea: bytes) -> str:
    """Extrae el texto de una linea `data: {...}` del stream de OpenAI.

    Todo lo que no encaje se ignora en silencio a proposito: comentarios de
    keepalive, lineas en blanco, `[DONE]` y campos que este modelo no manda.
    """
    texto = linea.decode("utf-8", errors="replace").strip()
    if not texto.startswith("data:"):
        return ""
    datos = texto[5:].strip()
    if not datos or datos == "[DONE]":
        return ""
    try:
        trozo = json.loads(datos)
    except json.JSONDecodeError:
        return ""
    try:
        # Solo `content`. Un modelo de razonamiento (qwen3) manda su cadena de
        # pensamiento en `reasoning_content`, y eso no es la respuesta: no se
        # ensenia ni se guarda.
        return trozo["choices"][0]["delta"].get("content") or ""
    except (KeyError, IndexError, TypeError):
        return ""


# --------------------------------------------------------------------------
# Embeddings
# --------------------------------------------------------------------------


def embeddings(
    base: str,
    clave: str | None,
    model: str,
    textos: list[str],
    prefijo: str | None = None,
    lote: int = LOTE_EMBEDDINGS,
    timeout: int | None = None,
) -> list[list[float]]:
    """Vectores de una lista de textos, en el mismo orden.

    Se pide por lotes y se reintenta **una vez** cada lote: reembeber una base
    entera es caro y un corte de red momentaneo no deberia obligar a repetirlo.
    """
    if not textos:
        return []
    timeout = timeout_embeddings(timeout)
    url = f"{base.rstrip('/')}/embeddings"
    vectores: list[list[float]] = []
    for inicio in range(0, len(textos), lote):
        trozo = textos[inicio : inicio + lote]
        entrada = [f"{prefijo}{t}" for t in trozo] if prefijo else trozo
        payload = {"model": model, "input": entrada}
        peticion = _peticion(url, clave, payload)
        try:
            with _abrir(peticion, base, timeout) as respuesta:
                cuerpo = json.loads(respuesta.read().decode("utf-8"))
        except ErrorLLM:
            with _abrir(_peticion(url, clave, payload), base, timeout) as respuesta:
                cuerpo = json.loads(respuesta.read().decode("utf-8"))
        vectores.extend(_vectores(cuerpo, len(trozo)))
    return vectores


def _vectores(cuerpo: dict, esperados: int) -> list[list[float]]:
    """Ordena por `index`: la API no garantiza que vuelvan en orden."""
    try:
        datos = sorted(cuerpo["data"], key=lambda d: d.get("index", 0))
        vectores = [list(d["embedding"]) for d in datos]
    except (KeyError, TypeError) as exc:
        raise ErrorLLM(
            f"Respuesta inesperada del modelo de embeddings: {cuerpo!r:.300}"
        ) from exc
    if len(vectores) != esperados:
        raise ErrorLLM(
            f"El modelo devolvio {len(vectores)} vectores para {esperados} textos."
        )
    return vectores


def comprobar(base: str, clave: str | None, timeout: int = 5) -> tuple[bool, str]:
    """Si el proxy responde. Para `/api/chat/estado` y para los CLI.

    Usa `GET /models`, que no gasta GPU: preguntar por la salud del proxy no
    deberia costar una generacion.
    """
    url = f"{base.rstrip('/')}/models"
    peticion = urllib.request.Request(url, method="GET")
    if clave:
        peticion.add_header("Authorization", f"Bearer {clave}")
    try:
        with urllib.request.urlopen(peticion, timeout=timeout) as respuesta:
            cuerpo = json.loads(respuesta.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 - aqui cualquier fallo es "no responde"
        return False, str(exc)
    modelos = [m.get("id") for m in cuerpo.get("data", []) if isinstance(m, dict)]
    return True, ", ".join(m for m in modelos if m)
