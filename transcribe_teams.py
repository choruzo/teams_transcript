"""
transcribe_teams.py

Transcribe un archivo de audio (por defecto el "_mixed.wav" generado por
record_teams.py) usando Whisper (openai-whisper) en local.

Genera junto al audio:
  <nombre>.txt   -> texto plano de la transcripcion
  <nombre>.srt   -> subtitulos con marcas de tiempo

Uso:
    python transcribe_teams.py grabaciones\20260907_090437_mixed.wav
    python transcribe_teams.py grabaciones\20260907_090437_mixed.wav --model medium
    python transcribe_teams.py grabaciones\20260907_090437_mixed.wav --language es --device cuda

Notas:
  - Modelos disponibles (de menor a mayor precision/coste): tiny, base, small,
    medium, large. En CPU, usa "small" o inferior si quieres resultados
    razonablemente rapidos; en GPU (CUDA) puedes usar "medium"/"large" sin
    problema.
  - Si no se indica --device, se detecta automaticamente si hay GPU CUDA
    disponible (torch.cuda.is_available()) y si no, usa CPU.
"""

import argparse
import sys
from pathlib import Path

# La consola de Windows suele usar cp1252/cp850, que no soporta todos los
# caracteres que puede producir Whisper (acentos, alfabetos no latinos, etc.).
# Forzamos UTF-8 en la salida estandar para evitar UnicodeEncodeError.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def format_timestamp(seconds: float) -> str:
    """Formato SRT: HH:MM:SS,mmm"""
    millis = round(seconds * 1000)
    hours, millis = divmod(millis, 3_600_000)
    minutes, millis = divmod(millis, 60_000)
    secs, millis = divmod(millis, 1_000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def write_srt(segments, path: Path) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for i, seg in enumerate(segments, start=1):
            f.write(f"{i}\n")
            f.write(f"{format_timestamp(seg['start'])} --> {format_timestamp(seg['end'])}\n")
            f.write(seg["text"].strip() + "\n\n")


def write_txt(segments, path: Path) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for seg in segments:
            f.write(seg["text"].strip() + "\n")


def detect_device(requested: str | None) -> str:
    if requested:
        return requested
    try:
        import torch

        return "cuda" if torch.cuda.is_available() else "cpu"
    except ImportError:
        return "cpu"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Transcribe un audio de una reunion de Teams usando Whisper."
    )
    parser.add_argument("audio", help="Ruta al archivo de audio (.wav/.mp3/...) a transcribir")
    parser.add_argument(
        "--model",
        default="small",
        choices=["tiny", "base", "small", "medium", "large", "large-v2", "large-v3"],
        help="Tamano del modelo Whisper (por defecto: small)",
    )
    parser.add_argument(
        "--language",
        default="es",
        help="Idioma del audio (codigo ISO, ej. 'es', 'en'). Usa 'auto' para autodetectar.",
    )
    parser.add_argument(
        "--device",
        default=None,
        choices=["cpu", "cuda"],
        help="Dispositivo a usar. Si no se indica, se autodetecta (GPU si esta disponible).",
    )
    parser.add_argument(
        "--output-dir",
        default=None,
        help="Carpeta de salida para el .txt/.srt (por defecto, la misma que el audio)",
    )
    args = parser.parse_args()

    audio_path = Path(args.audio)
    if not audio_path.exists():
        print(f"Error: no se encuentra el archivo '{audio_path}'", file=sys.stderr)
        sys.exit(1)

    import whisper

    device = detect_device(args.device)
    language = None if args.language == "auto" else args.language

    print(f"Modelo:     {args.model}")
    print(f"Dispositivo: {device}")
    print(f"Idioma:     {language or 'autodetectar'}")
    print(f"Cargando modelo Whisper '{args.model}' (puede tardar la primera vez, se descarga)...")

    model = whisper.load_model(args.model, device=device)

    print(f"Transcribiendo '{audio_path.name}'... (puede tardar varios minutos en CPU)")
    result = model.transcribe(str(audio_path), language=language, verbose=False)

    out_dir = Path(args.output_dir) if args.output_dir else audio_path.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = audio_path.stem

    txt_path = out_dir / f"{stem}.txt"
    srt_path = out_dir / f"{stem}.srt"

    write_txt(result["segments"], txt_path)
    write_srt(result["segments"], srt_path)

    print(f"\nGuardado:\n  {txt_path}\n  {srt_path}")
    print("\n--- Transcripcion ---\n")
    print(result["text"].strip())


if __name__ == "__main__":
    main()
