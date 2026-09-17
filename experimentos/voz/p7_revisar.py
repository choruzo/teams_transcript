"""Paso 7: tabla de revision de una reunion ya transcrita, sin modificarla.

Por cada cluster de pyannote enseña lo que dice la transcripcion actual
(etiqueta debil), lo que dice la voz (perfiles construidos SIN esta reunion)
y frases con marca de tiempo para comprobarlo. Deja la propuesta en
datos/voz/mapeo/<reunion>.json con revisado=false.

    .venv\Scripts\python.exe experimentos\voz\p7_revisar.py 20260910_090324
"""
import json
import sys
from collections import Counter, defaultdict

import comun
import p2_etiquetar
import p6_perfiles
import perfiles_voz as pv

sys.stdout.reconfigure(encoding="utf-8")


def ts(t):
    return f"{int(t//60):02}:{int(t%60):02}"


def main():
    reunion = sys.argv[1]
    filas, descartes = p6_perfiles.muestras()
    perfiles = p6_perfiles.construir([f for f in filas if f["reunion"] != reunion])
    diar = json.loads((comun.DATOS_VOZ / "diar" / f"{reunion}.json").read_text(encoding="utf-8"))
    res = pv.clasificar(diar["embeddings_cluster"], perfiles)
    srt = p2_etiquetar.leer_srt(comun.GRABACIONES / f"{reunion}_mixed.srt")
    mapa = p2_etiquetar.mapeo_bd(reunion)

    habla = defaultdict(float)
    for a, b, cl in diar["exclusivos"]:
        habla[cl] += b - a
    texto_cluster = defaultdict(Counter)
    frases = defaultdict(list)
    for ini, fin, et, texto in srt:
        solape = defaultdict(float)
        for a, b, cl in diar["turnos"]:
            if (s := min(fin, b) - max(ini, a)) > 0:
                solape[cl] += s
        if not solape:
            continue
        cl = max(solape, key=solape.get)
        nombre = p6_perfiles.ALIAS.get(mapa.get(et, et), mapa.get(et, et))
        texto_cluster[cl][nombre] += fin - ini
        if len(texto) > 35:
            frases[cl].append((ini, nombre, texto))

    ruta = comun.DATOS_VOZ / "mapeo" / f"{reunion}.json"
    mapeo = json.loads(ruta.read_text(encoding="utf-8")) if ruta.exists() else {}
    for cl in sorted(diar["embeddings_cluster"]):
        voz = res[cl]
        trans = texto_cluster[cl].most_common(3)
        total = sum(texto_cluster[cl].values()) or 1
        propuesta = voz["nombre"] or (trans[0][0] if trans else None)
        if cl not in mapeo or not mapeo[cl].get("revisado"):
            mapeo[cl] = {"nombre": propuesta, "voz": voz["nombre"], "transcripcion": trans[0][0] if trans else None,
                         "revisado": False}
        coincide = "=" if voz["nombre"] and trans and voz["nombre"] == trans[0][0] else "≠"
        cand = ", ".join(f"{n} {s:.2f}" for n, s in voz["candidatos"][:2])
        reparto = ", ".join(f"{n} {v/total:.0%}" for n, v in trans)
        print(f"\n### {cl}  ({habla[cl]:.0f}s)  propuesta: {propuesta}  {coincide}")
        print(f"    voz: {cand}")
        print(f"    transcripcion: {reparto}")
        ej = frases[cl]
        for ini, nombre, texto in ej[:: max(1, len(ej) // 3)][:3]:
            print(f"    {ts(ini)} [{nombre}] {texto[:110]}")
    ruta.parent.mkdir(parents=True, exist_ok=True)
    ruta.write_text(json.dumps(mapeo, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
