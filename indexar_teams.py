"""
indexar_teams.py

Construye y mantiene al dia el indice semantico (`datos/indice.db`) a partir
del historico (`datos/meetings.db`). Es lo que hace que el chat de la web y
`ask_teams.py` puedan buscar por significado y no solo por palabra exacta.

Uso:
    python indexar_teams.py                      # indexa lo que falte
    python indexar_teams.py --reconstruir        # lo rehace entero
    python indexar_teams.py --reunion 20260907_090437_mixed
    python indexar_teams.py --info               # que hay indexado

Notas:
  - Requiere `sqlite-vec` (`pip install sqlite-vec`) y un modelo de embeddings
    servido por LiteLLM (`--modelo`, por defecto bge-m3). Sin ellos, avisa y
    sale sin tocar nada: el chat sigue funcionando con busqueda literal.
  - Reindexa sola una reunion cuya transcripcion haya cambiado (reproceso).
    Volver a ejecutarlo cuando no ha cambiado nada no gasta ni una llamada.
  - El indice es reconstruible: borrarlo no pierde ningun dato.
"""

import argparse
import sys

import llm
import memoria
import rag

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def _imprimir_info(datos: dict, ruta) -> None:
    print(f"Indice:      {ruta}")
    print(f"Fragmentos:  {datos['chunks']} de {datos['reuniones']} reuniones")
    print(f"Modelo:      {datos['modelo'] or '(ninguno)'}")
    print(f"Dimensiones: {datos['dimensiones'] or '-'}")
    print(f"Acciones:    {datos['acciones']} con vector ({datos['modelo_acciones'] or '-'})")
    print(f"Extension:   {'sqlite-vec cargada' if datos['extension'] else 'NO disponible'}")
    print(f"Busqueda:    {'hibrida' if datos['densa'] else 'solo literal (FTS5)'}")
    print(f"Ultima vez:  {datos['indexado_en'] or '-'}")


def _progreso(resultado: dict) -> None:
    if resultado["accion"] == "al dia":
        return
    detalle = resultado.get("motivo") or f"{resultado.get('chunks', 0)} fragmentos"
    print(f"  {resultado['uid']}: {resultado['accion']} ({detalle})")


