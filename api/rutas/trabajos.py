"""Subir y transcribir desde la web (fase I10b).

La API **no lanza el pipeline**: solo escribe en la cola de `trabajos.py` y
sirve su estado. El trabajador (`trabajador_teams.py`) corre en el host, fuera
de Docker, porque necesita la GPU, Whisper y `systemctl`. Si la API lanzara el
subproceso, una transcripcion de una hora bloquearia el bucle de peticiones y
el contenedor no puede parar el `llama-server`.

La subida va **por trozos** y no en un unico POST: un `.wav` de una hora ronda
los cientos de MB, y una conexion que se corta a mitad no puede obligar a
empezar de cero. Cada `PUT` es un trozo de 16 MB con cuerpo crudo; el servidor
confirma los bytes en la base y el navegador reanuda por el ultimo confirmado.
No se usa `multipart/form-data` a proposito: exigiria `python-multipart` dentro
de la imagen y no aporta nada que no tengamos ya.
"""

import json
import os
import shutil
import sqlite3
import time
from collections.abc import Iterator

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

try:
    import trabajos
except Exception:  # noqa: BLE001 - la web sigue en pie sin este modulo
    trabajos = None

from api.deps import conexion_trabajos, subidas_activas
from api.modelos import (
    EstadoDelTrabajador,
    EstadoSubida,
    Finalizacion,
    NuevoTrabajo,
    PaginaDeTrabajos,
    Trabajo,
    TrabajoCreado,
)
from api.sse import evento

router = APIRouter(tags=["trabajos"])


def _exigir_subidas() -> None:
    """Con `TEAMS_SUBIDAS=0` se explica, no se finge que no existe la ruta."""
    _exigir_modulo()
    if not subidas_activas():
        raise HTTPException(
            status_code=503,
            detail=(
                "La subida de grabaciones esta apagada. Arranca la API con "
                "TEAMS_SUBIDAS=1 cuando el trabajador del host este instalado; "
                "sin el, el fichero se quedaria en la cola para siempre."
            ),
        )


def _exigir_modulo():
    if trabajos is None:
        raise HTTPException(
            status_code=503,
            detail=(
                "El modulo de la cola de subidas no esta disponible en el "
                "contenedor. Monta trabajos.py como volumen (ver "
                "docker-compose.yml)."
            ),
        )
    return trabajos


def _modelo(fila: dict) -> Trabajo:
    datos = dict(fila)
    try:
        datos["opciones"] = json.loads(datos.get("opciones") or "{}")
    except (TypeError, ValueError):
        datos["opciones"] = {}
    # Pydantic ignora los campos internos (ruta_parcial, audio_path, cancelar,
    # latido...). El contrato publico no expone rutas del servidor.
    return Trabajo(**datos)


def _creado(fila: dict) -> TrabajoCreado:
    base = _modelo(fila)
    return TrabajoCreado(**base.model_dump(), siguiente=0)


def _traducir(exc: Exception) -> HTTPException:
    if trabajos is None:
        return HTTPException(status_code=503, detail="La cola de subidas no esta disponible.")
    if isinstance(exc, trabajos.TrabajoNoEncontrado):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, trabajos.TrabajoDuplicado):
        return HTTPException(
            status_code=409,
            detail={
                "mensaje": str(exc),
                "uid": exc.uid,
                "reunion_uid": exc.reunion_uid,
                "estado": exc.estado,
            },
        )
    if isinstance(exc, trabajos.ParteFueraDeOrden):
        return HTTPException(
            status_code=409,
            detail={"mensaje": str(exc), "esperado": exc.esperado},
        )
    if isinstance(exc, trabajos.SinEspacio):
        return HTTPException(status_code=507, detail=str(exc))
    if isinstance(exc, trabajos.TrabajoInvalido):
        return HTTPException(status_code=422, detail=str(exc))
    if isinstance(exc, OSError):
        return HTTPException(
            status_code=503,
            detail=(
                f"No se puede escribir en {trabajos.ruta_subidas()}: {exc}. "
                "Crea el directorio en el host con el usuario del contenedor "
                f"(mkdir -p subidas) antes de arrancar el compose."
            ),
        )
    raise exc


