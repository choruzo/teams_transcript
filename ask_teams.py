"""
ask_teams.py

Preguntas en lenguaje natural sobre el historico de reuniones (Fase 4 del
plan del motor). Es el mismo motor que consume el chat de la web: la API
**importa** este modulo, no reimplementa nada.

Uso:
    python ask_teams.py "que dijo Francisco sobre OLS este mes?"
    python ask_teams.py "que lleva mas tiempo bloqueado?" --desde 2026-08-01
    python ask_teams.py "resumen de la semana" --json

Tres pasos:

 1. **Interpretacion**: una llamada barata al LLM convierte la pregunta en
    filtros (persona, fechas, tipo, terminos) y decide si es *puntual* -- que
    se dijo -- o *agregada* -- que hay abierto --. Si falla, no bloquea: se
    usa la pregunta cruda.
 2. **Recuperacion**: puntual va a `rag.buscar` (FTS5 + vectorial); agregada
    va a SQL de `memoria.py`, que es determinista y no se puede alucinar.
 3. **Respuesta**: el LLM escribe citando `[n]` en cada afirmacion, con
    instruccion explicita de decir "no consta" antes que inventarse nada.

La respuesta sale por trozos (`responder_en_streaming`) porque la web la
pinta segun se escribe. La CLI usa el mismo generador.
"""

import argparse
import json
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import date

import glosario as glosario_mod
import llm
import memoria
import rag

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


# Cuantas fuentes se le ensenian al modelo. Mas contexto no es mejor
# respuesta: pasado cierto punto el modelo local se pierde y empieza a citar
# lo que no toca.
MAXIMO_FUENTES = 12
MAXIMO_ACCIONES = 25
MAXIMO_UPDATES = 20
MAXIMO_RIESGOS = 15

# El interprete quiere obediencia, no creatividad; la respuesta, un poco de
# margen para redactar.
TEMPERATURA_INTERPRETE = 0.0
TEMPERATURA_RESPUESTA = 0.2

INTENCIONES = ("puntual", "agregada", "mixta")


PROMPT_INTERPRETE = """Eres el interprete de un buscador sobre el historico de \
reuniones de un equipo de desarrollo. Traduces la pregunta del usuario a filtros.

Responde UNICAMENTE con un objeto JSON valido, sin bloque de codigo y sin \
ninguna explicacion, con estas claves:

{
  "intencion": "puntual" | "agregada" | "mixta",
  "persona": "nombre o null",
  "desde": "AAAA-MM-DD o null",
  "hasta": "AAAA-MM-DD o null",
  "tipo": "daily" | "workshop" | "retro" | "planning" | null,
  "terminos": ["palabras clave para busqueda literal"],
  "consulta_semantica": "la pregunta reescrita como frase afirmativa"
}

Criterios:
- "puntual" es lo que se **dijo** en alguna reunion ("que conto X sobre Y",
  "cuando se hablo de Z"). "agregada" es el **estado** del trabajo ("que
  sigue abierto", "que lleva mas tiempo bloqueado", "en que se ha ido el
  mes"). "mixta" si hacen falta las dos cosas.
- Las fechas relativas se resuelven contra HOY, que se te da mas abajo. Si la
  pregunta no acota tiempo, deja las dos fechas a null: el historico entero.
- En "terminos", incluye **variantes** de cada palabra importante: la busqueda
  literal no reduce a la raiz, asi que "bloqueado" no encuentra "bloquear".
  Pon las dos formas.
- No inventes un nombre de persona que no aparezca en la pregunta.
"""


PROMPT_RESPUESTA = """Eres el asistente de la memoria de un equipo de \
desarrollo de software. Respondes preguntas sobre lo que ha pasado en sus \
reuniones usando UNICAMENTE la informacion que se te da como fuentes.

Reglas, por orden de importancia:

1. **Cita siempre.** Cada afirmacion lleva al final la fuente de la que sale,
   entre corchetes: [1], [3]. Si una frase se apoya en dos fuentes, [1][2].
2. **Si no consta, dilo.** Cuando las fuentes no respondan a la pregunta,
   responde exactamente que no consta en el historico, y di que es lo mas
   parecido que si aparece. Nunca completes con lo que sabes del mundo: aqui
   una respuesta inventada es peor que ninguna.
3. **No cites lo que no te han dado.** No existen mas fuentes que las
   numeradas. No te inventes numeros.
4. Responde en castellano, directo y breve. Sin preambulos del tipo "segun
   las fuentes proporcionadas": ve al grano.
5. Las transcripciones son automaticas y pueden tener errores de
   reconocimiento. Si una frase esta claramente mal transcrita, interpreta lo
   razonable, pero no la conviertas en un dato firme.
6. Los estados de las acciones son los de **hoy**, no los del dia de la
   reunion en que se hablo de ellas.
"""


