"""
record_teams.py

Graba simultaneamente el audio de una llamada/reunion de Teams:
  - el audio del sistema (lo que suena por los altavoces: la reunion, otros
    participantes) usando el modo loopback de WASAPI.
  - tu microfono por defecto (tu propia voz).

Genera 3 archivos .wav en la carpeta de salida, listos para transcribir despues:
  <timestamp>_system.wav   -> audio de la reunion (otros participantes)
  <timestamp>_mic.wav      -> tu voz
  <timestamp>_mixed.wav    -> mezcla de ambos (recomendado para transcribir)

Uso:
    python record_teams.py                    # graba hasta pulsar Ctrl+C
    python record_teams.py --output-dir salida
    python record_teams.py --duration 3600     # se detiene solo tras N segundos
    python record_teams.py --list-devices      # lista dispositivos y sale

AVISO LEGAL: graba una reunion/llamada solo si tienes el consentimiento de
todos los participantes, segun la normativa que te aplique.
"""

import argparse
import datetime
import queue
import threading
import time
import wave
from pathlib import Path

import numpy as np
import pyaudiowpatch as pyaudio

CHUNK = 1024
SAMPLE_WIDTH = 2  # bytes, paInt16


def list_devices(p: pyaudio.PyAudio) -> None:
    print("\n=== Dispositivos de audio disponibles ===")
    for i in range(p.get_device_count()):
        info = p.get_device_info_by_index(i)
        print(
            f"[{i}] {info['name']} "
            f"(in={info['maxInputChannels']}, out={info['maxOutputChannels']}, "
            f"rate={int(info['defaultSampleRate'])})"
        )


def get_default_loopback(p: pyaudio.PyAudio):
    """Devuelve el dispositivo loopback WASAPI asociado a los altavoces por defecto,
    es decir, el que permite "grabar" lo que suena en el sistema."""
    wasapi_info = p.get_host_api_info_by_type(pyaudio.paWASAPI)
    default_speakers = p.get_device_info_by_index(wasapi_info["defaultOutputDevice"])

    if not default_speakers.get("isLoopbackDevice", False):
        for loopback in p.get_loopback_device_info_generator():
            if default_speakers["name"] in loopback["name"]:
                return loopback
        raise RuntimeError(
            "No se encontro el dispositivo loopback para los altavoces por defecto. "
            "Ejecuta con --list-devices para revisar los dispositivos disponibles."
        )
    return default_speakers


def record_stream(
    p: pyaudio.PyAudio,
    device_info: dict,
    stop_event: threading.Event,
    out_queue: "queue.Queue[bytes]",
    channels: int,
    rate: int,
) -> None:
    """Graba usando modo callback (no bloqueante). Es necesario para el
    dispositivo loopback: un stream.read() bloqueante se queda colgado si en
    ese instante no esta sonando audio por los altavoces (silencio)."""

    def callback(in_data, frame_count, time_info, status):
        if stop_event.is_set():
            return (None, pyaudio.paComplete)
        out_queue.put(in_data)
        return (None, pyaudio.paContinue)

    stream = p.open(
        format=pyaudio.paInt16,
        channels=channels,
        rate=rate,
        frames_per_buffer=CHUNK,
        input=True,
        input_device_index=device_info["index"],
        stream_callback=callback,
    )
    stream.start_stream()
    try:
        while stream.is_active() and not stop_event.is_set():
            time.sleep(0.1)
    finally:
        stream.stop_stream()
        stream.close()


def drain_queue_to_wav(
    out_queue: "queue.Queue[bytes]", path: Path, channels: int, rate: int
) -> None:
    wf = wave.open(str(path), "wb")
    wf.setnchannels(channels)
    wf.setsampwidth(SAMPLE_WIDTH)
    wf.setframerate(rate)
    while not out_queue.empty():
        wf.writeframes(out_queue.get())
    wf.close()


def _read_wav_mono(path: Path):
    with wave.open(str(path), "rb") as wf:
        rate = wf.getframerate()
        channels = wf.getnchannels()
        frames = wf.readframes(wf.getnframes())
        data = np.frombuffer(frames, dtype=np.int16).astype(np.float32)
        if channels > 1:
            data = data.reshape(-1, channels).mean(axis=1)
        return data, rate


