"""Chat sobre el historico (fase I5).

Dos endpoints: `POST /api/chat`, que responde en streaming, y
`GET /api/chat/estado`, que dice de antemano si va a poder responder y con
que busqueda.

Esta capa **no interpreta preguntas ni recupera nada**: importa `ask_teams`
y le pasa las conexiones. Si la logica de recuperacion acabara aqui, existiria
dos veces -- una para la web y otra para la consola -- y las dos responderian
distinto al mes siguiente.

Dos detalles que no son negociables:

  - **No se usa `Depends(conexion)`.** Esa dependencia cierra la conexion en
    su `finally` cuando la funcion retorna, y un `StreamingResponse` sigue
    produciendo datos despues: la conexion se cerraria a mitad de la
    respuesta. Se abre y se cierra **dentro** del generador.
  - **Un fallo del LLM viaja como evento `error`**, no como un stream cortado.
    Una conexion que se corta sin decir nada deja al navegador esperando y sin
    forma de saber si la respuesta termino o se rompio.
"""

import json
import os
import sqlite3
from collections.abc import Iterator

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

import memoria
from api.deps import ruta_bd
from api.modelos import EstadoDelChat, Pregunta

router = APIRouter(tags=["chat"])

VARIABLE_SIN_INDICE = "TEAMS_CHAT_SIN_INDICE"


def _motor():
    """Import perezoso y tolerante, igual que el de `summarize_teams` en I2.

    `ask_teams.py` se monta como volumen en el contenedor. Si un despliegue se
    olvida de montarlo, la web tiene que seguir en pie con las otras cuatro
    paginas y decir por que el chat no esta, en vez de no arrancar.
    """
    try:
        import ask_teams

        return ask_teams, None
    except Exception as exc:  # noqa: BLE001
        return None, (
            f"No se pudo cargar el motor de preguntas ({exc}). En el "
            f"contenedor, comprueba que ask_teams.py, rag.py y llm.py estan "
            f"montados como volumen."
        )


def _usar_indice() -> bool:
    return os.environ.get(VARIABLE_SIN_INDICE, "").strip().lower() not in (
        "1", "true", "si", "sí", "yes", "on",
    )


def _abrir_indice():
    """El indice es opcional: sin el se responde con busqueda literal."""
    if not _usar_indice():
        return None
    try:
        import rag

        return rag.conectar(solo_lectura=True, entre_hilos=True)
    except Exception:  # noqa: BLE001 - no existe, o no esta sqlite-vec
        return None


def _glosario() -> str | None:
    try:
        import glosario as glosario_mod

        contenido = glosario_mod.cargar()
        return glosario_mod.texto_completo(contenido) if contenido else None
    except Exception:  # noqa: BLE001 - el glosario mejora la respuesta, no la habilita
        return None


def _sse(evento: str, dato) -> str:
    """Un evento SSE. El JSON va en una sola linea: `data:` no admite saltos."""
    return f"event: {evento}\ndata: {json.dumps(dato, ensure_ascii=False)}\n\n"


@router.post("/chat", summary="Preguntar sobre el historico (SSE)")
def chat(pregunta: Pregunta) -> StreamingResponse:
    """Responde en streaming, con las fuentes por delante del texto.

    Las fuentes se emiten **antes** que la respuesta a proposito: asi el front
    puede pintar de que se ha tirado mientras el modelo escribe, y si el
    modelo se cae a mitad, quien pregunto ve al menos de donde habria salido
    la respuesta.
    """
    motor, motivo = _motor()
    if motor is None:
        raise HTTPException(status_code=503, detail=motivo)

    return StreamingResponse(
        _generar(motor, pregunta),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # Sin esto, un proxy con buffer entrega la respuesta entera de
            # golpe al final y el streaming no se nota.
            "X-Accel-Buffering": "no",
        },
    )


def _generar(motor, pregunta: Pregunta) -> Iterator[str]:
    path = ruta_bd()
    try:
        conn = memoria.conectar(path, solo_lectura=True, entre_hilos=True)
    except FileNotFoundError:
        yield _sse("error", f"No se encuentra la base de datos en {path}.")
        return

    conn_idx = _abrir_indice()
    try:
        eventos = motor.responder_en_streaming(
            pregunta.pregunta,
            conn_hist=conn,
            conn_idx=conn_idx,
            historial=[t.model_dump() for t in pregunta.historial],
            filtros={
                "desde": pregunta.desde,
                "hasta": pregunta.hasta,
                "persona": pregunta.persona,
                "tipo": pregunta.tipo,
            },
            glosario=_glosario(),
        )
        for tipo, dato in eventos:
            yield _sse(tipo, dato)
    except Exception as exc:  # noqa: BLE001 - cualquier fallo, como evento
        yield _sse("error", f"Fallo inesperado al responder: {exc}")
    finally:
        conn.close()
        if conn_idx is not None:
            conn_idx.close()


@router.get("/chat/estado", response_model=EstadoDelChat, summary="Estado del chat")
def estado() -> EstadoDelChat:
    """Si el chat puede responder, y si lo hara con busqueda hibrida o literal.

    La pagina lo dice en pantalla: responder solo con busqueda literal da
    respuestas peores, y quien pregunta tiene derecho a saber con que se le
    esta respondiendo.
    """
    motor, motivo = _motor()
    if motor is None:
        return EstadoDelChat(disponible=False, motivo=motivo, busqueda="literal")

    datos = None
    conn_idx = _abrir_indice()
    if conn_idx is not None:
        try:
            import rag

            datos = rag.info(conn_idx)
        except Exception:  # noqa: BLE001
            datos = None
        finally:
            conn_idx.close()

    hibrida = bool(datos and datos.get("densa") and datos.get("chunks"))
    return EstadoDelChat(
        disponible=True,
        modelo=motor.Config().modelo,
        busqueda="hibrida" if hibrida else "literal",
        indice=datos,
    )
