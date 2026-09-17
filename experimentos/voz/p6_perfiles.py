"""Paso 6: regenera datos/perfiles_voz.json desde las reuniones etiquetadas.

Usa los embeddings de cluster de pyannote (los mismos que usa el pipeline al
identificar, sin centrar) para que el fichero siga siendo compatible con
perfiles_voz.clasificar. Una muestra por (reunion, cluster) con etiqueta
fiable y al menos MINIMO_SEGUNDOS_MUESTRA de habla.

Antes de escribir evalua dejando una reunion fuera: perfiles con las demas,
clasificar la excluida con el umbral por defecto.

    .venv\Scripts\python.exe experimentos\voz\p6_perfiles.py [--escribir]
"""
import argparse
import json
import sys
from collections import Counter, defaultdict

import comun
import perfiles_voz as pv

sys.stdout.reconfigure(encoding="utf-8")
MODELO = "pyannote/speaker-diarization-3.1"
PUREZA_CLUSTER = 0.85
ALIAS = {"Pablo": "Pablo Rodríguez"}  # nombre que ya tenia en perfiles_voz.json


def muestras():
    segmentos = [json.loads(l) for l in (comun.DATOS_VOZ / "segmentos.jsonl").open(encoding="utf-8")]
    por_cluster = defaultdict(list)
    for s in segmentos:
        por_cluster[(s["reunion"], s["cluster"])].append(s)
    salida, descartes = [], []
    for (r, cl), ss in sorted(por_cluster.items()):
        segundos = Counter()
        mic = sorted(s["mic_ratio"] or 0 for s in ss)[len(ss) // 2]
        for s in ss:
            segundos[s["nombre_transcripcion"] if s["pureza"] >= 0.7 else None] += s["fin"] - s["inicio"]
        total = sum(segundos.values())
        nombre, seg = max(((n, v) for n, v in segundos.items() if n), key=lambda x: x[1], default=(None, 0))
        if mic > 0.5:
            nombre, seg = "Javi", total  # mic.wav es etiqueta independiente
        motivo = None
        if not nombre:
            motivo = "sin nombre"
        elif nombre == "Javi" and mic <= 0.5:
            motivo = "la transcripcion dice Javi pero el micro no"
        elif seg / total < PUREZA_CLUSTER:
            motivo = f"cluster mezclado ({seg/total:.0%} {nombre})"
        elif total < pv.MINIMO_SEGUNDOS_MUESTRA:
            motivo = f"poco audio ({total:.0f}s)"
        diar = json.loads((comun.DATOS_VOZ / "diar" / f"{r}.json").read_text(encoding="utf-8"))
        emb = diar["embeddings_cluster"].get(cl)
        fila = {"reunion": r, "cluster": cl, "nombre": ALIAS.get(nombre, nombre), "segundos": round(total), "emb": emb}
        (descartes if motivo else salida).append({**fila, "motivo": motivo})
    return salida, descartes


def construir(filas):
    perfiles = pv.perfiles_vacios(MODELO)
    for f in filas:
        pv.actualizar(perfiles, f["nombre"], f["emb"], MODELO)
    return perfiles


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--escribir", action="store_true")
    args = ap.parse_args()
    filas, descartes = muestras()
    for d in descartes:
        print(f"  descartado {d['reunion'][:8]} {d['cluster']} {d['nombre'] or '-':<16} {d['motivo']}")

    print("\nDejando una reunion fuera (umbral %.2f):" % pv.UMBRAL_POR_DEFECTO)
    for r in comun.reuniones():
        perfiles = construir([f for f in filas if f["reunion"] != r])
        prueba = {f["cluster"]: f for f in filas + descartes if f["reunion"] == r and f["emb"] and f["nombre"]}
        res = pv.clasificar({cl: f["emb"] for cl, f in prueba.items()}, perfiles)
        ok = fallo = sin = 0
        for cl, f in sorted(prueba.items()):
            o = res[cl]
            tiene_perfil = f["nombre"] in perfiles["personas"]
            estado = "ok" if o["nombre"] == f["nombre"] else ("SIN NOMBRE" if o["nombre"] is None else "ERROR")
            if not tiene_perfil and o["nombre"] is None:
                estado = "ok (sin perfil)"
            ok += estado.startswith("ok"); fallo += estado == "ERROR"; sin += estado == "SIN NOMBRE"
            cand = ", ".join(f"{n} {s:.2f}" for n, s in o["candidatos"][:2])
            print(f"  {r[:8]} {cl} {f['nombre']:<16} -> {str(o['nombre']):<16} {estado:<15} [{cand}]")
        print(f"  {r[:8]}: {ok} ok, {sin} sin nombre, {fallo} errores")

    final = construir(filas)
    print("\nPerfiles finales:")
    for n, p in sorted(final["personas"].items()):
        fuentes = [f"{f['reunion'][4:8]}({f['segundos']}s)" for f in filas if f["nombre"] == n]
        print(f"  {n:<16} {p['muestras']} muestras  {' '.join(fuentes)}")
    if args.escribir:
        print(f"\nEscrito: {pv.guardar(final, comun.RAIZ / 'datos' / 'perfiles_voz.json')}")


if __name__ == "__main__":
    main()