def _imprimir_calibracion(datos: dict, umbral: float, minimo: float) -> None:
    print("\nModo simulado: no se escribe nada en el indice.")
    print(
        f"{datos['dimensiones']} dimensiones, {datos['llamadas']} llamadas al modelo."
    )
    print(
        f"Umbral {umbral:.2f}; se muestran las parejas con similitud "
        f">= {minimo:.2f}."
    )

    def bloque(fila: dict, etiqueta: str) -> None:
        print(f"  {fila['similitud']:.3f}  {etiqueta}")
        print(f"           {fila['a_uid']}  {fila['a_descripcion']}")
        print(f"           {fila['b_uid']}  {fila['b_descripcion']}")

    print(f"\nParejas ({len(datos['parejas'])}):")
    for fila in datos["parejas"]:
        if fila.get("sugerida"):
            etiqueta = "SUGERENCIA"
        elif fila["propuesta"]:
            etiqueta = "pasa el umbral, pero comparte accion"
        else:
            etiqueta = "no se propone"
        if fila["motivo"]:
            etiqueta += f" ({fila['motivo']})"
        bloque(fila, etiqueta)

    sugerencias = sum(1 for fila in datos["parejas"] if fila.get("sugerida"))
    print(f"\nSugerencias reales (una por accion): {sugerencias}")

    print(f"\nReferencia: fusiones hechas a mano ({len(datos['referencias'])}):")
    if not datos["referencias"]:
        print("  (no hay ninguna: el umbral no se puede medir contra la verdad)")
    for fila in datos["referencias"]:
        etiqueta = "llega al umbral" if fila["propuesta"] else "NO llega al umbral"
        bloque(fila, etiqueta)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Construye el indice semantico del historico de reuniones."
    )
    parser.add_argument("--db", default=None, help="Ruta de meetings.db (o TEAMS_DB)")
    parser.add_argument("--indice", default=None, help="Ruta de indice.db (o TEAMS_INDICE)")
    parser.add_argument(
        "--reunion", action="append", metavar="UID", default=None,
        help="Indexa solo esta reunion (repetible)",
    )
    parser.add_argument(
        "--reconstruir", action="store_true",
        help="Borra el indice y lo rehace entero (obligatorio al cambiar de modelo)",
    )
    parser.add_argument(
        "--acciones", action="store_true",
        help="Calcula tambien los vectores de las acciones (sugerencias de duplicados)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Con --acciones: ensena las parejas parecidas sin escribir nada",
    )
    parser.add_argument(
        "--umbral", type=float, default=None,
        help="Con --dry-run, similitud minima para proponer un duplicado "
        f"(por defecto: {rag.UMBRAL_SUGERENCIA})",
    )
    parser.add_argument(
        "--minimo", type=float, default=None,
        help="Con --dry-run, similitud minima a mostrar (por defecto: 0.5)",
    )
    parser.add_argument("--info", action="store_true", help="Muestra el estado y sale")
    parser.add_argument("--modelo", default=None, help="Modelo de embeddings (o TEAMS_EMBED_MODELO)")
    parser.add_argument(
        "--base-url", default=None,
        help="URL del modelo de embeddings (o TEAMS_EMBED_BASE_URL)",
    )
    parser.add_argument("--api-key", default=None, help="API key (o LITELLM_API_KEY)")
    parser.add_argument(
        "--prefijo", default=None,
        help="Prefijo de tarea del modelo, si lo exige (o TEAMS_EMBED_PREFIJO)",
    )
    args = parser.parse_args()

    if args.dry_run and not args.acciones:
        parser.error("--dry-run solo tiene sentido con --acciones")
    if args.info and (args.acciones or args.dry_run):
        parser.error("--info no se combina con --acciones ni --dry-run")
    if not args.dry_run and (args.umbral is not None or args.minimo is not None):
        parser.error("--umbral y --minimo solo tienen sentido con --dry-run")

    umbral = args.umbral if args.umbral is not None else rag.UMBRAL_SUGERENCIA
    minimo = args.minimo if args.minimo is not None else 0.5

    ruta_indice = rag.ruta_indice(args.indice)

    if args.info:
        try:
            conn_idx = rag.conectar(ruta_indice)
        except Exception as exc:  # noqa: BLE001
            print(f"No se pudo abrir el indice: {exc}", file=sys.stderr)
            sys.exit(1)
        _imprimir_info(rag.info(conn_idx), ruta_indice)
        conn_idx.close()
        return

    if not rag.hay_extension():
        print(
            "sqlite-vec no esta disponible en este Python, asi que no se pueden\n"
            "guardar vectores. Instalalo con `pip install sqlite-vec`.\n"
            "Mientras tanto el chat responde con busqueda literal (FTS5).",
            file=sys.stderr,
        )
        sys.exit(1)

    base = llm.base_url_embeddings(args.base_url)
    clave = llm.api_key(args.api_key)
    modelo = llm.modelo_embeddings(args.modelo)
    prefijo = llm.prefijo_embeddings(args.prefijo)

    ruta_db = memoria.ruta_bd(args.db)
    if not ruta_db.exists():
        print(f"No se encuentra la base de datos en {ruta_db}.", file=sys.stderr)
        sys.exit(1)

    print(f"Historico: {ruta_db}")
    print(f"Indice:    {ruta_indice}")
    print(f"Modelo:    {modelo} en {base}")
    if args.reconstruir:
        print("Reconstruyendo el indice desde cero.")

    embebedor = rag.embebedor_litellm(base, clave, modelo, prefijo)
    conn_hist = memoria.conectar(ruta_db, solo_lectura=True)
    conn_idx = None if args.dry_run else rag.conectar(ruta_indice)
    try:
        if args.dry_run:
            datos = rag.calibrar_acciones(
                memoria.acciones_para_indice(conn_hist),
                memoria.fusiones_de_referencia(conn_hist),
                embebedor=embebedor,
                umbral=umbral,
                minimo=minimo,
                rechazadas=memoria.sugerencias_rechazadas(conn_hist),
            )
            _imprimir_calibracion(datos, umbral, minimo)
            return

        resumen = rag.indexar(
            conn_idx,
            conn_hist,
            embebedor=embebedor,
            modelo=modelo,
            uids=args.reunion,
            reconstruir=args.reconstruir,
            progreso=_progreso,
        )
        huerfanas = rag.limpiar_huerfanas(conn_idx, conn_hist)
        acciones = None
        if args.acciones:
            acciones = rag.indexar_acciones(
                conn_idx,
                conn_hist,
                embebedor=embebedor,
                modelo=modelo,
                forzar=args.reconstruir,
            )
    except (rag.ErrorIndice, llm.ErrorLLM) as exc:
        print(f"\n{exc}", file=sys.stderr)
        sys.exit(2)
    finally:
        conn_hist.close()
        if conn_idx is not None:
            conn_idx.close()

    print(
        f"\nIndexadas {resumen['indexadas']}, reindexadas {resumen['reindexadas']}, "
        f"al dia {resumen['al dia']}, omitidas {resumen['omitidas']}. "
        f"{resumen['chunks']} fragmentos nuevos."
    )
    if huerfanas:
        print(f"Retiradas {huerfanas} reuniones que ya no estan en el historico.")
    if not resumen["densa"]:
        print(
            "Aviso: no se han guardado vectores; la busqueda sera solo literal.",
            file=sys.stderr,
        )
    if acciones is not None:
        print(
            f"Acciones: {acciones['acciones']} con vector "
            f"({acciones['nuevas']} nuevas, {acciones['actualizadas']} actualizadas, "
            f"{acciones['al dia']} al dia, {acciones['retiradas']} retiradas)."
        )
        if not acciones["acciones"]:
            print(
                "Aviso: no hay ninguna accion con descripcion que indexar.",
                file=sys.stderr,
            )


if __name__ == "__main__":
    main()
