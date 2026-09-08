"""Diagnostico del despliegue.

Deliberadamente **no** depende de `deps.conexion`: si la base no se puede
abrir, este endpoint tiene que seguir respondiendo y explicar por que. Un
healthcheck que devuelve 503 cuando falla lo que intenta diagnosticar no sirve
de nada.
"""

from fastapi import APIRouter

import memoria
from api import VERSION
from api.deps import ruta_bd, solo_lectura
from api.modelos import Salud

router = APIRouter()


@router.get("/salud", response_model=Salud, summary="Estado del servicio")
def salud() -> Salud:
    path = ruta_bd()
    base = Salud(
        version=VERSION,
        solo_lectura=solo_lectura(),
        base_de_datos=str(path),
        base_accesible=False,
        esquema_esperado=memoria.ESQUEMA_VERSION,
    )
    if not path.exists():
        base.detalle = (
            "No existe el fichero. Comprueba TEAMS_DB y, en Docker, que el "
            "volumen este montado."
        )
        return base

    try:
        # Siempre en solo lectura, aunque la API no lo este: diagnosticar no
        # puede tener el efecto secundario de migrar la base.
        conn = memoria.conectar(path, solo_lectura=True)
    except Exception as exc:  # noqa: BLE001 - cualquier fallo es informativo
        base.detalle = f"No se pudo abrir: {exc}"
        return base

    try:
        datos = memoria.resumen_bd(conn)
    finally:
        conn.close()

    base.base_accesible = True
    for clave, valor in datos.items():
        setattr(base, clave, valor)
    if base.esquema != memoria.ESQUEMA_VERSION:
        base.detalle = (
            f"La base esta en el esquema {base.esquema} y el codigo espera "
            f"{memoria.ESQUEMA_VERSION}. Se migrara sola en la proxima "
            f"ejecucion de summarize_teams.py."
        )
    return base
