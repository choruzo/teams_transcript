"""Paso 3: trocea cada turno en fragmentos de 1-4 s y calcula el embedding de
cada fragmento con wespeaker (el de pyannote, 256 d) y ECAPA-TDNN de
SpeechBrain (192 d). Sale `datos/voz/embeddings.npz` alineado con
`datos/voz/fragmentos.jsonl`.
"""
import json
import sys
import time

import numpy as np

import comun

comun.configurar_hf()
sys.stdout.reconfigure(encoding="utf-8")

import torch  # noqa: E402

RATE = 16000
MAX_FRAG, MIN_FRAG = 4.0, 1.0
LOTE = 64


def trocear(ini, fin):
    n = max(1, int((fin - ini) // MAX_FRAG) + (1 if (fin - ini) % MAX_FRAG >= MIN_FRAG else 0))
    paso = (fin - ini) / n
    return [(ini + i * paso, ini + (i + 1) * paso) for i in range(n) if paso >= MIN_FRAG]


def cargar_wespeaker():
    from pyannote.audio import Model

    m = Model.from_pretrained("pyannote/wespeaker-voxceleb-resnet34-LM", token=comun.configurar_hf())
    m.eval().to("cuda")
    return lambda x, lens: m(x.unsqueeze(1))  # (B, 1, T) -> (B, 256)


def cargar_ecapa():
    from speechbrain.inference.speaker import EncoderClassifier
    from speechbrain.utils.fetching import LocalStrategy

    m = EncoderClassifier.from_hparams(
        source="speechbrain/spkrec-ecapa-voxceleb",
        savedir=str(comun.DATOS_VOZ / "modelos" / "spkrec-ecapa-voxceleb"),
        run_opts={"device": "cuda"},
        local_strategy=LocalStrategy.COPY,
    )
    return lambda x, lens: m.encode_batch(x, wav_lens=lens).squeeze(1)


def main():
    segmentos = [json.loads(l) for l in (comun.DATOS_VOZ / "segmentos.jsonl").open(encoding="utf-8")]
    fragmentos = []
    for s in segmentos:
        for a, b in trocear(s["inicio"], s["fin"]):
            fragmentos.append({**s, "inicio": round(a, 3), "fin": round(b, 3), "turno_inicio": s["inicio"]})

    audios = {r: torch.from_numpy(comun.cargar_mono(comun.GRABACIONES / f"{r}_mixed.wav", RATE)[0])
              for r in {f["reunion"] for f in fragmentos}}
    # Ordenar por duracion minimiza el relleno dentro de cada lote.
    orden = sorted(range(len(fragmentos)), key=lambda i: fragmentos[i]["fin"] - fragmentos[i]["inicio"])

    resultados = {}
    for nombre, cargar in [("wespeaker", cargar_wespeaker), ("ecapa", cargar_ecapa)]:
        t0 = time.time()
        modelo = cargar()
        salida = [None] * len(fragmentos)
        with torch.inference_mode():
            for k in range(0, len(orden), LOTE):
                idx = orden[k : k + LOTE]
                trozos = [audios[fragmentos[i]["reunion"]][int(fragmentos[i]["inicio"] * RATE) : int(fragmentos[i]["fin"] * RATE)] for i in idx]
                largo = max(len(t) for t in trozos)
                x = torch.zeros(len(trozos), largo)
                for j, t in enumerate(trozos):
                    x[j, : len(t)] = t
                lens = torch.tensor([len(t) / largo for t in trozos])
                if nombre == "wespeaker":
                    # wespeaker no admite longitudes relativas: se pasa cada
                    # fragmento por separado para no meter relleno en el embedding.
                    emb = torch.stack([modelo(t.unsqueeze(0).cuda(), None)[0] for t in trozos])
                else:
                    emb = modelo(x.cuda(), lens.cuda())
                for j, i in enumerate(idx):
                    salida[i] = emb[j].float().cpu().numpy()
        resultados[nombre] = np.stack(salida)
        print(f"{nombre}: {resultados[nombre].shape} en {time.time()-t0:.0f}s")
        del modelo
        torch.cuda.empty_cache()

    np.savez(comun.DATOS_VOZ / "embeddings.npz", **resultados)
    with (comun.DATOS_VOZ / "fragmentos.jsonl").open("w", encoding="utf-8") as f:
        for fr in fragmentos:
            f.write(json.dumps(fr, ensure_ascii=False) + "\n")
    print(f"{len(fragmentos)} fragmentos")


if __name__ == "__main__":
    main()