@dataclass
class Config:
    """Todo lo configurable, junto, para que la API no repita los defaults."""

    base_url: str = ""
    # Los embeddings pueden vivir en otro sitio que el chat: con dos
    # `llama-server` sueltos, cada modelo tiene su puerto.
    base_url_embeddings: str = ""
    api_key: str | None = None
    modelo: str = ""
    modelo_embeddings: str = ""
    prefijo_consulta: str | None = None
    usar_indice: bool = True

    def __post_init__(self):
        self.base_url = llm.base_url(self.base_url or None)
        self.base_url_embeddings = llm.base_url_embeddings(
            self.base_url_embeddings or None
        )
        self.api_key = llm.api_key(self.api_key)
        self.modelo = llm.modelo(self.modelo or None)
        self.modelo_embeddings = llm.modelo_embeddings(self.modelo_embeddings or None)
        if self.prefijo_consulta is None:
            self.prefijo_consulta = llm.prefijo_consulta()


@dataclass
class Fuente:
    """Un dato citable. `uid` + `idx` es lo que el front convierte en enlace."""

    n: int
    clase: str  # transcripcion | accion | update | riesgo
    texto: str
    uid: str | None = None
    fecha: str | None = None
    titulo: str | None = None
    tipo: str | None = None
    idx: int | None = None
    persona: str | None = None
    origen: list[str] = field(default_factory=list)

    def a_dict(self) -> dict:
        return {
            "n": self.n,
            "clase": self.clase,
            "texto": self.texto,
            "uid": self.uid,
            "fecha": self.fecha,
            "titulo": self.titulo,
            "tipo": self.tipo,
            "idx": self.idx,
            "persona": self.persona,
            "origen": self.origen,
        }


# --------------------------------------------------------------------------
# 1. Interpretacion
# --------------------------------------------------------------------------


def plan_por_defecto(pregunta: str) -> dict:
    """Lo que se usa cuando el interprete no esta o no sirve.

    Buscar la pregunta tal cual funciona sorprendentemente bien: las palabras
    que la persona ha escrito son, casi siempre, las que estan en la
    transcripcion. Perder el interprete degrada la calidad, no el servicio.
    """
    return {
        "intencion": "mixta",
        "persona": None,
        "desde": None,
        "hasta": None,
        "tipo": None,
        "terminos": [],
        "consulta_semantica": pregunta,
        "interpretada": False,
    }


def _normalizar_plan(datos: dict, pregunta: str) -> dict:
    """Nada sin validar entra en una consulta. Mismo criterio que `normalizar`."""
    plan = plan_por_defecto(pregunta)
    plan["interpretada"] = True

    intencion = datos.get("intencion")
    if isinstance(intencion, str) and intencion in INTENCIONES:
        plan["intencion"] = intencion

    for clave in ("persona", "desde", "hasta", "tipo"):
        valor = datos.get(clave)
        if isinstance(valor, str) and valor.strip():
            plan[clave] = valor.strip()

    if plan["tipo"] not in (None, *memoria.TIPOS_REUNION):
        plan["tipo"] = None
    for clave in ("desde", "hasta"):
        if plan[clave] and not _es_fecha(plan[clave]):
            plan[clave] = None

    terminos = datos.get("terminos")
    if isinstance(terminos, list):
        plan["terminos"] = [t.strip() for t in terminos if isinstance(t, str) and t.strip()]

    semantica = datos.get("consulta_semantica")
    if isinstance(semantica, str) and semantica.strip():
        plan["consulta_semantica"] = semantica.strip()

    return plan


def _es_fecha(valor: str) -> bool:
    try:
        date.fromisoformat(valor)
    except ValueError:
        return False
    return True


