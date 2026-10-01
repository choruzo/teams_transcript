"""Aplicacion FastAPI: `uvicorn api.main:app`.

Un solo proceso sirve la API bajo `/api` y la pagina estatica de `web/` en la
raiz. Sin nginx delante mientras no haya un problema medido: es una pieza mas
que configurar y exportar a un servidor sin internet, para servir un punado de
ficheros en la red local.
"""

import sys
from contextlib import asynccontextmanager
from pathlib import Path

# Permite `uvicorn api.main:app` desde cualquier directorio y, sobre todo,
# `import memoria` desde dentro del paquete: el modulo vive en la raiz del
# repo, junto al resto del pipeline.
RAIZ = Path(__file__).resolve().parents[1]
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

from fastapi import APIRouter, FastAPI, Request  # noqa: E402
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

import autenticacion  # noqa: E402
from api import VERSION  # noqa: E402
from api.deps import (  # noqa: E402
    autenticacion_activa,
    directorio_web,
    usuario_de_peticion,
)
from api.rutas import (  # noqa: E402
    acciones,
    auth,
    busqueda,
    chat,
    metricas,
    personas,
    reuniones,
    salud,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Siembra el usuario inicial al arrancar, si no hay ninguno.

    Es comodo y ruidoso a proposito: en el primer arranque el log dice que se
    ha creado `admin` con la clave por defecto y como cambiarla. Con la
    autenticacion apagada no se toca la base de usuarios.
    """
    if autenticacion_activa():
        try:
            autenticacion.sembrar_usuario_inicial()
        except Exception as exc:  # noqa: BLE001 - no puede impedir arrancar
            print(f"[autenticacion] No se pudo sembrar el usuario: {exc}")
    yield


app = FastAPI(
    title="Memoria del equipo",
    description="Historico de reuniones: transcripciones, acciones y riesgos.",
    version=VERSION,
    lifespan=lifespan,
    # Sin CORS: el front se sirve desde este mismo origen. Si algun dia hace
    # falta abrirlo, que sea una decision consciente y no una herencia.
)

# Rutas que no exigen sesion. Los assets (`/css/`, `/js/`) se sirven sin
# sesion a proposito: la pagina de login los necesita para pintarse y no
# contienen ningun dato. `/api/salud` queda abierto porque es el healthcheck
# de Docker y el diagnostico de un despliegue a ciegas.
_RUTAS_LIBRES = {"/api/login", "/api/salud", "/login.html", "/favicon.svg", "/favicon.ico"}
_PREFIJOS_LIBRES = ("/css/", "/js/", "/favicon")

api = APIRouter(prefix="/api")
api.include_router(salud.router, tags=["salud"])
api.include_router(auth.router)
api.include_router(reuniones.router)
api.include_router(metricas.router)
api.include_router(acciones.router)
api.include_router(personas.router)
api.include_router(busqueda.router)
api.include_router(chat.router)
app.include_router(api)


def _es_libre(ruta: str) -> bool:
    return ruta in _RUTAS_LIBRES or ruta.startswith(_PREFIJOS_LIBRES)


@app.middleware("http")
async def exigir_sesion(request: Request, call_next):
    """Protege todo salvo lo publico: 401 en la API y redireccion en el HTML.

    La distincion importa para el front: al `fetch` de `api.js` le sirve un 401
    en JSON para redirigir a `/login.html`, mientras que abrir una pagina a
    mano debe llevar a la pantalla de login en vez de mostrar el JSON.
    """
    if not autenticacion_activa() or _es_libre(request.url.path):
        return await call_next(request)
    if usuario_de_peticion(request) is not None:
        return await call_next(request)

    if request.url.path.startswith("/api/"):
        return JSONResponse(
            {"detail": "Sesion no valida o caducada."}, status_code=401
        )
    destino = request.url.path
    if request.url.query:
        destino = f"{destino}?{request.url.query}"
    # Detras del portal, nginx quita /app5/ antes de reenviar, asi que sin esto
    # el `next` seria "/" y tras entrar se caeria en el portal en vez de en la
    # app. Solo lo manda el location /app5/; en acceso directo no hay cabecera.
    prefijo = request.headers.get("X-Forwarded-Prefix", "").rstrip("/")
    if prefijo and not destino.startswith(prefijo):
        destino = f"{prefijo}{destino}"
    from urllib.parse import quote

    return RedirectResponse(
        f"/login.html?next={quote(destino, safe='')}", status_code=303
    )


_web = directorio_web()


@app.get("/favicon.ico", include_in_schema=False)
def favicon() -> Response:
    """El navegador lo pide siempre; sin esto, un 404 en cada carga."""
    icono = _web / "favicon.svg"
    if icono.exists():
        return FileResponse(icono, media_type="image/svg+xml")
    return Response(status_code=204)


# El montaje va **el ultimo**: `/` coincide con cualquier ruta, asi que
# eclipsaria a todo lo que se registre despues (incluido /favicon.ico, que ya
# nos costo un 404).
if _web.is_dir():
    app.mount("/", StaticFiles(directory=_web, html=True), name="web")
else:  # pragma: no cover - solo en un despliegue mal montado

    @app.get("/", include_in_schema=False)
    def sin_front() -> dict:
        return {
            "aviso": f"No se encuentra el directorio web en {_web}.",
            "api": "/api/salud",
        }
