"""
report_teams.py

Informes agregados del historico de reuniones (Fase 5 del plan del motor).

Uso:
    python report_teams.py --semanal
    python report_teams.py --mensual --salida informe.md
    python report_teams.py --desde 2026-08-01 --persona "Pablo Gil"
    python report_teams.py --semanal --sin-llm

El reparto de trabajo es deliberado y es lo que hace fiable al informe:

- **Los numeros salen de SQL** (`memoria.py`), son deterministas y se pueden
  comprobar uno a uno contra la base. Nada de lo que se cuenta aqui lo ha
  decidido un modelo.
- **El LLM solo escribe la narrativa** que los rodea: una lectura en prosa de
  esas mismas cifras, con prohibicion explicita de inventarse ninguna. Si el
  modelo no esta disponible, el informe sale igual y lo dice: perder la
  narrativa degrada la lectura, no el dato.

Salida en Markdown por stdout o a fichero (`--salida`). Con `--json` se vuelca
solo la parte determinista, para encadenarlo con otra cosa.
"""

import argparse
import json
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

import glosario as glosario_mod
import llm
import memoria

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


# Topes de cada lista. El informe es para leerlo de arriba abajo: una tabla de
# doscientas filas no la mira nadie, y ademas se le manda al modelo para que
# escriba la narrativa, que tampoco mejora con mas contexto.
MAXIMO_ABIERTAS = 25
MAXIMO_CERRADAS = 25
MAXIMO_SIN_MENCION = 15
MAXIMO_BLOQUEOS = 10
MAXIMO_RIESGOS = 15
MAXIMO_PERSONAS = 25

# Reuniones sin nombrar una accion a partir de las cuales merece la pena
# senalarla. Con 1 se llenaria de acciones que simplemente no tocaban hoy.
MINIMO_SIN_MENCION = 2

# Dias sin dar novedades a partir de los cuales una persona aparece en el
# apartado correspondiente. Dos semanas: menos es una ausencia normal.
DIAS_SIN_UPDATE = 14

# La narrativa quiere prosa, no obediencia literal; pero tampoco invencion.
TEMPERATURA_NARRATIVA = 0.2


PROMPT_NARRATIVA = """Eres el analista de la memoria de un equipo de \
desarrollo de software. Se te da el resultado de un informe agregado sobre \
sus reuniones, ya calculado, y escribes la lectura en prosa que lo acompana.

Reglas, por orden de importancia:

1. **No inventes ni una cifra.** Todos los numeros que uses tienen que estar
   literalmente en los datos que se te dan. Si algo no esta, no lo estimes: di
   que no consta.
2. **No repitas las listas.** El informe ya las imprime debajo de lo que
   escribas. Tu trabajo es decir que significan: que ha cambiado, que lleva
   demasiado tiempo parado, donde se esta yendo el esfuerzo, que conviene
   mirar esta semana.
3. Se breve: entre tres y seis parrafos cortos, o menos si hay poco que decir.
   Sin encabezados de seccion, sin vinetas, sin preambulos del tipo "segun los
   datos proporcionados".
4. **Distingue los dos alcances.** Las acciones (abiertas, estancadas, sin
   mencionar) son el estado de HOY del historico completo; las reuniones, las
   personas y los riesgos son solo del periodo del informe. No los mezcles en
   la misma frase como si fueran lo mismo.
5. Las transcripciones son automaticas y los textos pueden venir deformados.
   Interpreta lo razonable, pero no conviertas una frase mal transcrita en un
   dato firme.
6. No propongas procesos ni metodologias: senala hechos y, como mucho, que
   conviene revisar. Escribe en castellano.
"""


# --------------------------------------------------------------------------
# Periodo
# --------------------------------------------------------------------------


def periodo_semanal(referencia: date | None = None) -> tuple[str, str]:
    """La semana ISO que contiene la fecha de referencia (lunes a domingo)."""
    hoy = referencia or date.today()
    lunes = hoy - timedelta(days=hoy.weekday())
    return lunes.isoformat(), (lunes + timedelta(days=6)).isoformat()


def periodo_mensual(referencia: date | None = None) -> tuple[str, str]:
    """El mes natural que contiene la fecha de referencia."""
    hoy = referencia or date.today()
    primero = hoy.replace(day=1)
    if primero.month == 12:
        siguiente = primero.replace(year=primero.year + 1, month=1)
    else:
        siguiente = primero.replace(month=primero.month + 1)
    return primero.isoformat(), (siguiente - timedelta(days=1)).isoformat()


