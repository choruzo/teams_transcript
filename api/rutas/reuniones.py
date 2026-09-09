"""Listado, consulta y descarga de reuniones."""

import json
import re
import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import PlainTextResponse

import memoria
from api.deps import conexion
from api.modelos import (
    AccionDeReunion,
    Hablante,
    Intervencion,
    PaginaDeReuniones,
    PaginaDeSegmentos,
    Reunion,
    ReunionDetalle,
    RiesgoDeReunion,
    SeccionExtra,
    Segmento,
)

router = APIRouter(prefix="/reuniones", tags=["reuniones"])

# Formato ISO de fecha. Se valida en la entrada para que un filtro mal escrito
# devuelva 422 con explicacion, y no una lista vacia que parece un fallo de
# datos.
_FECHA = r"^\d{4}-\d{2}-\d{2}$"


def a_modelo(fila: sqlite3.Row) -> Reunion:
    """Fila de `listar_reuniones` -> contrato publico. Lo comparte el timeline."""
    return Reunion(
        uid=fila["uid"],
        fecha=fila["fecha"],
        titulo=fila["titulo"],
        tipo=fila["tipo"],
        duracion_seg=fila["duracion_seg"],
        resumen=fila["resumen"],
        # Solo si la fila tiene ruta; que el fichero exista de verdad es cosa
        # de I7, que es quien va a servirlo.
        tiene_audio=bool(fila["audio_path"]),
        n_segmentos=fila["n_segmentos"],
        n_acciones=fila["n_acciones"],
        n_riesgos=fila["n_riesgos"],
        n_updates=fila["n_updates"],
    )


@router.get("", response_model=PaginaDeReuniones, summary="Lista de reuniones")
def listar(
    conn: sqlite3.Connection = Depends(conexion),
    desde: str | None = Query(None, pattern=_FECHA, description="Fecha ISO inclusive"),
    hasta: str | None = Query(None, pattern=_FECHA, description="Fecha ISO inclusive"),
    tipo: str | None = Query(None, description="daily | workshop | retro | planning"),
    limite: int = Query(50, ge=1, le=200),
    desplazamiento: int = Query(0, ge=0),
) -> PaginaDeReuniones:
    if tipo is not None and tipo not in memoria.TIPOS_REUNION:
        raise HTTPException(
            status_code=422,
            detail=f"Tipo desconocido: {tipo!r}. Validos: {list(memoria.TIPOS_REUNION)}",
        )
    filtros = {"desde": desde, "hasta": hasta, "tipo": tipo}
    filas = memoria.listar_reuniones(
        conn, limite=limite, desplazamiento=desplazamiento, **filtros
    )
    return PaginaDeReuniones(
        total=memoria.contar_reuniones(conn, **filtros),
        limite=limite,
        desplazamiento=desplazamiento,
        reuniones=[a_modelo(f) for f in filas],
    )


# --------------------------------------------------------------------------
# I2: la reunion por dentro
# --------------------------------------------------------------------------

# Cuantos segmentos se sirven de una vez. Una hora de reunion son ~350 y la
# pagina los pinta todos de golpe sin despeinarse; el tope existe para que una
# transcripcion de tres horas no llegue en una sola respuesta de varios MB.
SEGMENTOS_POR_PAGINA = 500
TOPE_SEGMENTOS = 2000

_CLAVES_MARKDOWN = ("hablantes", "por_persona", "acciones", "arrastres", "riesgos")


def _fila_reunion(conn: sqlite3.Connection, uid: str) -> sqlite3.Row:
    """La fila completa de `meetings`, o un 404 que dice que uid no existe."""
    fila = memoria.reunion_por_uid(conn, uid)
    if fila is None:
        raise HTTPException(
            status_code=404, detail=f"No hay ninguna reunion con uid {uid!r}"
        )
    return fila


def _datos_json(fila: sqlite3.Row) -> dict:
    try:
        datos = json.loads(fila["datos_json"] or "{}")
    except (TypeError, ValueError):
        return {}
    return datos if isinstance(datos, dict) else {}


def _secciones_extra(tipo: str, datos: dict) -> list[SeccionExtra]:
    """Las secciones propias de retro/planning/workshop, desde `datos_json`.

    Los titulos viven en `summarize_teams.TIPOS`, que es quien los inventa: si
    se copiaran aqui, anadir un tipo obligaria a tocar dos sitios y acabarian
    divergiendo. El import es perezoso y tolerante a fallos a proposito: la web
    no debe caerse porque un script del pipeline no se pueda importar en el
    contenedor, donde no esta garantizado que existan sus dependencias.
    """
    try:
        import summarize_teams
    except Exception:  # pragma: no cover - solo en un despliegue incompleto
        return []
    definicion = summarize_teams.TIPOS.get(tipo)
    if not definicion or not definicion["clave"]:
        return []
    bloque = datos.get(definicion["clave"])
    if not isinstance(bloque, dict):
        return []
    secciones = []
    for campo, titulo in definicion["secciones"]:
        puntos = [str(p).strip() for p in (bloque.get(campo) or []) if str(p).strip()]
        if puntos:
            secciones.append(SeccionExtra(titulo=titulo, puntos=puntos))
    return secciones


