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


# --------------------------------------------------------------------------
# I2: vista de reunion
# --------------------------------------------------------------------------


class Hablante(BaseModel):
    """Una etiqueta de la diarizacion y la persona a la que se atribuyo.

    `persona` es None cuando nadie le puso nombre: el pipeline no inventa
    gente para un `SPEAKER_01` suelto, y la interfaz tiene que poder decir
    "sin identificar" en vez de fingir que lo sabe.
    """

    etiqueta: str
    persona: str | None = None
    confianza: float | None = None
    metodo: str | None = None


class Intervencion(BaseModel):
    """Lo que conto una persona en la reunion (tabla `updates`)."""

    persona: str
    trabajo: str | None = None
    bloqueos: str | None = None
    proximos_pasos: str | None = None


class Accion(BaseModel):
    """Una accion del historico, en la unica forma en que la base la conoce.

    `estado` y `menciones` son siempre **los de hoy**: la base guarda un solo
    estado por accion y ningun historial de por donde paso (D10). Quien la
    muestre dentro de una reunion concreta tiene que decirlo, porque el `.md`
    de aquel dia puede decir otra cosa.
    """

    id: int
    descripcion: str
    persona: str | None = None
    estado: str
    menciones: int
    estancada: bool
    cerrada_en: str | None = None
    origen_uid: str
    origen_fecha: str
    ultima_uid: str
    ultima_fecha: str


class AccionDeReunion(Accion):
    """Una accion vista desde una reunion concreta.

    La interfaz avisa de que el estado puede ser posterior cuando la ultima
    mencion no es la reunion que se esta mirando.
    """

    comentario: str | None = Field(
        None,
        description=(
            "Lo que el modelo dijo del arrastre en esta reunion; sale de "
            "datos_json y puede faltar"
        ),
    )


class RiesgoDeReunion(BaseModel):
    descripcion: str
    area: str | None = None
    severidad: str | None = None


class SeccionExtra(BaseModel):
    """Las secciones propias del tipo: "Que fue bien", "Alcance comprometido"...

    Son lo unico de la vista que sale de `datos_json` y no de una tabla: el
    esquema de la base es comun a todos los tipos a proposito.
    """

    titulo: str
    puntos: list[str]


class ReunionDetalle(Reunion):
    """Todo lo de una reunion menos la transcripcion, que va aparte y paginada.

    Extiende `Reunion` en vez de sustituirla: `/api/reuniones/{uid}` devuelve
    un superconjunto de lo que devolvia en I0, asi que nada de lo que ya
    consumia el front se rompe.
    """

    transcript_path: str | None = None
    modelo_whisper: str | None = None
    modelo_llm: str | None = None
    creado_en: str | None = None
    hablantes: list[Hablante] = []
    intervenciones: list[Intervencion] = []
    acciones: list[AccionDeReunion] = []
    arrastres: list[AccionDeReunion] = []
    riesgos: list[RiesgoDeReunion] = []
    secciones: list[SeccionExtra] = []
    tiene_markdown: bool = Field(
        False, description="Si hay datos_json del que reconstruir el .md"
    )


class Segmento(BaseModel):
    """Una linea de la transcripcion.

    `inicio`/`fin` van en segundos y pueden ser None: si la reunion se proceso
    sin `.srt`, los segmentos entraron del `.txt` sin marcas de tiempo (D9).
    """

    idx: int
    inicio: float | None = None
    fin: float | None = None
    etiqueta: str | None = None
    persona: str | None = None
    texto: str


class PaginaDeSegmentos(BaseModel):
    total: int
    limite: int
    desplazamiento: int
    con_tiempos: bool = Field(
        description="Si los segmentos devueltos traen marcas de tiempo"
    )
    segmentos: list[Segmento]


# --------------------------------------------------------------------------
# I3: tablero de acciones
# --------------------------------------------------------------------------


class AccionTablero(Accion):
    """La misma accion, vista fuera de cualquier reunion.

    Añade lo que ahi no hacia falta: el titulo de las dos reuniones (una fecha
    suelta no dice a donde lleva el enlace cuando no se viene de ninguna
    reunion) y los dias sin tocar, que es la pregunta de esta vista.
    """

    origen_titulo: str | None = None
    ultima_titulo: str | None = None
    dias_sin_tocar: int = Field(
        description="Dias desde la ultima reunion que la menciono, hasta hoy"
    )


class Responsable(BaseModel):
    """Un nombre para el desplegable, con cuantas acciones tiene.

    No es `/api/personas` (fase I6, con alias y altas): aqui solo interesan
    los que tienen algo bajo el filtro actual.
    """

    persona: str = Field(
        description="El nombre, o el centinela de las acciones sin responsable"
    )
    n: int


class PaginaDeAcciones(BaseModel):
    """El tablero entero en una peticion: la pagina y con que contrastarla.

    `por_estado` y `responsables` se calculan ignorando su propio filtro, para
    que los controles sigan diciendo a donde lleva cambiarlos en vez de
    reflejar lo ya elegido.
    """

    total: int = Field(description="Acciones que cumplen el filtro, no las devueltas")
    limite: int
    desplazamiento: int
    orden: str
    umbral_estancamiento: int
    sin_responsable: str = Field(
        description="Valor de `persona` que filtra las acciones sin dueño"
    )
    por_estado: dict[str, int]
    responsables: list[Responsable]
    acciones: list[AccionTablero]


# --------------------------------------------------------------------------
# I4: busqueda
# --------------------------------------------------------------------------


class Coincidencia(BaseModel):
    """Un segmento que casa con la busqueda, con su reunion y su resaltado.

    Lleva `uid` e `idx` en vez de una URL montada: las direcciones del front
    viven en `js/enlaces.js` y la API no tiene por que saber cual es la pagina
    de una reunion.
    """

    uid: str
    fecha: str
    titulo: str | None = None
    tipo: str
    idx: int
    inicio: float | None = None
    fin: float | None = None
    etiqueta: str | None = None
    persona: str | None = None
    fragmento: str = Field(
        description=(
            "El texto del segmento, recortado si era largo, con los terminos "
            "encontrados entre los marcadores `marca_inicio`/`marca_fin`"
        )
    )


class ReunionConCoincidencias(BaseModel):
    """Cuantas veces aparece el termino en cada reunion.

    Se calcula **sin** el filtro `uid`, igual que `por_estado` en el tablero:
    acotar a una reunion no puede borrar la lista desde la que se acota.
    """

    uid: str
    fecha: str
    titulo: str | None = None
    tipo: str
    n: int


class PaginaDeBusqueda(BaseModel):
    """Resultados, con que contrastarlos y como se interpreto la consulta.

    `consulta_fts` se devuelve a proposito: lo que se teclea no es sintaxis de
    FTS5 y la API lo reescribe, asi que sin esto nadie podria explicarse por
    que `refact-` encontro lo que encontro.
    """

    q: str
    consulta_fts: str
    total: int = Field(description="Coincidencias que cumplen el filtro, no las devueltas")
    limite: int
    desplazamiento: int
    orden: str
    marca_inicio: str
    marca_fin: str
    reuniones: list[ReunionConCoincidencias]
    coincidencias: list[Coincidencia]
