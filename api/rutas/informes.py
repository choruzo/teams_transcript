"""Informes agregados del historico (fase I8).

Capa fina sobre `report_teams.py`, con el mismo reparto que ya tiene el motor:
**las cifras salen de SQL** y solo la narrativa la escribe el modelo.

Dos endpoints y no uno porque no tardan lo mismo:

- `GET /api/informes` devuelve el Markdown determinista en milisegundos. Es el
  mismo que imprime `python report_teams.py`: una sola fuente para la pantalla,
  la descarga y la consola. La pagina no recalcula nada, solo lo pinta.
- `GET /api/informes/narrativa` pide la lectura al modelo y la emite por SSE.
  Una narrativa de un informe mensual tarda decenas de segundos, y una peticion
  normal se quedaria colgada (o la cortaria un proxy) sin decir nada hasta el
  final. Con SSE se ve escribirse y un fallo llega como evento `error`.

La API no calcula ni una cifra: `recopilar`, `renderizar_markdown` y
`narrar_en_streaming` son de `report_teams.py`. Si se reimplementara aqui, el
informe de la web y el de por SSH podrian decir cosas distintas del mismo
periodo, que es justo lo que esta herramienta existe para evitar.
"""

import sqlite3
from collections.abc import Iterator
from datetime import date
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse

import memoria
from api.deps import conexion, glosario_del_proyecto, ruta_bd
from api.modelos import Informe
from api.rutas.reuniones import _FECHA
from api.sse import evento

router = APIRouter(tags=["informes"])

# `semana` y `mes` son la semana ISO y el mes natural **en curso**, igual que
# `--semanal` y `--mensual` del CLI. `todo` es el historico completo.
Periodo = Literal["semana", "mes", "todo"]


def _motor():
    """Import perezoso y tolerante, como el del chat.

    `report_teams.py` se monta como volumen en el contenedor. Si un despliegue
    se olvida, la web tiene que seguir en pie con las otras paginas y decir por
    que el informe no esta, en vez de no arrancar.
    """
    try:
        import report_teams

        return report_teams, None
    except Exception as exc:  # noqa: BLE001
        return None, (
            f"No se pudo cargar el motor de informes ({exc}). En el contenedor, "
            f"comprueba que report_teams.py, rag.py y llm.py estan montados "
            f"como volumen."
        )


def _referencia(referencia: str | None) -> date:
    """El dia sobre el que calcular `semana`/`mes`, hoy si no se dice otro."""
    if not referencia:
        return date.today()
    try:
        return date.fromisoformat(referencia)
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail=f"Fecha de referencia invalida: {referencia!r}. Formato AAAA-MM-DD.",
        )


def _resolver(mod, periodo: Periodo, referencia: str | None,
              desde: str | None, hasta: str | None) -> tuple[str | None, str | None]:
    """De `periodo` + `desde`/`hasta` a las dos fechas efectivas.

    Las fechas explicitas mandan sobre el atajo, igual que en el CLI (`--desde`
    gana a `--semanal`): si alguien ha escrito un rango, es lo que quiere ver.
    `todo` y el historico completo devuelven `None`, que es como `recopilar`
    entiende "sin recorte".
    """
    desde = _fecha(desde, "desde")
    hasta = _fecha(hasta, "hasta")
    if desde or hasta:
        return desde, hasta
    dia = _referencia(referencia)
    if periodo == "semana":
        return mod.periodo_semanal(dia)
    if periodo == "mes":
        return mod.periodo_mensual(dia)
    return None, None


def _fecha(valor: str | None, etiqueta: str) -> str | None:
    """Valida una fecha explicita y la devuelve normalizada.

    El `pattern` de FastAPI solo mira la forma, y `2026-13-01` la cumple. Sin
    esta comprobacion, la comparacion de texto de SQLite devolveria una lista
    vacia que se leeria como "no hubo reuniones" en vez de como un error de
    quien escribe.
    """
    if not valor:
        return None
    try:
        return date.fromisoformat(valor).isoformat()
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail=f"Fecha {etiqueta} invalida: {valor!r}. Formato AAAA-MM-DD.",
        )


def _datos(mod, conn: sqlite3.Connection, *, desde: str | None, hasta: str | None,
           persona: str | None, sugerencias: bool) -> dict:
    """Las cifras, con las sugerencias solo si se han pedido.

    `recopilar` solo incluye la seccion de duplicados si recibe la lista o un
    aviso; sin el flag, el informe no debe fingir que los ha mirado.
    """
    lista = aviso = None
    if sugerencias:
        lista, aviso = mod.cargar_sugerencias(conn)
    return mod.recopilar(
        conn,
        desde=desde,
        hasta=hasta,
        persona=persona,
        sugerencias=lista,
        aviso_sugerencias=aviso,
    )