def _comentarios_de_arrastre(datos: dict) -> dict[int, str]:
    """{action_id: comentario} de lo que el modelo dijo al arrastrar.

    El comentario es lo unico del bloque de arrastres que la base no guarda
    (D10), y es justo lo que explica por que algo sigue abierto. Se recupera
    del JSON por `action_id`, que si sobrevive al reprocesado, porque la accion
    pertenece a otra reunion.
    """
    comentarios: dict[int, str] = {}
    for arrastre in datos.get("arrastres") or []:
        if not isinstance(arrastre, dict):
            continue
        try:
            action_id = int(arrastre.get("action_id"))
        except (TypeError, ValueError):
            continue
        comentario = str(arrastre.get("comentario") or "").strip()
        if comentario:
            comentarios[action_id] = comentario
    return comentarios


def _a_accion(fila: sqlite3.Row, comentarios: dict[int, str]) -> AccionDeReunion:
    return AccionDeReunion(**dict(fila), comentario=comentarios.get(fila["id"]))


@router.get("/{uid}", response_model=ReunionDetalle, summary="Una reunion por su uid")
def obtener(uid: str, conn: sqlite3.Connection = Depends(conexion)) -> ReunionDetalle:
    """La reunion entera menos la transcripcion, que va aparte y paginada.

    Se direcciona por `uid` y nunca por `id`: el `id` cambia si la reunion se
    reprocesa; el `uid` no. Un enlace guardado o una cita del chat tienen que
    seguir funcionando despues de reprocesar (deuda D1, resuelta en D0).

    **El contenido sale de las tablas, no de `datos_json`**, salvo las
    secciones propias del tipo y los comentarios de arrastre, que no tienen
    tabla. El JSON es lo que dijo el modelo aquel dia; las tablas son lo que el
    historico da por bueno hoy, que es lo que la fase I6 permitira corregir.
    """
    fila = _fila_reunion(conn, uid)
    meeting_id = fila["id"]
    datos = _datos_json(fila)
    comentarios = _comentarios_de_arrastre(datos)
    # La misma consulta del listado, para que los recuentos de la cabecera
    # salgan de un unico sitio y no puedan discrepar de los de la tarjeta.
    resumida = memoria.listar_reuniones(conn, uid=uid, limite=1)[0]

    return ReunionDetalle(
        **a_modelo(resumida).model_dump(),
        transcript_path=fila["transcript_path"],
        modelo_whisper=fila["modelo_whisper"],
        modelo_llm=fila["modelo_llm"],
        creado_en=fila["creado_en"],
        hablantes=[
            Hablante(**dict(f)) for f in memoria.hablantes_de_reunion(conn, meeting_id)
        ],
        intervenciones=[
            Intervencion(**dict(f))
            for f in memoria.updates_de_reunion(conn, meeting_id)
        ],
        acciones=[
            _a_accion(f, comentarios)
            for f in memoria.acciones_de_reunion(conn, meeting_id)
        ],
        arrastres=[
            _a_accion(f, comentarios)
            for f in memoria.arrastres_de_reunion(conn, meeting_id)
        ],
        riesgos=[
            RiesgoDeReunion(**dict(f))
            for f in memoria.riesgos_de_reunion(conn, meeting_id)
        ],
        secciones=_secciones_extra(fila["tipo"], datos),
        tiene_markdown=bool(datos),
    )


@router.get(
    "/{uid}/segmentos",
    response_model=PaginaDeSegmentos,
    summary="Transcripcion con hablantes y tiempos",
)
def segmentos(
    uid: str,
    conn: sqlite3.Connection = Depends(conexion),
    limite: int = Query(SEGMENTOS_POR_PAGINA, ge=1, le=TOPE_SEGMENTOS),
    desplazamiento: int = Query(0, ge=0),
) -> PaginaDeSegmentos:
    """Paginada siempre, incluso con la reunion mas corta.

    Es la regla del plan para `segmentos` y `buscar`: hoy una reunion son ~350
    lineas, pero el limite no puede depender de que sigan siendo pocas.
    """
    fila = _fila_reunion(conn, uid)
    filas = memoria.segmentos_de_reunion(
        conn, fila["id"], limite=limite, desplazamiento=desplazamiento
    )
    return PaginaDeSegmentos(
        total=memoria.contar_segmentos(conn, fila["id"]),
        limite=limite,
        desplazamiento=desplazamiento,
        # Se mira lo devuelto y no la reunion entera: es lo que el front tiene
        # delante para decidir si pinta la columna de tiempos.
        con_tiempos=any(f["inicio"] is not None for f in filas),
        segmentos=[Segmento(**dict(f)) for f in filas],
    )


