"""Contrato de la API.

Los modelos son el contrato con el front: si un campo cambia de nombre aqui,
hay que cambiarlo en `web/js/`. Por eso se declaran explicitamente en vez de
devolver la fila de SQLite tal cual, que ataria la URL publica al esquema
interno de la base.
"""

from pydantic import BaseModel, Field


class Reunion(BaseModel):
    uid: str = Field(description="Identidad estable, sobrevive al reprocesado")
    fecha: str
    titulo: str | None = None
    tipo: str
    duracion_seg: float | None = None
    resumen: str | None = None
    tiene_audio: bool = False
    n_segmentos: int = 0
    n_acciones: int = 0
    n_riesgos: int = 0
    n_updates: int = 0


class PaginaDeReuniones(BaseModel):
    total: int = Field(description="Reuniones que cumplen el filtro, no las devueltas")
    limite: int
    desplazamiento: int
    reuniones: list[Reunion]


class Salud(BaseModel):
    """Lo que hace falta para diagnosticar un despliegue a ciegas.

    En el servidor no hay navegador ni comodidad para depurar: este endpoint
    tiene que responder a "por que no funciona" sin abrir una shell.
    """

    version: str
    solo_lectura: bool
    base_de_datos: str
    base_accesible: bool
    detalle: str | None = None
    esquema: int | None = None
    esquema_esperado: int
    journal_mode: str | None = None
    reuniones: int | None = None
    personas: int | None = None
    acciones: int | None = None
    acciones_abiertas: int | None = None
    riesgos: int | None = None
    segmentos: int | None = None
    primera_reunion: str | None = None
    ultima_reunion: str | None = None