@router.get(
    "/trabajos/estado",
    response_model=EstadoDelTrabajador,
    summary="Si se puede subir y si el trabajador esta vivo",
)
def estado() -> EstadoDelTrabajador:
    _exigir_modulo()
    base = EstadoDelTrabajador(
        disponible=subidas_activas(),
        maximo_bytes=trabajos.tamano_maximo(),
        trozo_bytes=trabajos.tamano_trozo(),
    )
    if not base.disponible:
        base.motivo = (
            "Las subidas estan apagadas (TEAMS_SUBIDAS=1). Mientras tanto, "
            "procesa la grabacion en el servidor con procesar_teams.py."
        )
        return base

    try:
        conn = trabajos.conectar(entre_hilos=True)
    except (OSError, sqlite3.Error) as exc:
        base.motivo = f"No se puede abrir la cola de trabajos: {exc}"
        return base
    try:
        vivo, fila = trabajos.trabajador_vivo(conn)
        base.trabajador = vivo
        base.latido_en = fila["latido_en"] if fila else None
        base.en_cola = trabajos.contar_en_cola(conn)
        base.en_curso = trabajos.contar_en_curso(conn)
    finally:
        conn.close()

    try:
        base.libre_bytes = shutil.disk_usage(trabajos.ruta_subidas()).free
    except OSError:
        base.libre_bytes = None
        directorio = trabajos.ruta_subidas()
        # Si no existe pero su padre es escribible, `crear` la creara. Solo es
        # un problema real si existe y no se puede escribir (Docker la creo de
        # root) o si tampoco se puede escribir el padre.
        if directorio.exists() or not os.access(directorio.parent, os.W_OK):
            base.motivo = (
                f"No existe o no se puede escribir {directorio}. Crea "
                "`subidas/` en el host con el usuario del contenedor antes de "
                "arrancar el compose."
            )
    return base


@router.post(
    "/trabajos",
    response_model=TrabajoCreado,
    status_code=201,
    summary="Reservar una subida",
)
def crear(
    cuerpo: NuevoTrabajo,
    conn: sqlite3.Connection = Depends(conexion_trabajos),
) -> TrabajoCreado:
    """Comprueba el espacio y devuelve por que trozo empezar (siempre el 0)."""
    _exigir_subidas()
    opciones = {
        "diarizar": cuerpo.diarizar,
        "modelo": cuerpo.modelo,
        "titulo": cuerpo.titulo,
    }
    try:
        fila = trabajos.crear(
            conn,
            nombre_original=cuerpo.nombre,
            tamano=cuerpo.tamano,
            fecha_reunion=cuerpo.fecha,
            hora_reunion=cuerpo.hora,
            tipo=cuerpo.tipo,
            opciones=opciones,
        )
    except (trabajos.TrabajoInvalido, OSError) as exc:
        raise _traducir(exc)
    return _creado(fila)


@router.get(
    "/trabajos/{uid}/subida",
    response_model=EstadoSubida,
    summary="Por donde va la subida",
)
def estado_subida(
    uid: str, conn: sqlite3.Connection = Depends(conexion_trabajos)
) -> EstadoSubida:
    """Es lo que consulta el navegador para reanudar tras un corte."""
    _exigir_modulo()
    try:
        return EstadoSubida(**trabajos.estado_subida(conn, uid))
    except Exception as exc:  # noqa: BLE001 - se traduce a HTTP
        raise _traducir(exc)


