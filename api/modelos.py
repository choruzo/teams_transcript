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


# --------------------------------------------------------------------------
# I1: metricas y timeline
# --------------------------------------------------------------------------


class Semana(BaseModel):
    """Una semana ISO del periodo. Solo aparecen las que tienen algo."""

    semana: str = Field(description="Semana ISO, `2026-W37`")
    reuniones: int
    minutos: int = Field(description="Suma de duraciones conocidas")
    sin_duracion: int = Field(
        description="Reuniones de la semana sin duracion en la base (D9)"
    )
    cierres: int = Field(description="Acciones con `cerrada_en` en la semana (D4)")


class CargaPersona(BaseModel):
    persona: str
    updates: int = Field(description="Intervenciones registradas en el periodo")
    acciones: int = Field(description="Acciones nacidas en el periodo")
    abiertas: int = Field(description="Acciones vivas hoy, de cualquier fecha")


class AreaRiesgo(BaseModel):
    area: str
    n: int


class Riesgos(BaseModel):
    """Riesgos **mencionados** en el periodo; nunca "abiertos" (D3)."""

    total: int
    por_severidad: dict[str, int]
    por_area: list[AreaRiesgo]


class Metricas(BaseModel):
    """Dos alcances en un mismo objeto, y el front tiene que respetarlos.

    `acciones`, `acciones_abiertas` y `estancadas` son el estado **de hoy** del
    historico entero; el resto es lo ocurrido **en el periodo**. Una accion no
    tiene fecha propia y recortarla al filtro daria un numero sin significado.
    """

    desde: str | None = None
    hasta: str | None = None
    umbral_estancamiento: int
    acciones: dict[str, int]
    acciones_abiertas: int
    estancadas: int
    reuniones: int
    reuniones_por_tipo: dict[str, int]
    minutos: int
    reuniones_sin_duracion: int
    semanas: list[Semana]
    personas: list[CargaPersona]
    riesgos: Riesgos


class Carril(BaseModel):
    """Una accion como tramo entre la reunion que la creo y la ultima que la cito.

    No hay marcas intermedias porque la base no las guarda (D10): solo el
    contador `menciones` y la ultima reunion.
    """

    id: int
    descripcion: str
    persona: str | None = None
    estado: str
    menciones: int
    estancada: bool
    origen_uid: str
    origen_fecha: str
    ultima_uid: str
    ultima_fecha: str


class Timeline(BaseModel):
    """Todo lo que el timeline necesita, en una sola peticion.

    Reuniones y carriles van juntos y no en dos llamadas: se dibujan sobre el
    mismo eje, y pintar uno sin el otro da un estado intermedio incoherente.
    """

    desde: str | None = None
    hasta: str | None = None
    reuniones: list[Reunion]
    carriles: list[Carril]
    total_reuniones: int = Field(description="Las que cumplen el filtro")
    truncado: bool = Field(
        description="Si el periodo tenia mas elementos de los devueltos"
    )
