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
    print(f"Extension:   {'sqlite-vec cargada' if datos['extension'] else 'NO disponible'}")
    print(f"Busqueda:    {'hibrida' if datos['densa'] else 'solo literal (FTS5)'}")
    print(f"Ultima vez:  {datos['indexado_en'] or '-'}")


def _progreso(resultado: dict) -> None:
    if resultado["accion"] == "al dia":
        return
    detalle = resultado.get("motivo") or f"{resultado.get('chunks', 0)} fragmentos"
    print(f"  {resultado['uid']}: {resultado['accion']} ({detalle})")


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

    conn_hist = memoria.conectar(ruta_db, solo_lectura=True)
    conn_idx = rag.conectar(ruta_indice)
    try:
        resumen = rag.indexar(
            conn_idx,
            conn_hist,
            embebedor=rag.embebedor_litellm(base, clave, modelo, prefijo),
            modelo=modelo,
            uids=args.reunion,
            reconstruir=args.reconstruir,
            progreso=_progreso,
        )
        huerfanas = rag.limpiar_huerfanas(conn_idx, conn_hist)
    except (rag.ErrorIndice, llm.ErrorLLM) as exc:
        print(f"\n{exc}", file=sys.stderr)
        sys.exit(2)
    finally:
        conn_hist.close()
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


if __name__ == "__main__":
    main()