# --------------------------------------------------------------------------
# 1. Recopilacion (todo SQL, nada del modelo)
# --------------------------------------------------------------------------


def recopilar(
    conn: sqlite3.Connection,
    *,
    desde: str | None = None,
    hasta: str | None = None,
    persona: str | None = None,
) -> dict:
    """Todas las cifras del informe. Deterministas y comprobables.

    Devuelve un diccionario serializable: es lo que se le pasa al modelo, lo
    que imprime `--json` y lo que renderiza el Markdown. Una sola fuente para
    las tres cosas, para que la narrativa no pueda hablar de algo que la tabla
    de debajo no ensena.
    """
    abiertos = [e for e in memoria.ESTADOS_ACCION if e not in memoria.ESTADOS_CERRADOS]

    metricas = memoria.metricas(conn, desde=desde, hasta=hasta)

    abiertas = memoria.listar_acciones(
        conn,
        estados=abiertos,
        persona=persona,
        orden="antiguedad",
        limite=MAXIMO_ABIERTAS,
    )
    # Los recuentos se cuentan **con** el filtro de persona, no se toman de
    # `metricas()`: esa funcion no lo conoce, y un "1 estancada" al lado de una
    # lista donde no hay ninguna es peor que no dar el numero.
    total_abiertas = memoria.contar_acciones(conn, estados=abiertos, persona=persona)
    estancadas = memoria.contar_acciones(
        conn, estados=abiertos, persona=persona, estancadas=True
    )
    por_estado = memoria.acciones_por_estado(conn, persona=persona)

    cerradas = memoria.acciones_cerradas(
        conn, desde=desde, hasta=hasta, persona=persona, limite=MAXIMO_CERRADAS
    )

    sin_mencion = memoria.acciones_sin_mencion(
        conn, minimo=MINIMO_SIN_MENCION, limite=MAXIMO_SIN_MENCION
    )
    if persona:
        # `acciones_sin_mencion` mira el historico entero a proposito (una
        # accion no tiene fecha propia), asi que el filtro de responsable se
        # aplica aqui, con el mismo criterio de mayusculas que el tablero.
        sin_mencion = [
            a for a in sin_mencion
            if (a["persona"] or "").lower() == persona.lower()
        ]

    bloqueos = memoria.bloqueos_recurrentes(
        conn, desde=desde, hasta=hasta, persona=persona, limite=MAXIMO_BLOQUEOS
    )

    riesgos = memoria.riesgos_recientes(
        conn, desde=desde, hasta=hasta, limite=MAXIMO_RIESGOS
    )

    sin_actualizar = memoria.personas_sin_actualizar(
        conn, hasta=hasta, dias=DIAS_SIN_UPDATE
    )

    personas = metricas["personas"][:MAXIMO_PERSONAS]
    if persona:
        clave = persona.lower()
        personas = [p for p in personas if p["persona"].lower() == clave]
        sin_actualizar = [
            p for p in sin_actualizar if p["persona"].lower() == clave
        ]

    return {
        "desde": desde,
        "hasta": hasta,
        "persona": persona,
        "generado": date.today().isoformat(),
        "umbral_estancamiento": memoria.UMBRAL_ESTANCAMIENTO,
        "minimo_sin_mencion": MINIMO_SIN_MENCION,
        "dias_sin_update": DIAS_SIN_UPDATE,
        "actividad": {
            "reuniones": metricas["reuniones"],
            "reuniones_por_tipo": metricas["reuniones_por_tipo"],
            "minutos": metricas["minutos"],
            "reuniones_sin_duracion": metricas["reuniones_sin_duracion"],
            "semanas": metricas["semanas"],
        },
        "acciones": {
            "por_estado": por_estado,
            "abiertas_total": total_abiertas,
            "estancadas": estancadas,
            "abiertas": [dict(f) for f in abiertas],
            "cerradas": [dict(f) for f in cerradas],
            "sin_mencion": [dict(f) for f in sin_mencion],
        },
        "bloqueos_recurrentes": bloqueos,
        "riesgos": {
            "resumen": metricas["riesgos"],
            "mencionados": [dict(f) for f in riesgos],
        },
        "personas": personas,
        "personas_sin_actualizar": sin_actualizar,
    }