def interpretar(pregunta: str, *, config: Config, hoy: str | None = None) -> dict:
    """Traduce la pregunta a filtros. **Nunca lanza**: degrada al plan basico."""
    import summarize_teams  # import perezoso: solo por `extraer_json`

    hoy = hoy or date.today().isoformat()
    mensajes = [
        {"role": "system", "content": PROMPT_INTERPRETE + f"\nHOY es {hoy}."},
        {"role": "user", "content": pregunta},
    ]
    try:
        crudo = llm.chat(
            config.base_url,
            config.api_key,
            config.modelo,
            mensajes,
            temperature=TEMPERATURA_INTERPRETE,
            # Aqui solo se quiere un JSON de filtros: la cadena de pensamiento
            # de un modelo de razonamiento no aporta nada y cuesta segundos.
            sin_razonamiento=True,
        )
        return _normalizar_plan(summarize_teams.extraer_json(crudo), pregunta)
    except Exception as exc:  # noqa: BLE001 - el interprete es un lujo
        print(f"Aviso: no se pudo interpretar la pregunta ({exc}).", file=sys.stderr)
        return plan_por_defecto(pregunta)


# --------------------------------------------------------------------------
# 2. Recuperacion
# --------------------------------------------------------------------------


def _consulta_literal(plan: dict, pregunta: str) -> str:
    """Los terminos del interprete, o la pregunta si no dio ninguno."""
    return " ".join(plan["terminos"]) if plan["terminos"] else pregunta


def _filtros(plan: dict) -> dict:
    return {
        "persona": plan["persona"],
        "desde": plan["desde"],
        "hasta": plan["hasta"],
        "tipo": plan["tipo"],
    }


def recuperar(
    conn_hist: sqlite3.Connection,
    conn_idx: sqlite3.Connection | None,
    plan: dict,
    pregunta: str,
    *,
    config: Config,
) -> tuple[list[Fuente], str, str | None]:
    """Reune las fuentes. Devuelve `(fuentes, modo, aviso)`."""
    fuentes: list[Fuente] = []
    modo = "agregada"
    aviso = None
    contador = 0

    if plan["intencion"] in ("puntual", "mixta"):
        embebedor = None
        if config.usar_indice and conn_idx is not None:
            embebedor = rag.embebedor_litellm(
                config.base_url_embeddings,
                config.api_key,
                config.modelo_embeddings,
                config.prefijo_consulta,
            )
        resultado = rag.buscar(
            conn_hist,
            conn_idx,
            texto=_consulta_literal(plan, pregunta),
            semantica=plan["consulta_semantica"],
            embebedor=embebedor,
            filtros=_filtros(plan),
            limite=MAXIMO_FUENTES,
        )
        modo, aviso = resultado["modo"], resultado["aviso"]
        for fragmento in resultado["fragmentos"]:
            contador += 1
            fuentes.append(
                Fuente(
                    n=contador,
                    clase="transcripcion",
                    texto=fragmento["texto"],
                    uid=fragmento["uid"],
                    fecha=fragmento["fecha"],
                    titulo=fragmento.get("titulo"),
                    tipo=fragmento.get("tipo"),
                    idx=fragmento["idx_inicio"],
                    persona=fragmento.get("personas"),
                    origen=list(fragmento.get("origen") or []),
                )
            )

    if plan["intencion"] in ("agregada", "mixta"):
        contador = _fuentes_agregadas(conn_hist, plan, pregunta, fuentes, contador)
        # El buscador no encontro transcripcion pero el SQL si trajo acciones,
        # updates o riesgos: decir "sin coincidencias" con cinco fuentes en
        # pantalla seria mentir sobre lo que se esta ensenando.
        if modo == "vacia" and fuentes:
            modo = "agregada"

    return fuentes, modo, aviso