def _resample(data: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
    if src_rate == dst_rate or len(data) == 0:
        return data
    duration = len(data) / src_rate
    n_samples = int(duration * dst_rate)
    x_old = np.linspace(0, duration, num=len(data), endpoint=False)
    x_new = np.linspace(0, duration, num=n_samples, endpoint=False)
    return np.interp(x_new, x_old, data)


def mix_wavs(mic_path: Path, system_path: Path, mixed_path: Path) -> None:
    """Mezcla mic + sistema en un unico wav mono, resampleando a la frecuencia
    mas alta de las dos y normalizando para evitar clipping."""
    mic, mic_rate = _read_wav_mono(mic_path)
    sysa, sys_rate = _read_wav_mono(system_path)

    target_rate = max(mic_rate, sys_rate)
    mic = _resample(mic, mic_rate, target_rate)
    sysa = _resample(sysa, sys_rate, target_rate)

    n = max(len(mic), len(sysa))
    mic = np.pad(mic, (0, n - len(mic)))
    sysa = np.pad(sysa, (0, n - len(sysa)))

    mixed = mic + sysa
    max_val = np.max(np.abs(mixed)) if mixed.size else 0.0
    if max_val > 32767:
        mixed = mixed * (32767 / max_val)
    mixed = mixed.astype(np.int16)

    with wave.open(str(mixed_path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(SAMPLE_WIDTH)
        wf.setframerate(target_rate)
        wf.writeframes(mixed.tobytes())


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Graba el audio de una llamada/reunion de Teams (microfono + sistema)."
    )
    parser.add_argument(
        "--output-dir", default="grabaciones", help="Carpeta donde guardar los .wav"
    )
    parser.add_argument(
        "--list-devices", action="store_true", help="Lista los dispositivos disponibles y sale"
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=None,
        help="Duracion maxima en segundos (opcional). Sin este parametro, graba hasta Ctrl+C.",
    )
    args = parser.parse_args()

    p = pyaudio.PyAudio()

    if args.list_devices:
        list_devices(p)
        p.terminate()
        return

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")

    mic_info = p.get_default_input_device_info()
    speaker_loopback = get_default_loopback(p)

    print(f"Microfono:          {mic_info['name']}")
    print(f"Sistema (loopback): {speaker_loopback['name']}")
    print("\nAVISO: graba solo con el consentimiento de todos los participantes.")
    print("Grabando... pulsa Ctrl+C para detener.\n")

    stop_event = threading.Event()
    mic_queue: "queue.Queue[bytes]" = queue.Queue()
    sys_queue: "queue.Queue[bytes]" = queue.Queue()

    mic_channels = int(mic_info["maxInputChannels"]) or 1
    mic_rate = int(mic_info["defaultSampleRate"])
    sys_channels = int(speaker_loopback["maxInputChannels"]) or 2
    sys_rate = int(speaker_loopback["defaultSampleRate"])

    mic_thread = threading.Thread(
        target=record_stream, args=(p, mic_info, stop_event, mic_queue, mic_channels, mic_rate)
    )
    sys_thread = threading.Thread(
        target=record_stream,
        args=(p, speaker_loopback, stop_event, sys_queue, sys_channels, sys_rate),
    )

    mic_thread.start()
    sys_thread.start()

    start = time.monotonic()
    try:
        while True:
            if args.duration is not None and (time.monotonic() - start) >= args.duration:
                print("\nDuracion maxima alcanzada, deteniendo grabacion...")
                break
            time.sleep(0.2)
    except KeyboardInterrupt:
        print("\nDeteniendo grabacion...")
    finally:
        stop_event.set()
        mic_thread.join()
        sys_thread.join()
        p.terminate()

    mic_path = out_dir / f"{timestamp}_mic.wav"
    sys_path = out_dir / f"{timestamp}_system.wav"
    mixed_path = out_dir / f"{timestamp}_mixed.wav"

    drain_queue_to_wav(mic_queue, mic_path, mic_channels, mic_rate)
    drain_queue_to_wav(sys_queue, sys_path, sys_channels, sys_rate)
    mix_wavs(mic_path, sys_path, mixed_path)

    print(f"\nGuardado:\n  {mic_path}\n  {sys_path}\n  {mixed_path}")
    print("Ya puedes transcribir '_mixed.wav' con el motor que prefieras (ej. Whisper).")


if __name__ == "__main__":
    main()
