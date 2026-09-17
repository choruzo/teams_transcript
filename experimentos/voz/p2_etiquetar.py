"""Paso 2: etiqueta cada turno exclusivo de pyannote con el nombre que le da
la transcripcion (.srt + mapeo SPEAKER_XX -> persona de meetings.db) y con la
energia relativa de mic.wav, que solo capta a Javi.

Sale `datos/voz/segmentos.jsonl`: una linea por turno, base del dataset.
"""
import json
import re
import sqlite3
import sys
from collections import Counter, defaultdict

import numpy as np

import comun

sys.stdout.reconfigure(encoding="utf-8")
MIN_DUR = 1.0
RE_TS = re.compile(r"(\d+):(\d+):(\d+),(\d+) --> (\d+):(\d+):(\d+),(\d+)")


def seg(h, m, s, ms):
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000


def leer_srt(path):
    bloques = []
    for bloque in path.read_text(encoding="utf-8").split("\n\n"):
        lineas = bloque.strip().splitlines()
        if len(lineas) < 3 or not (m := RE_TS.match(lineas[1])):
            continue
        texto = " ".join(lineas[2:])
        et = re.match(r"\[([^\]]+)\]\s*(.*)", texto)
        g = m.groups()
        bloques.append((seg(*g[:4]), seg(*g[4:]), et.group(1) if et else None, et.group(2) if et else texto))
    return bloques


def mapeo_bd(reunion):
    fecha = f"{reunion[:4]}-{reunion[4:6]}-{reunion[6:8]}"
    con = sqlite3.connect(comun.RAIZ / "datos" / "meetings.db")
    filas = con.execute(
        "select ms.etiqueta, p.nombre from meetings m join meeting_speakers ms on ms.meeting_id=m.id "
        "join personas p on p.id=ms.persona_id where m.fecha=? and m.audio_path like ?",
        (fecha, f"%{reunion}%"),
    ).fetchall()
    return dict(filas)


def rms_por_ventana(x, rate, ini, fin):
    a, b = int(ini * rate), int(fin * rate)
    trozo = x[a:b]
    return float(np.sqrt(np.mean(trozo**2))) if len(trozo) else 0.0


def main():
    salida = comun.DATOS_VOZ / "segmentos.jsonl"
    total = []
    for r in comun.reuniones():
        diar = json.loads((comun.DATOS_VOZ / "diar" / f"{r}.json").read_text(encoding="utf-8"))
        srt_path = comun.GRABACIONES / f"{r}_mixed.srt"
        srt = leer_srt(srt_path) if srt_path.exists() else []
        mapa = mapeo_bd(r)
        ruta_rev = comun.DATOS_VOZ / "mapeo" / f"{r}.json"
        revisado = {}
        if ruta_rev.exists():
            revisado = {cl: v["nombre"] for cl, v in json.loads(ruta_rev.read_text(encoding="utf-8")).items()
                        if v.get("revisado") and not v.get("mezclado")}
        mic, rmic = comun.cargar_mono(comun.GRABACIONES / f"{r}_mic.wav")
        mix, rmix = comun.cargar_mono(comun.GRABACIONES / f"{r}_mixed.wav")

        tabla = defaultdict(Counter)
        for ini, fin, cluster in diar["exclusivos"]:
            if fin - ini < MIN_DUR:
                continue
            votos = Counter()
            for s_ini, s_fin, et, _ in srt:
                solape = min(fin, s_fin) - max(ini, s_ini)
                if solape > 0 and et:
                    votos[mapa.get(et, et)] += solape
            nombre, pureza = None, 0.0
            if cluster in revisado:
                # Mapeo cluster -> persona validado a mano: etiqueta fuerte.
                nombre, pureza = revisado.get(cluster), 1.0
            elif votos:
                nombre, peso = votos.most_common(1)[0]
                pureza = peso / sum(votos.values())
                if nombre.startswith("SPEAKER_") or "/" in nombre or nombre == "equipo":
                    nombre = None
            m_rms = rms_por_ventana(mic, rmic, ini, fin)
            x_rms = rms_por_ventana(mix, rmix, ini, fin)
            fila = {
                "reunion": r, "inicio": round(ini, 3), "fin": round(fin, 3), "cluster": cluster,
                "nombre_transcripcion": nombre, "pureza": round(pureza, 3),
                "mic_ratio": round(m_rms / x_rms, 3) if x_rms > 0 else None,
            }
            total.append(fila)
            tabla[cluster][nombre if pureza >= 0.7 else "?"] += fin - ini

        print(f"\n== {r}  (srt: {'si' if srt else 'no'}, mapeo BD: {len(mapa)})")
        for cluster in sorted(tabla):
            filas = [f for f in total if f["reunion"] == r and f["cluster"] == cluster]
            mic_med = np.median([f["mic_ratio"] for f in filas if f["mic_ratio"] is not None])
            reparto = ", ".join(f"{n}:{s:.0f}s" for n, s in tabla[cluster].most_common(4))
            print(f"  {cluster}  mic_ratio~{mic_med:.2f}  {reparto}")

    with salida.open("w", encoding="utf-8") as f:
        for fila in total:
            f.write(json.dumps(fila, ensure_ascii=False) + "\n")
    print(f"\n{len(total)} turnos >= {MIN_DUR}s -> {salida}")


if __name__ == "__main__":
    main()