# --------------------------------------------------------------------------
# 2. Narrativa (lo unico que escribe el modelo)
# --------------------------------------------------------------------------


def _para_el_modelo(datos: dict) -> dict:
    """Los mismos datos, sin lo que al modelo no le sirve.

    Se le quitan los identificadores internos y los uids: no puede enlazar
    nada y solo gastan contexto. Las listas ya vienen recortadas de
    `recopilar`.
    """
    def limpiar(accion: dict) -> dict:
        return {
            clave: valor
            for clave, valor in accion.items()
            if clave not in ("id", "origen_uid", "ultima_uid")
        }

    copia = json.loads(json.dumps(datos, ensure_ascii=False))
    for clave in ("abiertas", "cerradas", "sin_mencion"):
        copia["acciones"][clave] = [limpiar(a) for a in copia["acciones"][clave]]
    for bloqueo in copia["bloqueos_recurrentes"]:
        bloqueo.pop("reuniones", None)
    copia["riesgos"]["mencionados"] = [
        {k: v for k, v in r.items() if k != "uid"}
        for r in copia["riesgos"]["mencionados"]
    ]
    return copia


def construir_mensajes(datos: dict, *, glosario: str | None = None) -> list[dict]:
    sistema = PROMPT_NARRATIVA
    if glosario:
        sistema += (
            "\n\nGlosario del proyecto. Uselo para entender la jerga interna y "
            "para reconocer terminos que la transcripcion automatica haya "
            "deformado:\n\n" + glosario
        )
    periodo = _texto_periodo(datos)
    cuerpo = (
        f"Informe {periodo}. Hoy es {datos['generado']}.\n\n"
        "Datos del informe (JSON):\n\n"
        + json.dumps(_para_el_modelo(datos), ensure_ascii=False, indent=1)
    )
    return [
        {"role": "system", "content": sistema},
        {"role": "user", "content": cuerpo},
    ]


def narrar(
    datos: dict,
    *,
    base_url: str | None = None,
    api_key: str | None = None,
    modelo: str | None = None,
    glosario: str | None = None,
    cliente=llm,
) -> tuple[str | None, str | None]:
    """Pide la lectura en prosa. Devuelve `(narrativa, aviso)`.

    **Nunca lanza**: igual que el interprete de `ask_teams.py`, la narrativa
    es un lujo. Si el modelo no responde, el informe sale con sus tablas y una
    nota diciendo por que falta el analisis, que es mas util que un traceback
    encima de unos numeros que si estaban bien.
    """
    mensajes = construir_mensajes(datos, glosario=glosario)
    try:
        texto = cliente.chat(
            llm.base_url(base_url),
            llm.api_key(api_key),
            llm.modelo(modelo),
            mensajes,
            temperature=TEMPERATURA_NARRATIVA,
        )
    except Exception as exc:  # noqa: BLE001 - la narrativa no bloquea el informe
        return None, f"No se pudo generar la narrativa: {exc}"
    texto = (texto or "").strip()
    if not texto:
        return None, "El modelo devolvio una narrativa vacia."
    return texto, None


# --------------------------------------------------------------------------
# 3. Markdown
# --------------------------------------------------------------------------


def _n(cantidad: int, singular: str, plural: str | None = None) -> str:
    """`1 reunion` / `3 reuniones`. Es un informe: se lee, no se parsea."""
    return f"{cantidad} {singular if cantidad == 1 else (plural or singular + 's')}"


def _texto_periodo(datos: dict) -> str:
    desde, hasta = datos["desde"], datos["hasta"]
    if desde and hasta:
        return f"del {desde} al {hasta}"
    if desde:
        return f"desde {desde}"
    if hasta:
        return f"hasta {hasta}"
    return "del historico completo"


def _fila_accion(accion: dict) -> str:
    quien = accion.get("persona") or "sin responsable"
    marcas = [accion["estado"], _n(accion["menciones"], "reunion", "reuniones")]
    if accion.get("estancada"):
        marcas.append("ESTANCADA")
    if accion.get("dias_sin_tocar") is not None:
        marcas.append(f"{_n(accion['dias_sin_tocar'], 'dia')} sin tocar")
    return (
        f"- **{quien}** — {accion['descripcion']} "
        f"({', '.join(marcas)}) · desde {accion['origen_fecha']}"
    )