def _fuentes_agregadas(conn, plan, pregunta, fuentes: list[Fuente], contador: int) -> int:
    """Acciones, updates y riesgos: SQL determinista, nada de FTS5.

    Aqui no hay recuperacion que valga: la pregunta "que sigue abierto" tiene
    una respuesta exacta en la base, y pasarla por un buscador de texto solo
    puede empeorarla.
    """
    acciones = memoria.listar_acciones(
        conn,
        estados=[e for e in memoria.ESTADOS_ACCION if e not in memoria.ESTADOS_CERRADOS],
        persona=plan["persona"],
        texto=_texto_de_accion(plan, pregunta),
        desde=plan["desde"],
        hasta=plan["hasta"],
        orden="prioridad",
        limite=MAXIMO_ACCIONES,
    )
    for accion in acciones:
        contador += 1
        estancada = " ESTANCADA" if accion["estancada"] else ""
        fuentes.append(
            Fuente(
                n=contador,
                clase="accion",
                texto=(
                    f"Accion ({accion['estado']}{estancada}, "
                    f"{accion['menciones']} menciones, "
                    f"{accion['dias_sin_tocar']} dias sin tocar): "
                    f"{accion['descripcion']}"
                ),
                uid=accion["origen_uid"],
                fecha=accion["origen_fecha"],
                titulo=accion["origen_titulo"],
                persona=accion["persona"],
            )
        )

    # Un mismo bloqueo o un mismo riesgo se repiten literalmente en varias
    # dailys seguidas. Sin deduplicar, doce copias de "falta el certificado"
    # desplazan del contexto a las fuentes que si aportan algo distinto. Se
    # conserva la mas reciente, que es la que las consultas devuelven primero.
    vistos: set[str] = set()

    for update in memoria.updates_recientes(
        conn, persona=plan["persona"], desde=plan["desde"], hasta=plan["hasta"],
        limite=MAXIMO_UPDATES,
    ):
        huella = memoria.sin_acentos(
            f"{update['persona']}|{update['trabajo']}|{update['bloqueos']}"
        )
        if huella in vistos:
            continue
        vistos.add(huella)
        contador += 1
        partes = [f"{update['persona']}"]
        if update["trabajo"]:
            partes.append(f"trabajo: {update['trabajo']}")
        if update["bloqueos"]:
            partes.append(f"bloqueos: {update['bloqueos']}")
        if update["proximos_pasos"]:
            partes.append(f"proximos pasos: {update['proximos_pasos']}")
        fuentes.append(
            Fuente(
                n=contador,
                clase="update",
                texto=" — ".join(partes),
                uid=update["uid"],
                fecha=update["fecha"],
                titulo=update["titulo"],
                tipo=update["tipo"],
                persona=update["persona"],
            )
        )

    for riesgo in memoria.riesgos_recientes(
        conn, desde=plan["desde"], hasta=plan["hasta"], limite=MAXIMO_RIESGOS
    ):
        huella = memoria.sin_acentos(riesgo["descripcion"])
        if huella in vistos:
            continue
        vistos.add(huella)
        contador += 1
        fuentes.append(
            Fuente(
                n=contador,
                clase="riesgo",
                texto=(
                    f"Riesgo mencionado ({riesgo['severidad'] or 'sin severidad'}, "
                    f"{riesgo['area'] or 'sin area'}): {riesgo['descripcion']}"
                ),
                uid=riesgo["uid"],
                fecha=riesgo["fecha"],
                titulo=riesgo["titulo"],
                tipo=riesgo["tipo"],
            )
        )
    return contador


def _texto_de_accion(plan: dict, pregunta: str) -> str | None:
    """Un unico termino como filtro `LIKE` de la descripcion.

    Las descripciones de accion no estan en el indice FTS5 (que cubre las
    transcripciones), asi que este es el unico modo de encontrarlas por texto.
    Con varios terminos no se filtra: mejor devolver el tablero entero
    ordenado por prioridad que una lista vacia por una palabra de mas.
    """
    if len(plan["terminos"]) == 1:
        return plan["terminos"][0]
    return None


# --------------------------------------------------------------------------
# 3. Respuesta
# --------------------------------------------------------------------------


def _bloque_fuentes(fuentes: list[Fuente]) -> str:
    lineas = []
    for fuente in fuentes:
        cabecera = f"[{fuente.n}]"
        if fuente.fecha:
            cabecera += f" {fuente.titulo or fuente.tipo or 'reunion'} ({fuente.fecha})"
        lineas.append(f"{cabecera}\n{fuente.texto}")
    return "\n\n".join(lineas)


