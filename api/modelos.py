"""Contrato de la API.

Los modelos son el contrato con el front: si un campo cambia de nombre aqui,
hay que cambiarlo en `web/js/`. Por eso se declaran explicitamente en vez de
devolver la fila de SQLite tal cual, que ataria la URL publica al esquema
interno de la base.
"""

from typing import Literal

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
    escritura: bool = Field(
        description=(
            "Si las rutas de correccion (I6a) aceptan escrituras; el panel lo "
            "consulta para no tener que intentar un PATCH"
        )
    )
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


class MarcaDeCarril(BaseModel):
    reunion_uid: str
    fecha: str


class Carril(BaseModel):
    """Una accion como tramo entre su primera y su ultima mencion.

    Desde el esquema 3 cada mencion es una marca (`marcas`), incluidas las de
    las acciones absorbidas; por eso el tramo empieza en `primera_fecha`, que
    puede ser anterior a `origen_fecha` tras una fusion.
    """

    id: int
    uid: str
    descripcion: str
    persona: str | None = None
    estado: str
    menciones: int
    estancada: bool
    origen_uid: str
    origen_fecha: str
    ultima_uid: str
    ultima_fecha: str
    primera_fecha: str
    revisar: bool = False
    descartada: bool = False
    absorbidas: int = 0
    marcas: list[MarcaDeCarril] = Field(default_factory=list)
    depende_de: list[str] = Field(default_factory=list, description="uid")
    bloquea_a: list[str] = Field(default_factory=list, description="uid")


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
    uid: str = Field(description="Identidad estable de la accion (I6a)")
    descripcion: str
    persona: str | None = None
    estado: str
    menciones: int
    estancada: bool
    revisar: bool = False
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
# I6a: correccion de acciones
# --------------------------------------------------------------------------


class AccionRelacionada(BaseModel):
    uid: str
    descripcion: str
    estado: str
    creado_en: str | None = None


class AccionAbsorbida(BaseModel):
    uid: str
    descripcion: str
    estado: str
    version: int


class MencionDeAccion(BaseModel):
    reunion_uid: str
    fecha: str
    titulo: str | None = None
    estado: str
    comentario: str | None = None
    accion_uid: str = Field(
        description="La accion que recibio la mencion: la propia o una absorbida"
    )


class Correccion(BaseModel):
    id: int
    campo: str
    valor_anterior: str | None = None
    valor_nuevo: str | None = None
    origen: str
    creado_en: str
    deshecha_en: str | None = None
    deshacible: bool = Field(
        description="Si es la ultima vigente de su campo (las demas no se deshacen)"
    )


class FichaAccion(BaseModel):
    """`memoria.detalle_accion`: tambien de una descartada o absorbida.

    `version` es la de la concurrencia optimista: cada escritura la devuelve
    en la cabecera `If-Match` y recibe un 409 si ha cambiado. Va tambien como
    `ETag` de la respuesta.
    """

    uid: str
    version: int
    descripcion: str
    descripcion_llm: str | None = None
    persona: str | None = None
    estado: str
    menciones: int
    estancada: bool
    revisar: bool
    cerrada_en: str | None = None
    descartada_en: str | None = None
    motivo_descarte: str | None = None
    absorbida_por: str | None = None
    absorbida_por_descripcion: str | None = None
    corregida: list[str] = Field(
        description="Campos con una correccion humana vigente"
    )
    origen_uid: str
    origen_fecha: str
    origen_titulo: str | None = None
    ultima_uid: str
    ultima_fecha: str
    ultima_titulo: str | None = None
    absorbidas: list[AccionAbsorbida]
    menciones_detalle: list[MencionDeAccion]
    depende_de: list[AccionRelacionada]
    bloquea_a: list[AccionRelacionada]
    correcciones: list[Correccion]
    avisos: list[str] = Field(
        default_factory=list,
        description="Lo que se ha permitido pero conviene saber (cerrar con dependencias abiertas)",
    )


class CambiosDeAccion(BaseModel):
    """Cuerpo de `PATCH /api/acciones/{uid}`: solo cambia lo que se envia.

    `persona` a `null` o `""` la deja sin responsable, que es distinto de no
    enviarla.
    """

    descripcion: str | None = Field(None, max_length=2000)
    persona: str | None = Field(None, max_length=200)
    estado: str | None = None


class Descarte(BaseModel):
    motivo: str | None = Field(None, max_length=500)


class Fusion(BaseModel):
    duplicada_uid: str


class Persona(BaseModel):
    nombre: str
    alias: list[str]
    activo: bool
    acciones: int = Field(description="Acciones vigentes de las que es responsable")


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


# --------------------------------------------------------------------------
# I5: chat
# --------------------------------------------------------------------------


class TurnoDeChat(BaseModel):
    """Un turno previo de la conversacion.

    El historial lo guarda **el cliente**, no la base: el chat es de solo
    lectura y guardarlo aqui romperia `TEAMS_API_SOLO_LECTURA`. Solo se
    aceptan los dos papeles de una conversacion; un `system` colado por aqui
    seria una inyeccion de instrucciones desde el navegador.
    """

    role: Literal["user", "assistant"]
    content: str = Field(max_length=8000)


class Pregunta(BaseModel):
    """El cuerpo de `POST /api/chat`."""

    pregunta: str = Field(min_length=1, max_length=2000)
    historial: list[TurnoDeChat] = Field(default_factory=list, max_length=20)
    desde: str | None = None
    hasta: str | None = None
    persona: str | None = None
    tipo: str | None = None


class EstadoDelChat(BaseModel):
    """Que puede hacer el chat ahora mismo, y que no.

    Va aparte de `/api/salud` a proposito: el pie de las cinco paginas no debe
    hacer ping a LiteLLM en cada carga. Aqui si, porque la pagina del chat
    necesita decir de antemano si va a responder y con que.
    """

    disponible: bool = Field(description="Si `ask_teams` se pudo importar")
    motivo: str | None = Field(
        default=None, description="Por que no esta disponible, si no lo esta"
    )
    modelo: str | None = None
    busqueda: str = Field(
        description="'hibrida' si hay indice semantico, 'literal' si solo FTS5"
    )
    indice: dict | None = Field(
        default=None, description="Fragmentos, reuniones y modelo del indice"
    )