def _seccion(lineas: list[str], titulo: str, cuerpo: list[str], vacio: str) -> None:
    lineas.append(f"## {titulo}")
    lineas.append("")
    lineas.extend(cuerpo or [vacio])
    lineas.append("")


def renderizar_markdown(
    datos: dict, narrativa: str | None = None, aviso: str | None = None
) -> str:
    """El informe entero. Las cifras primero; el analisis, si lo hay, arriba."""
    acciones = datos["acciones"]
    actividad = datos["actividad"]
    lineas: list[str] = []

    encabezado = f"Informe de reuniones ({_texto_periodo(datos)})"
    lineas.append(f"# {encabezado}")
    lineas.append("")
    cabecera = f"*Generado el {datos['generado']}"
    if datos["persona"]:
        # El filtro solo alcanza a lo que tiene responsable. Las reuniones y
        # los riesgos son del equipo, y sin decirlo se leerian como suyos.
        cabecera += (
            f" · filtrado por {datos['persona']}: acciones, bloqueos y"
            " personas; la actividad y los riesgos son los del equipo"
        )
    lineas.append(cabecera + "*")
    lineas.append("")

    if narrativa:
        _seccion(lineas, "Lectura", [narrativa], "")
    elif aviso:
        _seccion(lineas, "Lectura", [f"*(no disponible: {aviso})*"], "")

    # -- Actividad ---------------------------------------------------------
    cuerpo = []
    tipos = ", ".join(
        f"{n} {tipo}" for tipo, n in actividad["reuniones_por_tipo"].items() if n
    )
    cuerpo.append(
        f"- **{_n(actividad['reuniones'], 'reunion', 'reuniones')}**"
        + (f" ({tipos})" if tipos else "")
    )
    minutos = f"- **{_n(actividad['minutos'], 'minuto')}** de reunion"
    if actividad["reuniones_sin_duracion"]:
        # D9: sin `.srt` no hay duracion. Sumar solo lo que se sabe y callarse
        # el resto convertiria un dato parcial en uno falso.
        minutos += (
            f" (sin contar {actividad['reuniones_sin_duracion']} sin duracion "
            "registrada)"
        )
    cuerpo.append(minutos)
    for semana in actividad["semanas"]:
        cuerpo.append(
            f"  - {semana['semana']}: "
            f"{_n(semana['reuniones'], 'reunion', 'reuniones')}, "
            f"{semana['minutos']} min, "
            f"{_n(semana['cierres'], 'accion cerrada', 'acciones cerradas')}"
        )
    _seccion(lineas, "Actividad", cuerpo, "(sin reuniones en el periodo)")

    # -- Acciones abiertas -------------------------------------------------
    cuerpo = [
        f"{_n(acciones['abiertas_total'], 'abierta')} en total, "
        f"{_n(acciones['estancadas'], 'estancada')} "
        f"(>= {datos['umbral_estancamiento']} menciones sin cerrarse). "
        "Es el estado de **hoy** del historico completo, no del periodo.",
        "",
    ]
    cuerpo.extend(_fila_accion(a) for a in acciones["abiertas"])
    if not acciones["abiertas"]:
        # El recuento en cero ya esta arriba, pero una seccion que se corta sin
        # lista se lee como si faltara algo.
        cuerpo.append("(ninguna)")
    if acciones["abiertas_total"] > len(acciones["abiertas"]):
        cuerpo.append(
            f"- *(y {acciones['abiertas_total'] - len(acciones['abiertas'])} mas)*"
        )
    _seccion(lineas, "Acciones abiertas, de la mas antigua a la mas reciente",
             cuerpo, "(ninguna)")

    # -- Sin mencion -------------------------------------------------------
    cuerpo = [
        f"- **{a.get('persona') or 'sin responsable'}** — {a['descripcion']} "
        f"({_n(a['reuniones_sin_mencion'], 'reunion', 'reuniones')} "
        f"sin nombrarla, "
        f"ultima vez el {a['ultima_fecha']})"
        for a in acciones["sin_mencion"]
    ]
    _seccion(
        lineas,
        f"Abiertas sin mencionar en {datos['minimo_sin_mencion']} reuniones o mas",
        cuerpo,
        "(ninguna: todo lo abierto se ha tocado recientemente)",
    )

    # -- Cerradas ----------------------------------------------------------
    cuerpo = [
        f"- **{a.get('persona') or 'sin responsable'}** — {a['descripcion']} "
        f"({a['estado']}, cerrada el {a['cerrada_en']})"
        for a in acciones["cerradas"]
    ]
    if cuerpo:
        # D4: `cerrada_en` es la fecha en que se proceso la reunion que la
        # cerro, no la del cierre real. Decirlo al lado del numero es la unica
        # forma honesta de darlo.
        cuerpo.append("")
        cuerpo.append(
            "*La fecha de cierre es la del procesado de la reunion que la "
            "cerro, no la del cierre real.*"
        )
    _seccion(lineas, "Cerradas en el periodo", cuerpo, "(ninguna)")

    # -- Bloqueos recurrentes ----------------------------------------------
    cuerpo = []
    for bloqueo in datos["bloqueos_recurrentes"]:
        cuerpo.append(
            f"- {bloqueo['texto']} — "
            f"**{_n(bloqueo['n_reuniones'], 'reunion', 'reuniones')}** "
            f"({', '.join(bloqueo['personas'])}), "
            f"del {bloqueo['primera_fecha']} al {bloqueo['ultima_fecha']}"
        )
    if cuerpo:
        cuerpo.append("")
        cuerpo.append(
            "*Se agrupan bloqueos con el mismo texto (sin acentos ni "
            "puntuacion). Uno contado dos veces con otras palabras no se "
            "detecta.*"
        )
    _seccion(lineas, "Bloqueos recurrentes", cuerpo, "(ninguno repetido)")

    # -- Reparto por persona -----------------------------------------------
    cuerpo = [
        f"- **{p['persona']}**: {_n(p['updates'], 'intervencion', 'intervenciones')}, "
        f"{_n(p['acciones'], 'accion nacida', 'acciones nacidas')} en el periodo, "
        f"{_n(p['abiertas'], 'abierta')} hoy"
        for p in datos["personas"]
    ]
    _seccion(lineas, "Reparto por persona", cuerpo, "(sin datos en el periodo)")

    # -- Sin actualizar ----------------------------------------------------
    cuerpo = []
    for p in datos["personas_sin_actualizar"]:
        cuando = (
            f"ultima novedad el {p['ultimo_update']} "
            f"({_n(p['dias_sin_update'], 'dia')})"
            if p["ultimo_update"]
            else "sin ninguna novedad registrada"
        )
        extra = (
            f", {_n(p['abiertas'], 'accion abierta', 'acciones abiertas')}"
            if p["abiertas"] else ""
        )
        cuerpo.append(f"- **{p['persona']}**: {cuando}{extra}")
    if cuerpo:
        # D7: no hay roster del equipo. Solo se ve a quien la base ya conoce.
        cuerpo.append("")
        cuerpo.append(
            "*Solo aparecen personas que ya constan en el historico: sin lista "
            "de equipo no se puede saber quien falta y no ha hablado nunca.*"
        )
    _seccion(
        lineas,
        f"Sin novedades desde hace {datos['dias_sin_update']} dias o mas",
        cuerpo,
        "(nadie)",
    )

    # -- Riesgos -----------------------------------------------------------
    cuerpo = []
    resumen = datos["riesgos"]["resumen"]
    if resumen["total"]:
        severidades = ", ".join(
            f"{n} {sev}" for sev, n in resumen["por_severidad"].items()
        )
        cuerpo.append(f"{_n(resumen['total'], 'mencion', 'menciones')} ({severidades}).")
        cuerpo.append("")
    for riesgo in datos["riesgos"]["mencionados"]:
        marcas = [m for m in (riesgo["area"], riesgo["severidad"]) if m]
        sufijo = f" ({', '.join(marcas)})" if marcas else ""
        cuerpo.append(f"- {riesgo['descripcion']}{sufijo} · {riesgo['fecha']}")
    if cuerpo:
        # D3: `risks` no tiene estado ni continuidad entre reuniones.
        cuerpo.append("")
        cuerpo.append(
            "*Son riesgos **mencionados** en el periodo. No tienen estado ni "
            "seguimiento: uno que nadie volvio a nombrar desaparece de aqui.*"
        )
    _seccion(lineas, "Riesgos mencionados", cuerpo, "(ninguno)")

    return "\n".join(lineas).rstrip() + "\n"