def _validar_rango(desde: str | None, hasta: str | None) -> None:
    if desde and hasta and desde > hasta:
        raise HTTPException(
            status_code=422,
            detail=(
                f"El periodo esta al reves: desde {desde} es posterior "
                f"a hasta {hasta}."
            ),
        )


@router.get("/informes", response_model=Informe, summary="Informe del periodo")
def obtener(
    conn: sqlite3.Connection = Depends(conexion),
    periodo: Periodo = Query("mes", description="semana | mes | todo (en curso)"),
    referencia: str | None = Query(
        None, pattern=_FECHA, description="Dia sobre el que calcular semana/mes"
    ),
    desde: str | None = Query(None, pattern=_FECHA, description="Fecha ISO inclusive"),
    hasta: str | None = Query(None, pattern=_FECHA, description="Fecha ISO inclusive"),
    persona: str | None = Query(None, description="Filtra por responsable"),
    sugerencias: bool = Query(
        False,
        description=(
            "Anade los posibles duplicados ya calculados por el indice "
            "(`indexar_teams.py --acciones`)"
        ),
    ),
) -> Informe:
    """Los numeros, sin llamar al modelo: llega antes de que se pueda leer."""
    mod, motivo = _motor()
    if mod is None:
        raise HTTPException(status_code=503, detail=motivo)

    desde, hasta = _resolver(mod, periodo, referencia, desde, hasta)
    _validar_rango(desde, hasta)
    datos = _datos(
        mod, conn, desde=desde, hasta=hasta, persona=persona, sugerencias=sugerencias
    )
    return Informe(
        desde=desde,
        hasta=hasta,
        persona=persona,
        generado=datos["generado"],
        sugerencias=sugerencias,
        aviso=datos.get("aviso_sugerencias") if sugerencias else None,
        markdown=mod.renderizar_markdown(datos),
    )


@router.get(
    "/informes/narrativa",
    summary="Lectura del informe, en streaming (SSE)",
    response_class=StreamingResponse,
)
def narrativa(
    periodo: Periodo = Query("mes"),
    referencia: str | None = Query(None, pattern=_FECHA),
    desde: str | None = Query(None, pattern=_FECHA),
    hasta: str | None = Query(None, pattern=_FECHA),
    persona: str | None = Query(None),
    sugerencias: bool = Query(False),
) -> StreamingResponse:
    """La lectura en prosa, trozo a trozo.

    **No usa `Depends(conexion)`**: esa dependencia cierra la conexion cuando
    la funcion retorna y el `StreamingResponse` sigue escribiendo despues. Se
    abre y se cierra dentro del generador, igual que en el chat.

    Se repiten los filtros del informe porque la narrativa tiene que hablar
    **de lo mismo que hay en pantalla**; calcularla con otro periodo daria una
    prosa que contradice las tablas de debajo.
    """
    mod, motivo = _motor()
    if mod is None:
        raise HTTPException(status_code=503, detail=motivo)

    desde, hasta = _resolver(mod, periodo, referencia, desde, hasta)
    _validar_rango(desde, hasta)

    return StreamingResponse(
        _generar(mod, desde, hasta, persona, sugerencias),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # Sin esto, un proxy con buffer entrega la respuesta de golpe al
            # final y el streaming no se nota.
            "X-Accel-Buffering": "no",
        },
    )


def _generar(
    mod,
    desde: str | None,
    hasta: str | None,
    persona: str | None,
    sugerencias: bool,
) -> Iterator[str]:
    path = ruta_bd()
    try:
        conn = memoria.conectar(path, solo_lectura=True, entre_hilos=True)
    except FileNotFoundError:
        yield evento("error", f"No se encuentra la base de datos en {path}.")
        return

    try:
        datos = _datos(
            mod,
            conn,
            desde=desde,
            hasta=hasta,
            persona=persona,
            sugerencias=sugerencias,
        )
        # El glosario y los datos ya estan en memoria: la conexion se puede
        # cerrar antes de hablar con el modelo, que es lo que tarda.
        conn.close()
        for tipo, dato in mod.narrar_en_streaming(
            datos, glosario=glosario_del_proyecto()
        ):
            yield evento(tipo, dato)
    except Exception as exc:  # noqa: BLE001 - cualquier fallo, como evento
        yield evento("error", f"Fallo inesperado al generar el informe: {exc}")
    finally:
        conn.close()