async def _leer_acotado(peticion: Request, maximo: int) -> bytes:
    """Lee el cuerpo sin fiarse de Content-Length ni de un chunked sin tope.

    Un `await request.body()` de un cliente malicioso podria cargar gigas en
    memoria. Aqui se corta en cuanto se pasa del tamano de un trozo.
    """
    trozos: list[bytes] = []
    total = 0
    async for bloque in peticion.stream():
        total += len(bloque)
        if total > maximo:
            raise HTTPException(
                status_code=413,
                detail=f"Un trozo no puede pasar de {maximo} bytes.",
            )
        trozos.append(bloque)
    return b"".join(trozos)


@router.put(
    "/trabajos/{uid}/partes/{indice}",
    response_model=EstadoSubida,
    summary="Subir un trozo (cuerpo crudo)",
)
async def subir_parte(
    uid: str,
    indice: int,
    peticion: Request,
    x_sha256: str | None = Header(None, alias="X-Sha256"),
    conn: sqlite3.Connection = Depends(conexion_trabajos),
) -> EstadoSubida:
    """El cuerpo es el trozo tal cual, sin multipart ni base64.

    El tope se comprueba antes y despues de leer: `Content-Length` se puede
    mentir y un cuerpo sin longitud se leeria hasta el infinito.
    """
    _exigir_subidas()
    try:
        maximo = int(trabajos.detalle(conn, uid)["trozo"])
    except Exception as exc:  # noqa: BLE001 - 404 o base ocupada
        raise _traducir(exc)
    largo = peticion.headers.get("content-length")
    if largo:
        try:
            if int(largo) > maximo:
                raise HTTPException(
                    status_code=413,
                    detail=f"Un trozo no puede pasar de {maximo} bytes.",
                )
        except ValueError:
            raise HTTPException(status_code=400, detail="Content-Length no valido.")
    datos = await _leer_acotado(peticion, maximo)
    if len(datos) > maximo:
        raise HTTPException(
            status_code=413, detail=f"Un trozo no puede pasar de {maximo} bytes."
        )
    try:
        return EstadoSubida(
            **trabajos.anotar_parte(conn, uid, indice, datos, sha256=x_sha256)
        )
    except Exception as exc:  # noqa: BLE001 - se traduce a HTTP
        raise _traducir(exc)


@router.post(
    "/trabajos/{uid}/finalizar",
    response_model=Trabajo,
    summary="Validar el WAV y ponerlo en la cola",
)
def finalizar(
    uid: str,
    cuerpo: Finalizacion | None = None,
    conn: sqlite3.Connection = Depends(conexion_trabajos),
) -> Trabajo:
    """Calcula el sha256, valida el RIFF/WAVE y detecta duplicados.

    Repetir la misma grabacion devuelve 409 con el uid de la reunion que ya
    existe, salvo que se pida `forzar`. Es la forma de que subir dos veces por
    error no genere dos reuniones con las mismas acciones.
    """
    _exigir_subidas()
    forzar = bool(cuerpo and cuerpo.forzar)
    try:
        return _modelo(trabajos.finalizar(conn, uid, forzar=forzar))
    except Exception as exc:  # noqa: BLE001 - se traduce a HTTP
        raise _traducir(exc)


@router.get("/trabajos", response_model=PaginaDeTrabajos, summary="Ultimos trabajos")
def listar(
    limite: int = Query(25, ge=1, le=100),
    estado: list[str] | None = Query(None, description="Repetible"),
    conn: sqlite3.Connection = Depends(conexion_trabajos),
) -> PaginaDeTrabajos:
    _exigir_modulo()
    if estado:
        desconocidos = [e for e in estado if e not in trabajos.ESTADOS]
        if desconocidos:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Estados desconocidos: {desconocidos}. "
                    f"Validos: {list(trabajos.ESTADOS)}"
                ),
            )
    return PaginaDeTrabajos(
        trabajos=[
            _modelo(fila)
            for fila in trabajos.listar(conn, limite=limite, estados=estado)
        ]
    )


