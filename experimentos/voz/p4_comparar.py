"""Paso 4: compara wespeaker y ECAPA con las etiquetas de las transcripciones.

Solo mide entre reuniones distintas: dentro de una misma reunion la
separacion ya la hizo pyannote y el resultado seria optimista. "Javi" sale de
mic.wav (etiqueta independiente del LLM); el resto, de la transcripcion.
"""
import json
import sys
from collections import Counter, defaultdict
from itertools import permutations

import numpy as np

import comun

sys.stdout.reconfigure(encoding="utf-8")
PUREZA_MIN = 0.7


def etiqueta(f):
    mic = f["mic_ratio"] or 0
    if mic > 0.5:
        return "Javi"
    if f["nombre_transcripcion"] == "Javi":
        return None  # la transcripcion dice Javi pero el micro no: se descarta
    return f["nombre_transcripcion"] if f["pureza"] >= PUREZA_MIN else None


def l2(x):
    return x / np.linalg.norm(x, axis=-1, keepdims=True)


def normalizar(emb, frag):
    """Centra con la media de las medias por hablante (reunion+cluster) y no
    con la media global: Pedro es ~40% de los fragmentos y la media global
    le resta su propia voz (medido: EER 12% -> 28% con wespeaker)."""
    u = l2(emb)
    grupos = defaultdict(list)
    for i, f in enumerate(frag):
        grupos[(f["reunion"], f["cluster"])].append(u[i])
    media = np.mean([np.mean(v, axis=0) for v in grupos.values()], axis=0)
    return l2(u - media)


def eer(sims, iguales):
    orden = np.argsort(-sims)
    iguales = iguales[orden]
    tp = np.cumsum(iguales) / iguales.sum()
    fp = np.cumsum(~iguales) / (~iguales).sum()
    i = np.argmin(np.abs((1 - tp) - fp))
    return (1 - tp[i] + fp[i]) / 2


def centroides(X, idx, etiquetas):
    grupos = defaultdict(list)
    for i in idx:
        grupos[etiquetas[i]].append(X[i])
    c = {n: np.mean(v, axis=0) for n, v in grupos.items()}
    return {n: v / np.linalg.norm(v) for n, v in c.items()}


