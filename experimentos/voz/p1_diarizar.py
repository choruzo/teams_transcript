"""Paso 1: diariza cada mixed.wav con pyannote en GPU y guarda los turnos
(normales y exclusivos, sin solapes) y los embeddings de cluster.

    .venv\Scripts\python.exe experimentos\voz\p1_diarizar.py [--forzar]
"""
import argparse
import json
import time

import comun

comun.configurar_hf()

import torch  # noqa: E402
from pyannote.audio import Pipeline  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--forzar", action="store_true")
    args = ap.parse_args()
    destino = comun.DATOS_VOZ / "diar"
    destino.mkdir(parents=True, exist_ok=True)

    pipeline = Pipeline.from_pretrained("pyannote/speaker-diarization-3.1", token=comun.configurar_hf())
    pipeline.to(torch.device("cuda"))

    for r in comun.reuniones():
        salida = destino / f"{r}.json"
        if salida.exists() and not args.forzar:
            print(f"{r}: ya diarizada")
            continue
        t0 = time.time()
        x, rate = comun.cargar_mono(comun.GRABACIONES / f"{r}_mixed.wav", 16000)
        out = pipeline({"waveform": torch.from_numpy(x).unsqueeze(0), "sample_rate": rate})
        ann = out.speaker_diarization
        exc = getattr(out, "exclusive_speaker_diarization", ann)
        etiquetas = ann.labels()
        emb = getattr(out, "speaker_embeddings", None)
        datos = {
            "reunion": r,
            "turnos": [[s.start, s.end, l] for s, _, l in ann.itertracks(yield_label=True)],
            "exclusivos": [[s.start, s.end, l] for s, _, l in exc.itertracks(yield_label=True)],
            "embeddings_cluster": {l: [float(v) for v in emb[i]] for i, l in enumerate(etiquetas)} if emb is not None else {},
        }
        salida.write_text(json.dumps(datos), encoding="utf-8")
        print(f"{r}: {len(etiquetas)} hablantes, {len(datos['exclusivos'])} turnos, {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