def construir_mensajes(
    pregunta: str,
    fuentes: list[Fuente],
    *,
    historial: list[dict] | None = None,
    glosario: str | None = None,
    hoy: str | None = None,
) -> list[dict]:
    sistema = PROMPT_RESPUESTA + f"\nHOY es {hoy or date.today().isoformat()}."
    if glosario:
        sistema += (
            "\n\nGlosario del proyecto. Uselo para entender la jerga interna y "
            "para reconocer terminos que la transcripcion automatica haya "
            "deformado:\n\n" + glosario
        )
    mensajes = [{"role": "system", "content": sistema}]
    for turno in historial or []:
        if turno.get("role") in ("user", "assistant") and turno.get("content"):
            mensajes.append({"role": turno["role"], "content": turno["content"]})

    if fuentes:
        cuerpo = f"Fuentes:\n\n{_bloque_fuentes(fuentes)}\n\nPregunta: {pregunta}"
    else:
        cuerpo = (
            "No hay ninguna fuente en el historico para esta pregunta. "
            "Responde que no consta y no anadas nada mas.\n\n"
            f"Pregunta: {pregunta}"
        )
    mensajes.append({"role": "user", "content": cuerpo})
    return mensajes


def responder_en_streaming(
    pregunta: str,
    *,
    conn_hist: sqlite3.Connection,
    conn_idx: sqlite3.Connection | None = None,
    historial: list[dict] | None = None,
    filtros: dict | None = None,
    config: Config | None = None,
    glosario: str | None = None,
    hoy: str | None = None,
    cliente=llm,
):
    """El motor entero, rindiendo eventos.

    Rinde tuplas `(tipo, dato)` con tipo en `plan`, `fuentes`, `texto`, `fin`
    y `error`. La CLI las imprime y la ruta SSE las serializa: una sola
    implementacion para las dos caras, que es lo que pide el plan.

    `cliente` se inyecta para que las pruebas no llamen a nadie.
    """
    config = config or Config()
    pregunta = (pregunta or "").strip()
    if not pregunta:
        yield ("error", "La pregunta esta vacia.")
        return

    plan = interpretar(pregunta, config=config, hoy=hoy)
    for clave, valor in (filtros or {}).items():
        # Lo que pide el usuario en la interfaz manda sobre lo que dedujo el
        # modelo: si ha elegido un periodo, no se lo puede reinterpretar.
        if valor:
            plan[clave] = valor
    yield ("plan", plan)

    try:
        fuentes, modo, aviso = recuperar(
            conn_hist, conn_idx, plan, pregunta, config=config
        )
    except Exception as exc:  # noqa: BLE001
        yield ("error", f"No se pudo consultar el historico: {exc}")
        return

    yield (
        "fuentes",
        {
            "fuentes": [f.a_dict() for f in fuentes],
            "modo": modo,
            "aviso": aviso,
        },
    )

    mensajes = construir_mensajes(
        pregunta, fuentes, historial=historial, glosario=glosario, hoy=hoy
    )
    try:
        for trozo in cliente.chat_stream(
            config.base_url,
            config.api_key,
            config.modelo,
            mensajes,
            temperature=TEMPERATURA_RESPUESTA,
        ):
            yield ("texto", trozo)
    except Exception as exc:  # noqa: BLE001
        yield ("error", str(exc))
        return

    yield ("fin", {"fuentes": len(fuentes), "modo": modo})