@router.get("/trabajos/{uid}", response_model=Trabajo, summary="Ficha de un trabajo")
def ficha(
    uid: str, conn: sqlite3.Connection = Depends(conexion_trabajos)
) -> Trabajo:
    _exigir_modulo()
    try:
        return _modelo(trabajos.detalle(conn, uid))
    except Exception as exc:  # noqa: BLE001
        raise _traducir(exc)


@router.post(
    "/trabajos/{uid}/cancelar", response_model=Trabajo, summary="Cancelar un trabajo"
)
def cancelar(
    uid: str, conn: sqlite3.Connection = Depends(conexion_trabajos)
) -> Trabajo:
    """En cola se cancela ya; en ejecucion se pide al trabajador que pare."""
    _exigir_modulo()
    try:
        return _modelo(trabajos.cancelar(conn, uid))
    except Exception as exc:  # noqa: BLE001
        raise _traducir(exc)


@router.post(
    "/trabajos/{uid}/reintentar",
    response_model=Trabajo,
    summary="Volver a encolar lo que fallo",
)
def reintentar(
    uid: str, conn: sqlite3.Connection = Depends(conexion_trabajos)
) -> Trabajo:
    _exigir_modulo()
    try:
        return _modelo(trabajos.reintentar(conn, uid))
    except Exception as exc:  # noqa: BLE001
        raise _traducir(exc)


def _eventos(uid: str) -> Iterator[str]:
    """Estado y cola del log, hasta que el trabajo termina.

    La conexion se abre y se cierra **dentro** del generador, igual que en el
    chat: `Depends` la cerraria al devolver la respuesta y el stream seguiria
    produciendo datos sobre una conexion cerrada.
    """
    try:
        conn = trabajos.conectar(entre_hilos=True)
    except (OSError, sqlite3.Error) as exc:
        yield evento("error", f"No se puede abrir la cola de trabajos: {exc}")
        return
    try:
        visto = None
        tamano_log = -1
        ultimo_latido = 0.0
        while True:
            try:
                fila = trabajos.detalle(conn, uid)
            except trabajos.TrabajoNoEncontrado:
                yield evento("error", f"No existe el trabajo {uid}.")
                return
            datos = _modelo(fila).model_dump()
            if datos != visto:
                visto = datos
                yield evento("estado", datos)
            lineas, tamano = trabajos.leer_log(uid)
            if tamano != tamano_log:
                tamano_log = tamano
                yield evento("log", {"lineas": lineas, "bytes": tamano})
            if fila["estado"] in trabajos.ESTADOS_TERMINALES:
                yield evento("fin", datos)
                return
            # Un latido aunque no cambie nada: mantiene viva la conexion y,
            # sobre todo, le da a Starlette un punto donde notar que el cliente
            # se ha ido. Sin el, un trabajo parado dejaria el generador
            # durmiendo para siempre.
            if time.monotonic() - ultimo_latido >= 10:
                ultimo_latido = time.monotonic()
                yield evento("latido", {"estado": fila["estado"]})
            time.sleep(1)
    finally:
        conn.close()


@router.get(
    "/trabajos/{uid}/eventos",
    summary="Progreso por SSE",
    response_class=StreamingResponse,
)
def eventos(uid: str) -> StreamingResponse:
    _exigir_modulo()
    return StreamingResponse(
        _eventos(uid),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # Sin esto nginx acumula la respuesta y el progreso llega de golpe.
            "X-Accel-Buffering": "no",
        },
    )


@router.get(
    "/trabajos/{uid}/log",
    summary="Log completo del trabajo",
    response_class=StreamingResponse,
)
def log(uid: str) -> StreamingResponse:
    """Para depurar un fallo sin abrir una shell en el servidor."""
    _exigir_modulo()
    ruta = trabajos.ruta_log(uid)
    texto, _ = trabajos.leer_log(uid, maximo=256 * 1024)
    if not texto and not ruta.exists():
        raise HTTPException(status_code=404, detail="Ese trabajo no tiene log.")
    return StreamingResponse(
        iter([texto]), media_type="text/plain; charset=utf-8"
    )
