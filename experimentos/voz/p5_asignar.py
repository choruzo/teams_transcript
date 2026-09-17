"""Paso 5: pone nombre a cada linea de una transcripcion sin etiquetar.

Usa la diarizacion de p1 (turnos de pyannote), asigna cada segmento de Whisper
al cluster con mas solape y traduce cluster -> persona con
`datos/voz/mapeo/<reunion>.json`. Si ese mapeo no existe lo crea con la
propuesta de wespeaker (normalizacion de p4) para que se revise a mano; si
existe, lo respeta: asi las correcciones se aplican re-ejecutando.

    .venv\Scripts\python.exe experimentos\voz\p5_asignar.py 20260917_090629
"""
import json
import re
import shutil
import sys
from collections import defaultdict

import numpy as np

import comun
import p2_etiquetar
import p4_comparar as p4

sys.stdout.reconfigure(encoding="utf-8")
UMBRAL = 0.60


def proponer(reunion):
    frag = [json.loads(l) for l in (comun.DATOS_VOZ / "fragmentos.jsonl").open(encoding="utf-8")]
    X = p4.normalizar(np.load(comun.DATOS_VOZ / "embeddings.npz")["wespeaker"], frag)
    et = [p4.etiqueta(f) for f in frag]
    lab = [i for i, e in enumerate(et) if e and frag[i]["reunion"] != reunion]
    cen = p4.centroides(X, lab, et)
    nombres = list(cen)
    M = np.stack([cen[n] for n in nombres])
    por_cluster = defaultdict(list)
    for i, f in enumerate(frag):
        if f["reunion"] == reunion:
            por_cluster[f["cluster"]].append(i)
    mapeo = {}
    for cl, ids in sorted(por_cluster.items()):
        v = p4.l2(X[ids].mean(axis=0))
        s = M @ v
        orden = np.argsort(-s)
        mapeo[cl] = {
            "nombre": nombres[orden[0]] if s[orden[0]] >= UMBRAL else None,
            "propuesta": nombres[orden[0]], "similitud": round(float(s[orden[0]]), 3),
            "segunda": nombres[orden[1]], "similitud_segunda": round(float(s[orden[1]]), 3),
            "revisado": False,
        }
    return mapeo


def main():
    reunion = sys.argv[1]
    ruta_mapeo = comun.DATOS_VOZ / "mapeo" / f"{reunion}.json"
    ruta_mapeo.parent.mkdir(parents=True, exist_ok=True)
    if not ruta_mapeo.exists():
        ruta_mapeo.write_text(json.dumps(proponer(reunion), ensure_ascii=False, indent=1), encoding="utf-8")
    mapeo = json.loads(ruta_mapeo.read_text(encoding="utf-8"))

    diar = json.loads((comun.DATOS_VOZ / "diar" / f"{reunion}.json").read_text(encoding="utf-8"))
    srt = comun.GRABACIONES / f"{reunion}_mixed.srt"
    original = srt.with_suffix(".sin_hablantes.srt")
    if not original.exists():
        shutil.copy2(srt, original)  # se conserva el .srt previo (con sus etiquetas)
    segmentos = p2_etiquetar.leer_srt(original)

    lineas_srt, lineas_txt, muestras = [], [], defaultdict(list)
    mapa_bd = p2_etiquetar.mapeo_bd(reunion)
    for n, (ini, fin, et_original, texto) in enumerate(segmentos, 1):
        previo = mapa_bd.get(et_original, et_original)
        if previo and (previo.startswith("SPEAKER_") or previo == "?"):
            previo = None
        solape = defaultdict(float)
        for t_ini, t_fin, cl in diar["turnos"]:
            s = min(fin, t_fin) - max(ini, t_ini)
            if s > 0:
                solape[cl] += s
        cl = max(solape, key=solape.get) if solape else None
        info = mapeo.get(cl) or {}
        nombre = info.get("nombre") or cl or previo
        if info.get("mezclado") and previo:
            nombre = previo
        # Sin hablante la linea va sin etiqueta, como en transcribe_teams.py
        linea = f"[{nombre}] {texto}" if nombre else texto
        ts = lambda t: f"{int(t//3600):02}:{int(t%3600//60):02}:{int(t%60):02},{int(round(t%1*1000)):03}"
        lineas_srt.append(f"{n}\n{ts(ini)} --> {ts(fin)}\n{linea}\n")
        lineas_txt.append(linea)
        if cl and len(texto) > 40:
            muestras[cl].append(f"{ts(ini)[:8]}  {texto}")

    srt.write_text("\n".join(lineas_srt), encoding="utf-8")
    srt.with_suffix(".txt").write_text("\n".join(lineas_txt) + "\n", encoding="utf-8")

    for cl, m in sorted(mapeo.items()):
        voz = f"[{m['propuesta']} {m['similitud']:.2f} | {m['segunda']} {m['similitud_segunda']:.2f}]" if "propuesta" in m else ""
        print(f"\n### {cl} -> {m['nombre'] or '(sin asignar)'}{' (mezclado)' if m.get('mezclado') else ''}   {voz}")
        ejemplos = muestras.get(cl, [])
        paso = max(1, len(ejemplos) // 3)
        for e in ejemplos[::paso][:3]:
            print(f"    {e[:150]}")
    print(f"\nMapeo editable: {ruta_mapeo}")


if __name__ == "__main__":
    main()