def main():
    frag = [json.loads(l) for l in (comun.DATOS_VOZ / "fragmentos.jsonl").open(encoding="utf-8")]
    embs = np.load(comun.DATOS_VOZ / "embeddings.npz")
    et = [etiqueta(f) for f in frag]
    reun = np.array([f["reunion"] for f in frag])
    etiquetadas = [r for r in comun.reuniones() if any(e and e != "Javi" for e, rr in zip(et, reun) if rr == r)]
    sin_etiquetar = [r for r in comun.reuniones() if r not in etiquetadas]
    lab = np.array([i for i, e in enumerate(et) if e and reun[i] in etiquetadas])
    print("Fragmentos etiquetados por persona y reunion:")
    tabla = Counter((et[i], reun[i][:8]) for i in lab)
    for n in sorted({et[i] for i in lab}):
        print(f"  {n:<12}" + "  ".join(f"{r[:8]}:{tabla[(n, r[:8])]:>3}" for r in etiquetadas))

    informe = {}
    normalizados = {m: normalizar(embs[m], frag) for m in ["wespeaker", "ecapa"]}
    normalizados["fusion"] = l2(np.concatenate([normalizados["wespeaker"], normalizados["ecapa"]], axis=1))
    for modelo, X in normalizados.items():
        print(f"\n===== {modelo} =====")
        # a) EER sobre pares de fragmentos de reuniones distintas
        Xi, ri, ei = X[lab], reun[lab], np.array([et[i] for i in lab])
        S = Xi @ Xi.T
        mask = ri[:, None] != ri[None, :]
        sims, iguales = S[mask], (ei[:, None] == ei[None, :])[mask]
        e = eer(sims, iguales)
        print(f"EER fragmento-fragmento (entre reuniones): {e*100:.1f}%  "
              f"| misma persona {np.mean(sims[iguales]):.2f}  distinta {np.mean(sims[~iguales]):.2f}")

        # b) perfiles de una reunion -> identificar fragmentos y turnos de otra
        aciertos_f, aciertos_t = [], []
        for a, b in permutations(etiquetadas, 2):
            cen = centroides(X, [i for i in lab if reun[i] == a], et)
            idx_b = [i for i in lab if reun[i] == b and et[i] in cen]
            nombres = list(cen)
            M = np.stack([cen[n] for n in nombres])
            pred = [nombres[j] for j in np.argmax(X[idx_b] @ M.T, axis=1)]
            aciertos_f += [p == et[i] for p, i in zip(pred, idx_b)]
            turnos = defaultdict(list)
            for i in idx_b:
                turnos[(frag[i]["turno_inicio"])].append(i)
            for ids in turnos.values():
                v = X[ids].mean(axis=0)
                aciertos_t.append(nombres[int(np.argmax(M @ v))] == et[ids[0]])
        print(f"Identificacion con perfiles de otra reunion: fragmentos {np.mean(aciertos_f)*100:.1f}%  "
              f"turnos {np.mean(aciertos_t)*100:.1f}%  (n={len(aciertos_f)}/{len(aciertos_t)})")

        # c) nivel hablante (lo que usa perfiles_voz): centroide por persona y reunion
        mismos, distintos = [], []
        for a, b in permutations(etiquetadas, 2):
            ca = centroides(X, [i for i in lab if reun[i] == a], et)
            cb = centroides(X, [i for i in lab if reun[i] == b], et)
            for n1, v1 in ca.items():
                for n2, v2 in cb.items():
                    (mismos if n1 == n2 else distintos).append((float(v1 @ v2), n1, n2))
        m_min, d_max = min(mismos), max(distintos)
        print(f"Centroide por persona entre reuniones: misma {min(s for s,*_ in mismos):.2f}-{max(s for s,*_ in mismos):.2f}, "
              f"distinta {min(s for s,*_ in distintos):.2f}-{d_max[0]:.2f}  "
              f"margen {m_min[0]-d_max[0]:+.2f}  (peor misma: {m_min[1]}; peor distinta: {d_max[1]}~{d_max[2]})")

        # d) reuniones sin transcripcion: perfiles de todas las etiquetadas
        cen = centroides(X, list(lab), et)
        nombres = list(cen)
        M = np.stack([cen[n] for n in nombres])
        prediccion = {}
        for r in sin_etiquetar:
            print(f"Prediccion {r} (sin transcripcion):")
            por_cluster = defaultdict(list)
            for i, f in enumerate(frag):
                if f["reunion"] == r:
                    por_cluster[f["cluster"]].append(i)
            for cl, ids in sorted(por_cluster.items()):
                v = X[ids].mean(axis=0); v /= np.linalg.norm(v)
                s = M @ v
                top = np.argsort(-s)[:2]
                mic = np.median([frag[i]["mic_ratio"] or 0 for i in ids])
                prediccion[f"{r}/{cl}"] = [nombres[top[0]], float(s[top[0]]), nombres[top[1]], float(s[top[1]])]
                print(f"  {cl} ({len(ids):>3} frag, mic {mic:.2f}): {nombres[top[0]]} {s[top[0]]:.2f} | {nombres[top[1]]} {s[top[1]]:.2f}")
        informe[modelo] = {"eer": e, "acc_fragmento": float(np.mean(aciertos_f)), "acc_turno": float(np.mean(aciertos_t)),
                           "misma": [s for s, *_ in mismos], "distinta": [s for s, *_ in distintos], "prediccion": prediccion}
    (comun.DATOS_VOZ / "informe_comparacion.json").write_text(json.dumps(informe, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
