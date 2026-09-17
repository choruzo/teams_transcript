"""Utilidades compartidas del experimento de identificacion de voz.

Los datos que generan estos scripts son biometricos (RGPD): van a
`datos/voz/`, que no se versiona.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[2]
GRABACIONES = RAIZ / "grabaciones"
DATOS_VOZ = RAIZ / "datos" / "voz"
sys.path.insert(0, str(RAIZ))


def configurar_hf() -> str | None:
    """Usa el token guardado por `hf auth login` si no hay HF_TOKEN, para que
    pyannote tire de la cache local con los repos gated ya aceptados."""
    token = os.environ.get("HF_TOKEN")
    if not token:
        fichero = Path.home() / ".cache" / "huggingface" / "token"
        if fichero.exists():
            token = fichero.read_text(encoding="utf-8").strip()
            os.environ["HF_TOKEN"] = token
    return token


def reuniones() -> list[str]:
    """Prefijos `AAAAMMDD_HHMMSS` de las grabaciones con mixed.wav."""
    return sorted(p.name[: -len("_mixed.wav")] for p in GRABACIONES.glob("*_mixed.wav"))


def cargar_mono(path: Path, rate_destino: int | None = None):
    """wav PCM16 -> (numpy float32 mono, rate). Remuestrea si se pide."""
    import wave

    import numpy as np

    with wave.open(str(path), "rb") as wf:
        rate, canales = wf.getframerate(), wf.getnchannels()
        datos = np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16)
    x = datos.astype(np.float32) / 32768.0
    if canales > 1:
        x = x.reshape(-1, canales).mean(axis=1)
    if rate_destino and rate_destino != rate:
        import torch
        import torchaudio.functional as F

        x = F.resample(torch.from_numpy(x), rate, rate_destino).numpy()
        rate = rate_destino
    return x, rate
