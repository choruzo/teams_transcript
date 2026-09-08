"""Aplicacion FastAPI: `uvicorn api.main:app`.

Un solo proceso sirve la API bajo `/api` y la pagina estatica de `web/` en la
raiz. Sin nginx delante mientras no haya un problema medido: es una pieza mas
que configurar y exportar a un servidor sin internet, para servir un punado de
ficheros en la red local.
"""

import sys
from pathlib import Path

# Permite `uvicorn api.main:app` desde cualquier directorio y, sobre todo,
# `import memoria` desde dentro del paquete: el modulo vive en la raiz del
# repo, junto al resto del pipeline.
RAIZ = Path(__file__).resolve().parents[1]
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

from fastapi import APIRouter, FastAPI  # noqa: E402
from fastapi.responses import FileResponse, Response  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

from api import VERSION  # noqa: E402
from api.deps import directorio_web  # noqa: E402
from api.rutas import metricas, reuniones, salud  # noqa: E402

app = FastAPI(
    title="Memoria del equipo",
    description="Historico de reuniones: transcripciones, acciones y riesgos.",
    version=VERSION,
    # Sin CORS: el front se sirve desde este mismo origen. Si algun dia hace
    # falta abrirlo, que sea una decision consciente y no una herencia.
)

api = APIRouter(prefix="/api")
api.include_router(salud.router, tags=["salud"])
api.include_router(reuniones.router)
api.include_router(metricas.router)
app.include_router(api)

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