def responder(pregunta: str, **kwargs) -> dict:
    """La respuesta entera de una vez. Para la CLI con `--json` y los tests."""
    texto = []
    fuentes: list[dict] = []
    modo = "vacia"
    aviso = None
    error = None
    for tipo, dato in responder_en_streaming(pregunta, **kwargs):
        if tipo == "texto":
            texto.append(dato)
        elif tipo == "fuentes":
            fuentes, modo, aviso = dato["fuentes"], dato["modo"], dato["aviso"]
        elif tipo == "error":
            error = dato
    return {
        "respuesta": "".join(texto),
        "fuentes": fuentes,
        "modo": modo,
        "aviso": aviso,
        "error": error,
    }


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _abrir_indice(ruta, usar: bool):
    """El indice es opcional: sin el se responde con busqueda literal."""
    if not usar:
        return None
    try:
        return rag.conectar(ruta, solo_lectura=True)
    except FileNotFoundError:
        print(
            "Aviso: no hay indice semantico; se responde solo con busqueda "
            "literal. Construyelo con `python indexar_teams.py`.",
            file=sys.stderr,
        )
        return None


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Pregunta en castellano sobre el historico de reuniones."
    )
    parser.add_argument("pregunta", help="La pregunta, entre comillas")
    parser.add_argument("--desde", default=None, metavar="AAAA-MM-DD")
    parser.add_argument("--hasta", default=None, metavar="AAAA-MM-DD")
    parser.add_argument("--persona", default=None)
    parser.add_argument("--tipo", choices=sorted(memoria.TIPOS_REUNION), default=None)
    parser.add_argument("--db", default=None, help="Ruta de meetings.db (o TEAMS_DB)")
    parser.add_argument("--indice", default=None, help="Ruta de indice.db (o TEAMS_INDICE)")
    parser.add_argument(
        "--sin-indice", action="store_true",
        help="No usar el indice semantico (solo busqueda literal)",
    )
    parser.add_argument("--model", default=None, help="Modelo de chat (o TEAMS_LLM_MODELO)")
    parser.add_argument("--base-url", default=None, help="URL del chat (o LITELLM_BASE_URL)")
    parser.add_argument(
        "--base-url-embeddings",
        default=None,
        help="URL del modelo de embeddings si no es la misma (o TEAMS_EMBED_BASE_URL)",
    )
    parser.add_argument("--api-key", default=None, help="API key (o LITELLM_API_KEY)")
    parser.add_argument("--glosario", default=None, help="Ruta del glosario")
    parser.add_argument("--sin-glosario", action="store_true")
    parser.add_argument(
        "--json", action="store_true",
        help="Vuelca respuesta y fuentes como JSON en vez de texto",
    )
    args = parser.parse_args()

    config = Config(
        base_url=args.base_url or "",
        base_url_embeddings=args.base_url_embeddings or "",
        api_key=args.api_key,
        modelo=args.model or "",
        usar_indice=not args.sin_indice,
    )
    ruta_db = memoria.ruta_bd(args.db)
    if not ruta_db.exists():
        print(f"No se encuentra la base de datos en {ruta_db}.", file=sys.stderr)
        sys.exit(1)

    glosario = None
    if not args.sin_glosario:
        contenido = glosario_mod.cargar(args.glosario)
        if contenido:
            glosario = glosario_mod.texto_completo(contenido)

    conn_hist = memoria.conectar(ruta_db, solo_lectura=True)
    conn_idx = _abrir_indice(args.indice, not args.sin_indice)
    filtros = {
        "desde": args.desde, "hasta": args.hasta,
        "persona": args.persona, "tipo": args.tipo,
    }
    try:
        if args.json:
            resultado = responder(
                args.pregunta, conn_hist=conn_hist, conn_idx=conn_idx,
                filtros=filtros, config=config, glosario=glosario,
            )
            print(json.dumps(resultado, ensure_ascii=False, indent=2))
            sys.exit(2 if resultado["error"] else 0)

        codigo = 0
        citadas: list[dict] = []
        for tipo, dato in responder_en_streaming(
            args.pregunta, conn_hist=conn_hist, conn_idx=conn_idx,
            filtros=filtros, config=config, glosario=glosario,
        ):
            if tipo == "fuentes":
                citadas = dato["fuentes"]
                print(
                    f"{len(dato['fuentes'])} fuentes "
                    f"(busqueda {dato['modo']}).\n",
                    file=sys.stderr,
                )
                if dato["aviso"]:
                    print(f"Aviso: {dato['aviso']}\n", file=sys.stderr)
            elif tipo == "texto":
                print(dato, end="", flush=True)
            elif tipo == "error":
                print(f"\nError: {dato}", file=sys.stderr)
                codigo = 2
            elif tipo == "fin":
                print()
        _imprimir_fuentes(citadas)
        sys.exit(codigo)
    finally:
        conn_hist.close()
        if conn_idx is not None:
            conn_idx.close()


def _imprimir_fuentes(fuentes: list[dict]) -> None:
    """Resuelve los [n] del texto a reuniones concretas.

    En la web cada cita es un enlace al segmento; en consola lo mas parecido
    es decir de que reunion y de que linea salio, para poder ir a mirarlo.
    """
    if not fuentes:
        return
    print("\nFuentes:", file=sys.stderr)
    for fuente in fuentes:
        donde = fuente["titulo"] or fuente["uid"] or "?"
        linea = f" (linea {fuente['idx']})" if fuente["idx"] is not None else ""
        print(
            f"  [{fuente['n']}] {donde} — {fuente['fecha'] or '?'}{linea}",
            file=sys.stderr,
        )


if __name__ == "__main__":
    main()