@router.get(
    "/{uid}/markdown",
    response_class=PlainTextResponse,
    summary="El resumen en Markdown, reconstruido",
)
def markdown(
    uid: str, conn: sqlite3.Connection = Depends(conexion)
) -> PlainTextResponse:
    """Regenera el `.md` desde la base en vez de servir el fichero del disco.

    El `.md` de `grabaciones/` puede estar en otra maquina o no existir, y en
    cuanto la fase I6 permita corregir datos quedara obsoleto sin que nadie lo
    regenere. La base es la fuente de verdad y el Markdown un artefacto de
    exportacion. Se reutiliza `renderizar_markdown` de `summarize_teams.py`
    para que la descarga y el fichero del pipeline no puedan divergir.
    """
    fila = _fila_reunion(conn, uid)
    datos = _datos_json(fila)
    if not datos:
        raise HTTPException(
            status_code=404,
            detail=(
                "Esta reunion no tiene datos_json: se proceso con una version "
                "anterior del pipeline y el resumen no se puede reconstruir."
            ),
        )
    try:
        import summarize_teams
    except Exception as error:  # pragma: no cover - despliegue incompleto
        raise HTTPException(
            status_code=503, detail=f"No se puede cargar summarize_teams: {error}"
        )
    # `renderizar_markdown` espera el diccionario ya normalizado y accede a sus
    # claves con []; un `datos_json` de una version anterior puede no tenerlas
    # todas, asi que se completan antes en vez de arriesgar un KeyError.
    completo = dict(datos)
    for clave in _CLAVES_MARKDOWN:
        if not isinstance(completo.get(clave), list):
            completo[clave] = []
    completo["resumen"] = str(datos.get("resumen") or "")
    tipo = fila["tipo"] if fila["tipo"] in summarize_teams.TIPOS else "daily"
    texto = summarize_teams.renderizar_markdown(
        completo, tipo, fila["fecha"], fila["titulo"]
    )
    return PlainTextResponse(
        texto,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": _adjunto(uid, "md")},
    )


@router.get(
    "/{uid}/srt",
    response_class=PlainTextResponse,
    summary="La transcripcion en formato .srt",
)
def srt(uid: str, conn: sqlite3.Connection = Depends(conexion)) -> PlainTextResponse:
    """El `.srt` reconstruido desde `segments`, no leido del disco.

    Mismo motivo que el Markdown: el fichero original vive en la maquina que
    transcribio, y aqui solo hay base de datos.
    """
    fila = _fila_reunion(conn, uid)
    filas = memoria.segmentos_de_reunion(conn, fila["id"], limite=TOPE_SEGMENTOS)
    if not filas:
        raise HTTPException(
            status_code=404, detail="Esta reunion no tiene transcripcion."
        )
    if all(f["inicio"] is None for f in filas):
        raise HTTPException(
            status_code=409,
            detail=(
                "Los segmentos no tienen marcas de tiempo: la reunion se "
                "proceso sin el .srt hermano y no se puede reconstruir."
            ),
        )
    return PlainTextResponse(
        _a_srt(filas),
        media_type="application/x-subrip; charset=utf-8",
        headers={"Content-Disposition": _adjunto(uid, "srt")},
    )


def _adjunto(uid: str, extension: str) -> str:
    """El uid ya esta normalizado a [a-z0-9_-], pero el nombre acaba en una
    cabecera HTTP: se filtra igualmente en vez de confiar."""
    nombre = re.sub(r"[^A-Za-z0-9_.-]", "_", uid) or "reunion"
    return f'attachment; filename="{nombre}.{extension}"'


def _marca_srt(segundos: float) -> str:
    total_ms = int(round(segundos * 1000))
    horas, resto = divmod(total_ms, 3_600_000)
    minutos, resto = divmod(resto, 60_000)
    segs, ms = divmod(resto, 1000)
    return f"{horas:02d}:{minutos:02d}:{segs:02d},{ms:03d}"


def _a_srt(filas: list[sqlite3.Row]) -> str:
    """Mismo formato que escribe `transcribe_teams.py`, hablante incluido."""
    bloques = []
    for numero, f in enumerate(filas, start=1):
        if f["inicio"] is None:
            continue
        fin = f["fin"] if f["fin"] is not None else f["inicio"]
        quien = f["persona"] or f["etiqueta"]
        texto = f"[{quien}] {f['texto']}" if quien else f["texto"]
        bloques.append(
            f"{numero}\n{_marca_srt(f['inicio'])} --> {_marca_srt(fin)}\n{texto}\n"
        )
    return "\n".join(bloques)
