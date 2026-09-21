"""Paso 8: vuelve a transcribir un tramo de audio y lo sustituye en el .srt.

Para cuando Whisper se atasca en un bucle ("Vale. Vale. Vale...") y se come
un turno: el tramo se transcribe aislado, sin arrastrar el texto anterior
(condition_on_previous_text=False, que es lo que realimenta el bucle), y sus
segmentos sustituyen a los del .srt base que empiezan dentro de
[--srt-desde, --srt-hasta). Ese rango va aparte del de audio porque en un
bucle los tiempos de Whisper se desplazan respecto al audio real.

El .srt base es `*_mixed.sin_hablantes.srt` (el que lee p5_asignar); antes de
tocarlo se guarda una copia con sufijo `.antes_p8`. Despues hay que volver a
ejecutar p5_asignar para poner nombres.

    .venv\\Scripts\\python.exe experimentos\\voz\\p8_retranscribir.py 20260921_090137 ^
        --audio 24:38 25:23.4 --srt-desde 24:31 --srt-hasta 24:44
"""
import argparse
import re
import shutil
import sys

import comun

sys.stdout.reconfigure(encoding="utf-8")
RATE = 16000
RE_TS = re.compile(r"(\d+):(\d+):(\d+),(\d+) --> (\d+):(\d+):(\d+),(\d+)")


def segundos(texto):
    """'24:38' o '24:38.5' o '1:02:03' -> segundos."""
    partes = [float(p) for p in texto.split(":")]
    total = 0.0
    for p in partes:
        total = total * 60 + p
    return total


def ts(t):
    ms = int(round(t * 1000))
    return f"{ms // 3600000:02}:{ms // 60000 % 60:02}:{ms // 1000 % 60:02},{ms % 1000:03}"


def leer_bloques(path):
    bloques = []
    for bloque in path.read_text(encoding="utf-8").split("\n\n"):
        lineas = bloque.strip().splitlines()
        if len(lineas) < 3 or not (m := RE_TS.match(lineas[1])):
            continue
        g = [int(x) for x in m.groups()]
        ini = g[0] * 3600 + g[1] * 60 + g[2] + g[3] / 1000
        fin = g[4] * 3600 + g[5] * 60 + g[6] + g[7] / 1000
        bloques.append((ini, fin, "\n".join(lineas[2:])))
    return bloques


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("reunion")
    ap.add_argument("--audio", nargs=2, required=True, metavar=("INICIO", "FIN"))
    ap.add_argument("--srt-desde", required=True)
    ap.add_argument("--srt-hasta", required=True)
    ap.add_argument("--model", default="large-v3")
    ap.add_argument("--simular", action="store_true", help="Enseña el cambio sin escribir")
    args = ap.parse_args()

    a_ini, a_fin = (segundos(x) for x in args.audio)
    s_ini, s_fin = segundos(args.srt_desde), segundos(args.srt_hasta)
    base = comun.GRABACIONES / f"{args.reunion}_mixed.sin_hablantes.srt"
    bloques = leer_bloques(base)
    quitar = [b for b in bloques if s_ini <= b[0] < s_fin]

    import torch
    import whisper

    import glosario

    contenido = glosario.cargar(None)
    prompt = glosario.prompt_whisper(contenido) if contenido else None
    x, _ = comun.cargar_mono(comun.GRABACIONES / f"{args.reunion}_mixed.wav", RATE)
    trozo = torch.from_numpy(x[int(a_ini * RATE) : int(a_fin * RATE)].copy())
    modelo = whisper.load_model(args.model, device="cuda")
    res = modelo.transcribe(trozo, language="es", initial_prompt=prompt,
                            condition_on_previous_text=False, verbose=None)
    nuevos = [(a_ini + s["start"], min(a_ini + s["end"], a_fin), s["text"].strip())
              for s in res["segments"] if s["text"].strip()]

    print("Se quitan:")
    for ini, _, texto in quitar:
        print(f"  {ts(ini)}  {texto}")
    print("Entran:")
    for ini, fin, texto in nuevos:
        print(f"  {ts(ini)} -> {ts(fin)}  {texto}")
    if args.simular:
        return

    shutil.copy2(base, base.with_suffix(".antes_p8.srt"))
    resultado = sorted([b for b in bloques if b not in quitar] + nuevos, key=lambda b: b[0])
    base.write_text(
        "\n".join(f"{n}\n{ts(i)} --> {ts(f)}\n{t}\n" for n, (i, f, t) in enumerate(resultado, 1)),
        encoding="utf-8",
    )
    print(f"\nEscrito {base.name} ({len(quitar)} segmentos fuera, {len(nuevos)} dentro). "
          f"Siguiente: p5_asignar.py {args.reunion}")


if __name__ == "__main__":
    main()
