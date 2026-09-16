"""
perfiles_voz.py

Perfiles de voz persistentes (Fase 3 de PLAN_MEJORAS.md).

Guarda, por persona, el centroide de su embedding de voz y cuantas muestras
lo han formado, en `datos/perfiles_voz.json`. Con eso, la diarizacion de
`transcribe_teams.py` puede etiquetar `[Pablo Gil]` en vez de `[SPEAKER_03]`,
y usar el mismo nombre para la misma persona en reuniones distintas.

Este modulo es **solo stdlib y deliberadamente puro**: no importa numpy, ni
torch, ni pyannote. Los embeddings entran y salen como listas de float. Toda
la logica que se puede probar sin GPU vive aqui (similitud, umbral, media
incremental, renombrado de turnos, estadisticas de habla) y
`transcribe_teams.py` solo orquesta. Asi los tests corren en el servidor
offline sin instalar nada.

AVISO LEGAL: un perfil de voz es un dato biometrico a efectos de RGPD. El
consentimiento para grabar una reunion no cubre automaticamente crear un
perfil de voz reutilizable de alguien. `--olvidar` borra el de una persona.

CLI de mantenimiento:
    python perfiles_voz.py --listar
    python perfiles_voz.py --olvidar "Pablo Gil"
    python perfiles_voz.py --renombrar "Pablo" "Pablo Gil"
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from datetime import datetime
from pathlib import Path

# Version del formato del fichero. Si cambia la forma de calcular el
# centroide, se sube y los perfiles anteriores se descartan con un aviso en
# vez de mezclar vectores que ya no significan lo mismo.
VERSION_PERFILES = 1

# Calibrado el 2026-09-10 con dos reuniones reales (7 y 10 de septiembre) en
# el servidor con pyannote 4.0.7 / wespeaker-voxceleb-resnet34-LM (256 dims),
# comparando los embeddings de cada hablante entre tramos distintos:
#
#   misma persona, tramos y reuniones distintas : 0,729 - 0,968 (tipico ~0,89)
#   personas distintas                          : 0,05 - 0,52 (0,638 en un
#                                                 tramo corto de 2 minutos)
#
# El 0,6 que proponia el plan cae dentro del ruido, asi que el punto de corte
# se pone en 0,70: por encima del peor caso de un impostor medido y por debajo
# del peor acierto. Sigue siendo ajustable con --umbral-voz; recalibrar con
# --dry-run-voz si se cambia de modelo de embeddings.
UMBRAL_POR_DEFECTO = 0.7

# Los dos extremos de la calibracion (0,729 acertado y 0,638 equivocado) son
# ambos de hablantes con menos de 20 s de habla: con poco audio el embedding
# de wespeaker no representa bien a nadie. No se crea ni se actualiza un
# centroide con una muestra asi, porque el error queda guardado para siempre;
# identificar con ella si se permite, porque eso solo afecta a esta reunion.
MINIMO_SEGUNDOS_MUESTRA = 15.0

RUTA_POR_DEFECTO = Path("datos") / "perfiles_voz.json"

# Nombres que no identifican a nadie: los mismos que rechaza
# `memoria.obtener_o_crear_persona`, para que un perfil no pueda llamarse de
# una forma que luego la BD tira a la basura.
NOMBRES_GENERICOS = {"no identificado", "desconocido", "n/a", "-", ""}


class PerfilesIncompatibles(RuntimeError):
    """El fichero de perfiles no se puede usar con estos embeddings."""


# --------------------------------------------------------------------------
# Carga y guardado
# --------------------------------------------------------------------------


def ruta_perfiles(ruta: str | os.PathLike | None = None) -> Path:
    """Resuelve la ruta del fichero: --perfiles > TEAMS_PERFILES_VOZ >
    datos/perfiles_voz.json."""
    if ruta:
        return Path(ruta)
    entorno = os.environ.get("TEAMS_PERFILES_VOZ")
    if entorno:
        return Path(entorno)
    return RUTA_POR_DEFECTO


def perfiles_vacios(modelo: str | None = None) -> dict:
    return {
        "version": VERSION_PERFILES,
        "modelo": modelo,
        "dimension": None,
        "personas": {},
    }


def cargar(ruta: str | os.PathLike | None = None) -> dict:
    """Lee el fichero de perfiles. Si no existe, devuelve uno vacio: no tener
    perfiles es el caso normal la primera vez, no un error."""
    destino = ruta_perfiles(ruta)
    if not destino.exists():
        return perfiles_vacios()
    try:
        datos = json.loads(destino.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PerfilesIncompatibles(
            f"No se pudo leer '{destino}': {exc}"
        ) from exc
    if not isinstance(datos, dict) or "personas" not in datos:
        raise PerfilesIncompatibles(
            f"'{destino}' no tiene el formato esperado (falta 'personas')"
        )
    if datos.get("version") != VERSION_PERFILES:
        raise PerfilesIncompatibles(
            f"'{destino}' esta en la version {datos.get('version')} y este "
            f"codigo usa la {VERSION_PERFILES}. Borra el fichero y vuelve a "
            "dar de alta al equipo con --enroll."
        )
    return _normalizar(datos)


def _normalizar(datos: dict) -> dict:
    """Tolera claves ausentes o del tipo equivocado, igual que
    `summarize_teams.normalizar`: un fichero editado a mano no debe tumbar
    una transcripcion de 40 minutos."""
    personas: dict[str, dict] = {}
    for nombre, perfil in (datos.get("personas") or {}).items():
        if not isinstance(perfil, dict):
            continue
        centroide = perfil.get("centroide")
        if not isinstance(centroide, list) or not centroide:
            continue
        try:
            vector = [float(x) for x in centroide]
        except (TypeError, ValueError):
            continue
        muestras = perfil.get("muestras")
        personas[str(nombre)] = {
            "centroide": vector,
            "muestras": int(muestras) if isinstance(muestras, (int, float)) else 1,
            "actualizado": perfil.get("actualizado"),
        }
    return {
        "version": VERSION_PERFILES,
        "modelo": datos.get("modelo"),
        "dimension": datos.get("dimension"),
        "personas": personas,
    }


def guardar(perfiles: dict, ruta: str | os.PathLike | None = None) -> Path:
    """Escribe el fichero de forma atomica (temporal + replace): un Ctrl-C a
    mitad no puede dejar el fichero de perfiles truncado."""
    destino = ruta_perfiles(ruta)
    destino.parent.mkdir(parents=True, exist_ok=True)
    temporal = destino.with_suffix(destino.suffix + ".tmp")
    temporal.write_text(
        json.dumps(perfiles, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temporal.replace(destino)
    return destino


# --------------------------------------------------------------------------
# Similitud e identificacion
# --------------------------------------------------------------------------


def similitud(a, b) -> float:
    """Similitud coseno entre dos vectores. Devuelve 0.0 si alguno es nulo o
    tiene dimension distinta: un vector degenerado no se parece a nada."""
    if a is None or b is None or len(a) != len(b) or not a:
        return 0.0
    producto = 0.0
    norma_a = 0.0
    norma_b = 0.0
    for x, y in zip(a, b):
        producto += x * y
        norma_a += x * x
        norma_b += y * y
    if norma_a <= 0.0 or norma_b <= 0.0:
        return 0.0
    return producto / math.sqrt(norma_a * norma_b)


def es_vector_valido(vector) -> bool:
    """Un embedding con NaN no es un fallo del pipeline: pyannote los devuelve
    para hablantes de los que no pudo extraer muestras suficientes."""
    if not vector:
        return False
    for x in vector:
        if not isinstance(x, (int, float)) or math.isnan(x) or math.isinf(x):
            return False
    return any(x != 0.0 for x in vector)


def comprobar_dimension(perfiles: dict, dimension: int) -> None:
    """Mezclar vectores de dos modelos no da ningun error, solo resultados sin
    sentido (misma trampa que `rag._comprobar_compatible`). Se para en seco."""
    guardada = perfiles.get("dimension")
    if guardada and dimension and int(guardada) != int(dimension):
        raise PerfilesIncompatibles(
            f"Los perfiles guardados son de {guardada} dimensiones y los "
            f"embeddings actuales tienen {dimension}. Vienen de modelos "
            "distintos: borra datos/perfiles_voz.json y vuelve a dar de alta "
            "al equipo con --enroll."
        )


def clasificar(
    embeddings: dict[str, list[float]],
    perfiles: dict,
    umbral: float = UMBRAL_POR_DEFECTO,
) -> dict[str, dict]:
    """Para cada etiqueta de pyannote, devuelve a quien se parece mas.

    El resultado es un dict por etiqueta con `nombre` (o None si no llega al
    umbral o el embedding no sirve), `similitud` (la mejor encontrada) y
    `candidatos` (las tres mejores, para que --dry-run-voz enseñe el margen
    con el segundo: un 0,61 contra un 0,59 no es la misma decision que un
    0,80 contra un 0,20).

    La asignacion es **exclusiva**: dos hablantes distintos de la misma
    reunion no pueden ser la misma persona, asi que se resuelve por similitud
    descendente y el nombre ya tomado se descarta para los siguientes.
    """
    personas = perfiles.get("personas") or {}

    tabla: dict[str, list[tuple[str, float]]] = {}
    for etiqueta, vector in embeddings.items():
        if not es_vector_valido(vector):
            tabla[etiqueta] = []
            continue
        parecidos = [
            (nombre, similitud(vector, perfil["centroide"]))
            for nombre, perfil in personas.items()
        ]
        parecidos.sort(key=lambda par: par[1], reverse=True)
        tabla[etiqueta] = parecidos

    resultado: dict[str, dict] = {
        etiqueta: {
            "nombre": None,
            "similitud": (parecidos[0][1] if parecidos else 0.0),
            "candidatos": parecidos[:3],
        }
        for etiqueta, parecidos in tabla.items()
    }

    # Orden global de todas las parejas (etiqueta, persona) que superan el
    # umbral, de mejor a peor; se van fijando sin repetir ni etiqueta ni
    # persona.
    parejas = [
        (etiqueta, nombre, valor)
        for etiqueta, parecidos in tabla.items()
        for nombre, valor in parecidos
        if valor >= umbral
    ]
    parejas.sort(key=lambda p: p[2], reverse=True)

    etiquetas_usadas: set[str] = set()
    nombres_usados: set[str] = set()
    for etiqueta, nombre, valor in parejas:
        if etiqueta in etiquetas_usadas or nombre in nombres_usados:
            continue
        etiquetas_usadas.add(etiqueta)
        nombres_usados.add(nombre)
        resultado[etiqueta]["nombre"] = nombre
        resultado[etiqueta]["similitud"] = valor

    return resultado


# --------------------------------------------------------------------------
# Actualizacion de centroides
# --------------------------------------------------------------------------


def nombre_valido(nombre: str | None) -> bool:
    if not nombre:
        return False
    limpio = " ".join(str(nombre).split())
    if not limpio or limpio.lower() in NOMBRES_GENERICOS:
        return False
    return not limpio.lower().startswith("speaker_")


def limpiar_nombre(nombre: str) -> str:
    return " ".join(str(nombre).split())


def actualizar(perfiles: dict, nombre: str, embedding, modelo: str | None = None) -> dict:
    """Incorpora una muestra al perfil de una persona como media incremental,
    de modo que el perfil mejora con cada reunion y una sola grabacion mala no
    lo arrastra. Modifica y devuelve `perfiles`."""
    if not nombre_valido(nombre):
        raise ValueError(f"'{nombre}' no identifica a nadie; no se guarda perfil")
    if not es_vector_valido(embedding):
        raise ValueError(f"El embedding de '{nombre}' no es utilizable")

    vector = [float(x) for x in embedding]
    comprobar_dimension(perfiles, len(vector))

    personas = perfiles.setdefault("personas", {})
    clave = _clave_persona(personas, nombre)
    perfil = personas.get(clave)

    if perfil is None:
        personas[limpiar_nombre(nombre)] = {
            "centroide": vector,
            "muestras": 1,
            "actualizado": datetime.now().isoformat(timespec="seconds"),
        }
    else:
        anterior = perfil["centroide"]
        if len(anterior) != len(vector):
            raise PerfilesIncompatibles(
                f"El perfil de '{clave}' tiene {len(anterior)} dimensiones y "
                f"la muestra nueva {len(vector)}"
            )
        n = max(1, int(perfil.get("muestras", 1)))
        perfil["centroide"] = [
            (viejo * n + nuevo) / (n + 1) for viejo, nuevo in zip(anterior, vector)
        ]
        perfil["muestras"] = n + 1
        perfil["actualizado"] = datetime.now().isoformat(timespec="seconds")

    perfiles["dimension"] = len(vector)
    if modelo:
        perfiles["modelo"] = modelo
    perfiles.setdefault("version", VERSION_PERFILES)
    return perfiles


def _clave_persona(personas: dict, nombre: str) -> str | None:
    """Emparejamiento por nombre sin distinguir mayusculas, igual que
    `memoria.obtener_o_crear_persona`: 'pablo gil' y 'Pablo Gil' son la misma
    persona y no dos perfiles a medias."""
    bajo = limpiar_nombre(nombre).lower()
    for clave in personas:
        if clave.lower() == bajo:
            return clave
    return None


def olvidar(perfiles: dict, nombre: str) -> bool:
    """Borra el perfil de una persona (RGPD). True si existia."""
    personas = perfiles.get("personas") or {}
    clave = _clave_persona(personas, nombre)
    if clave is None:
        return False
    del personas[clave]
    return True


def renombrar(perfiles: dict, viejo: str, nuevo: str) -> bool:
    """Corrige el nombre de un perfil conservando su centroide y muestras."""
    personas = perfiles.get("personas") or {}
    clave = _clave_persona(personas, viejo)
    if clave is None:
        return False
    if not nombre_valido(nuevo):
        raise ValueError(f"'{nuevo}' no identifica a nadie")
    destino = _clave_persona(personas, nuevo)
    if destino is not None and destino != clave:
        raise ValueError(f"Ya existe un perfil para '{destino}'")
    personas[limpiar_nombre(nuevo)] = personas.pop(clave)
    return True


# --------------------------------------------------------------------------
# Turnos y muestras (lo que necesita transcribe_teams.py, sin numpy)
# --------------------------------------------------------------------------


def renombrar_turnos(
    turns: list[tuple[float, float, str]], nombres: dict[str, str | None]
) -> list[tuple[float, float, str]]:
    """Devuelve los turnos con la etiqueta de pyannote sustituida por el
    nombre de la persona donde lo haya. Las que no se reconocen conservan su
    `SPEAKER_XX` y las resuelve el LLM por contexto, como hasta ahora."""
    return [
        (inicio, fin, nombres.get(etiqueta) or etiqueta)
        for inicio, fin, etiqueta in turns
    ]


def estadisticas_hablantes(
    turns: list[tuple[float, float, str]],
    segments=None,
    maximo_muestra: int = 160,
) -> dict[str, dict]:
    """Duracion de habla, numero de turnos y un fragmento de transcripcion por
    etiqueta. Es lo que se le enseña a quien ejecuta `--enroll` para que pueda
    decidir quien es cada hablante sin escuchar el audio entero."""
    stats: dict[str, dict] = {}
    for inicio, fin, etiqueta in turns:
        entrada = stats.setdefault(
            etiqueta, {"segundos": 0.0, "turnos": 0, "muestra": ""}
        )
        entrada["segundos"] += max(0.0, float(fin) - float(inicio))
        entrada["turnos"] += 1

    if segments:
        for etiqueta, entrada in stats.items():
            entrada["muestra"] = _muestra_de_texto(
                turns, segments, etiqueta, maximo_muestra
            )
    return stats


def _muestra_de_texto(turns, segments, etiqueta, maximo: int) -> str:
    """El fragmento se toma del turno mas **largo** de esa etiqueta, no del
    primero: el primero suele ser un 'hola' que no distingue a nadie."""
    turnos = [t for t in turns if t[2] == etiqueta]
    if not turnos:
        return ""
    inicio, fin, _ = max(turnos, key=lambda t: t[1] - t[0])
    trozos = []
    for seg in segments:
        solapa = min(float(seg["end"]), fin) - max(float(seg["start"]), inicio)
        if solapa > 0:
            trozos.append(str(seg.get("text", "")).strip())
        if sum(len(t) for t in trozos) > maximo:
            break
    texto = " ".join(t for t in trozos if t)
    if len(texto) > maximo:
        texto = texto[:maximo].rstrip() + "..."
    return texto


def formato_duracion(segundos: float) -> str:
    minutos, seg = divmod(int(round(segundos)), 60)
    return f"{minutos}m{seg:02d}s"


def informe_clasificacion(
    clasificacion: dict[str, dict],
    stats: dict[str, dict],
    umbral: float,
) -> str:
    """Tabla de texto con lo que se ha decidido y con que margen. Es la salida
    de --dry-run-voz, que muestra las similitudes sin aplicarlas."""
    lineas = [f"Similitudes de voz (umbral {umbral:.2f}):"]
    for etiqueta in sorted(clasificacion):
        datos = clasificacion[etiqueta]
        info = stats.get(etiqueta, {})
        habla = formato_duracion(info.get("segundos", 0.0))
        candidatos = datos.get("candidatos") or []
        if candidatos:
            detalle = ", ".join(f"{n} {v:.3f}" for n, v in candidatos)
        else:
            detalle = "sin perfiles comparables"
        decision = datos.get("nombre") or "(sin asignar)"
        lineas.append(f"  {etiqueta}  {habla:>7}  -> {decision}   [{detalle}]")
    return "\n".join(lineas)


# --------------------------------------------------------------------------
# CLI de mantenimiento
# --------------------------------------------------------------------------


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Mantenimiento de los perfiles de voz (datos/perfiles_voz.json)."
    )
    parser.add_argument(
        "--perfiles",
        default=None,
        help="Ruta al fichero de perfiles (por defecto: TEAMS_PERFILES_VOZ o datos/perfiles_voz.json)",
    )
    grupo = parser.add_mutually_exclusive_group(required=True)
    grupo.add_argument("--listar", action="store_true", help="Lista las personas con perfil")
    grupo.add_argument("--olvidar", metavar="NOMBRE", help="Borra el perfil de voz de una persona")
    grupo.add_argument(
        "--renombrar",
        nargs=2,
        metavar=("VIEJO", "NUEVO"),
        help="Cambia el nombre de un perfil conservando su centroide",
    )
    args = parser.parse_args(argv)

    try:
        perfiles = cargar(args.perfiles)
    except PerfilesIncompatibles as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    destino = ruta_perfiles(args.perfiles)

    if args.listar:
        personas = perfiles.get("personas") or {}
        print(f"Fichero:   {destino}")
        print(f"Modelo:    {perfiles.get('modelo') or '(sin anotar)'}")
        print(f"Dimension: {perfiles.get('dimension') or '(vacio)'}")
        if not personas:
            print("\nNo hay ningun perfil todavia. Usa "
                  "'transcribe_teams.py ... --diarize --enroll' para dar de alta al equipo.")
            return 0
        print(f"\n{len(personas)} persona(s):")
        for nombre in sorted(personas):
            perfil = personas[nombre]
            print(
                f"  {nombre}  ({perfil['muestras']} muestra(s), "
                f"actualizado {perfil.get('actualizado') or '?'})"
            )
        return 0

    if args.olvidar:
        if olvidar(perfiles, args.olvidar):
            guardar(perfiles, args.perfiles)
            print(f"Perfil de voz de '{args.olvidar}' borrado de {destino}.")
            return 0
        print(f"No hay perfil de voz para '{args.olvidar}'.", file=sys.stderr)
        return 1

    viejo, nuevo = args.renombrar
    try:
        cambiado = renombrar(perfiles, viejo, nuevo)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    if not cambiado:
        print(f"No hay perfil de voz para '{viejo}'.", file=sys.stderr)
        return 1
    guardar(perfiles, args.perfiles)
    print(f"Perfil '{viejo}' renombrado a '{nuevo}'.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