def generar(
    conn: sqlite3.Connection,
    *,
    desde: str | None = None,
    hasta: str | None = None,
    persona: str | None = None,
    con_llm: bool = True,
    glosario: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    modelo: str | None = None,
    cliente=llm,
) -> tuple[str, dict, str | None]:
    """El informe de punta a punta: `(markdown, datos, aviso)`."""
    datos = recopilar(conn, desde=desde, hasta=hasta, persona=persona)
    narrativa, aviso = (None, None)
    if con_llm:
        narrativa, aviso = narrar(
            datos,
            base_url=base_url,
            api_key=api_key,
            modelo=modelo,
            glosario=glosario,
            cliente=cliente,
        )
    return renderizar_markdown(datos, narrativa, aviso), datos, aviso


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _resolver_periodo(args) -> tuple[str | None, str | None]:
    """`--semanal`/`--mensual` son atajos; `--desde`/`--hasta` mandan sobre ellos."""
    referencia = date.fromisoformat(args.referencia) if args.referencia else None
    desde, hasta = None, None
    if args.semanal:
        desde, hasta = periodo_semanal(referencia)
    elif args.mensual:
        desde, hasta = periodo_mensual(referencia)
    return (args.desde or desde), (args.hasta or hasta)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Informe agregado del historico de reuniones."
    )
    periodo = parser.add_mutually_exclusive_group()
    periodo.add_argument(
        "--semanal", action="store_true",
        help="La semana ISO en curso (lunes a domingo)",
    )
    periodo.add_argument(
        "--mensual", action="store_true", help="El mes natural en curso"
    )
    parser.add_argument(
        "--referencia", default=None, metavar="AAAA-MM-DD",
        help="Calcular --semanal/--mensual sobre otro dia y no sobre hoy",
    )
    parser.add_argument("--desde", default=None, metavar="AAAA-MM-DD")
    parser.add_argument("--hasta", default=None, metavar="AAAA-MM-DD")
    parser.add_argument("--persona", default=None, help="Filtra por responsable")
    parser.add_argument("--db", default=None, help="Ruta de meetings.db (o TEAMS_DB)")
    parser.add_argument(
        "-o", "--salida", default=None,
        help="Fichero .md de salida (por defecto, la consola)",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Vuelca solo las cifras (sin narrativa ni Markdown)",
    )
    parser.add_argument(
        "--sin-llm", action="store_true",
        help="No pedir la narrativa: solo los numeros",
    )
    parser.add_argument("--model", default=None, help="Modelo (o TEAMS_LLM_MODELO)")
    parser.add_argument("--base-url", default=None, help="URL del LLM (o LITELLM_BASE_URL)")
    parser.add_argument("--api-key", default=None, help="API key (o LITELLM_API_KEY)")
    parser.add_argument("--glosario", default=None, help="Ruta del glosario")
    parser.add_argument("--sin-glosario", action="store_true")
    args = parser.parse_args()

    for etiqueta in ("desde", "hasta", "referencia"):
        valor = getattr(args, etiqueta)
        if valor:
            try:
                date.fromisoformat(valor)
            except ValueError:
                parser.error(f"--{etiqueta} debe ser una fecha AAAA-MM-DD: {valor!r}")

    desde, hasta = _resolver_periodo(args)

    ruta_db = memoria.ruta_bd(args.db)
    if not ruta_db.exists():
        print(f"No se encuentra la base de datos en {ruta_db}.", file=sys.stderr)
        sys.exit(1)

    glosario = None
    if not args.sin_glosario and not args.sin_llm and not args.json:
        contenido = glosario_mod.cargar(args.glosario)
        if contenido:
            glosario = glosario_mod.texto_completo(contenido)

    conn = memoria.conectar(ruta_db, solo_lectura=True)
    try:
        if args.json:
            datos = recopilar(conn, desde=desde, hasta=hasta, persona=args.persona)
            print(json.dumps(datos, ensure_ascii=False, indent=2))
            return

        markdown, _datos, aviso = generar(
            conn,
            desde=desde,
            hasta=hasta,
            persona=args.persona,
            con_llm=not args.sin_llm,
            glosario=glosario,
            base_url=args.base_url,
            api_key=args.api_key,
            modelo=args.model,
        )
    finally:
        conn.close()

    if aviso:
        print(f"Aviso: {aviso}", file=sys.stderr)

    if args.salida:
        destino = Path(args.salida)
        destino.parent.mkdir(parents=True, exist_ok=True)
        destino.write_text(markdown, encoding="utf-8")
        print(f"Informe escrito en {destino}", file=sys.stderr)
    else:
        print(markdown)


if __name__ == "__main__":
    main()
