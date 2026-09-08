"""
transcribe_teams.py

Transcribe un archivo de audio (por defecto el "_mixed.wav" generado por
record_teams.py) usando Whisper (openai-whisper) en local.

Genera junto al audio:
  <nombre>.txt   -> texto plano de la transcripcion
  <nombre>.srt   -> subtitulos con marcas de tiempo

Con --diarize, ademas identifica quien habla en cada segmento usando
pyannote.audio (diarizacion de hablantes) y antepone una etiqueta
[SPEAKER_00], [SPEAKER_01], etc. a cada linea.

Uso:
    python transcribe_teams.py grabaciones\20260907_090437_mixed.wav
    python transcribe_teams.py grabaciones\20260907_090437_mixed.wav --model medium
    python transcribe_teams.py grabaciones\20260907_090437_mixed.wav --language es --device cuda
    python transcribe_teams.py grabaciones\20260907_090437_mixed.wav --diarize --hf-token hf_xxx

Notas:
  - Modelos disponibles (de menor a mayor precision/coste): tiny, base, small,
    medium, large. En CPU, usa "small" o inferior si quieres resultados
    razonablemente rapidos; en GPU (CUDA) puedes usar "medium"/"large" sin
    problema.
  - Si existe `datos/glosario.md` se usa automaticamente para sesgar el
    reconocimiento hacia las siglas y nombres del proyecto (initial_prompt de
    Whisper). Usa --glosario RUTA para otro fichero o --sin-glosario para
    desactivarlo. Ver datos/glosario.ejemplo.md.
  - Si no se indica --device, se detecta automaticamente si hay GPU CUDA
    disponible (torch.cuda.is_available()) y si no, usa CPU.
  - --diarize requiere el paquete `pyannote.audio` (no incluido por defecto,
    ver requirements.txt) y un token de acceso de HuggingFace con las
    condiciones de "pyannote/speaker-diarization-3.1" y
    "pyannote/segmentation-3.0" aceptadas. Pasa el token con --hf-token o la
    variable de entorno HF_TOKEN.
"""

import argparse
import os
import sys
import wave
from pathlib import Path

import numpy as np

import glosario as glosario_mod

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


def _segment_label(seg: dict) -> str:
    text = seg["text"].strip()
    speaker = seg.get("speaker")
    return f"[{speaker}] {text}" if speaker else text


def write_srt(segments, path: Path) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for i, seg in enumerate(segments, start=1):
            f.write(f"{i}\n")
            f.write(f"{format_timestamp(seg['start'])} --> {format_timestamp(seg['end'])}\n")
            f.write(_segment_label(seg) + "\n\n")


def write_txt(segments, path: Path) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for seg in segments:
            f.write(_segment_label(seg) + "\n")


def colapsar_repeticiones(segments, maximo: int = 2, ventana: int = 10) -> int:
    """Elimina bucles de repeticion de Whisper. Modifica la lista en sitio y
    devuelve cuantos segmentos se han eliminado.

    Whisper entra en bucle sobre silencio o ruido (tipicamente al final del
    audio), y `carry_initial_prompt` lo realimenta. Se distinguen dos casos
    para no destruir repeticiones legitimas:

    - Textos cortos ("Vale.", "Gracias."): son normales en una conversacion,
      asi que solo se recortan si aparecen mas de `maximo` veces SEGUIDAS.
    - Frases largas (4 palabras o mas): que se repitan identicas dentro de una
      ventana de `ventana` segmentos no pasa en una conversacion real, asi que
      basta una repeticion cercana para considerarlo artefacto. Se mira la
      ventana y no solo el segmento anterior porque el bucle suele venir
      salpicado de lineas sueltas que rompen la racha.
    """
    salida = []
    anterior = None
    seguidas = 0
    for seg in segments:
        clave = seg["text"].strip().lower()
        largo = len(clave.split()) >= 4

        if largo:
            recientes = [s["text"].strip().lower() for s in salida[-ventana:]]
            if clave in recientes:
                continue
        else:
            seguidas = seguidas + 1 if clave == anterior else 1
            if seguidas > maximo:
                anterior = clave
                continue

        anterior = clave
        salida.append(seg)

    eliminados = len(segments) - len(salida)
    segments[:] = salida
    return eliminados


def _load_waveform(path: Path):
    """Carga un .wav PCM 16-bit con el modulo estandar `wave` y lo devuelve
    como tensor mono (1, num_samples) + sample rate. Se usa en vez de dejar
    que pyannote.audio decodifique el fichero via torchcodec/FFmpeg, ya que
    torchcodec requiere DLLs compartidas de FFmpeg que no vienen instaladas
    por defecto en Windows."""
    import torch

    with wave.open(str(path), "rb") as wf:
        rate = wf.getframerate()
        channels = wf.getnchannels()
        sampwidth = wf.getsampwidth()
        frames = wf.readframes(wf.getnframes())

    if sampwidth != 2:
        raise RuntimeError(
            f"Formato de audio no soportado (sampwidth={sampwidth} bytes); "
            "se espera PCM 16-bit, como el que genera record_teams.py"
        )

    data = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
    if channels > 1:
        data = data.reshape(-1, channels).mean(axis=1)

    waveform = torch.from_numpy(data).unsqueeze(0)
    return waveform, rate


def diarize_audio(
    audio_path: Path, device: str, hf_token: str, num_speakers: int | None
) -> list[tuple[float, float, str]]:
    """Ejecuta pyannote.audio sobre el audio y devuelve una lista de turnos
    (start, end, speaker) ordenados por tiempo de inicio."""
    from pyannote.audio import Pipeline

    print("Cargando modelo de diarizacion 'pyannote/speaker-diarization-3.1'...")
    pipeline = Pipeline.from_pretrained(
        "pyannote/speaker-diarization-3.1", token=hf_token
    )
    if device == "cuda":
        import torch

        pipeline.to(torch.device("cuda"))

    print(f"Diarizando '{audio_path.name}'... (puede tardar varios minutos en CPU)")
    waveform, sample_rate = _load_waveform(audio_path)
    kwargs = {"num_speakers": num_speakers} if num_speakers else {}
    output = pipeline({"waveform": waveform, "sample_rate": sample_rate}, **kwargs)
    # pyannote.audio >= 4 devuelve un DiarizeOutput; versiones anteriores
    # devuelven directamente el Annotation con itertracks().
    annotation = getattr(output, "speaker_diarization", output)

    turns = [
        (segment.start, segment.end, speaker)
        for segment, _, speaker in annotation.itertracks(yield_label=True)
    ]
    turns.sort(key=lambda t: t[0])
    return turns


def assign_speakers(segments, turns: list[tuple[float, float, str]]) -> None:
    """Asigna a cada segmento de Whisper el hablante de pyannote con mayor
    solapamiento temporal. Modifica los segmentos en sitio."""
    for seg in segments:
        best_speaker = None
        best_overlap = 0.0
        for t_start, t_end, speaker in turns:
            overlap = min(seg["end"], t_end) - max(seg["start"], t_start)
            if overlap > best_overlap:
                best_overlap = overlap
                best_speaker = speaker
        seg["speaker"] = best_speaker


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
    parser.add_argument(
        "--diarize",
        action="store_true",
        help="Identifica hablantes con pyannote.audio y etiqueta cada linea (requiere --hf-token o HF_TOKEN)",
    )
    parser.add_argument(
        "--hf-token",
        default=None,
        help="Token de acceso de HuggingFace para descargar el modelo de pyannote (o usa la variable de entorno HF_TOKEN)",
    )
    parser.add_argument(
        "--glosario",
        default=None,
        help="Ruta al glosario del proyecto, para sesgar el reconocimiento de "
        "siglas y nombres propios (por defecto: datos/glosario.md si existe)",
    )
    parser.add_argument(
        "--sin-glosario",
        action="store_true",
        help="No usar el glosario aunque exista datos/glosario.md",
    )
    parser.add_argument(
        "--sin-carry",
        action="store_true",
        help="No reinyectar el glosario en cada ventana de 30s de Whisper "
        "(por defecto se reinyecta; sin esto, el glosario solo afecta al "
        "primer fragmento del audio y su efecto se diluye)",
    )
    parser.add_argument(
        "--sin-limpieza",
        action="store_true",
        help="No eliminar los bucles de repeticion de Whisper (segmentos "
        "identicos consecutivos repetidos mas de dos veces)",
    )
    parser.add_argument(
        "--num-speakers",
        type=int,
        default=None,
        help="Numero exacto de hablantes, si se conoce (mejora la precision de la diarizacion)",
    )
    args = parser.parse_args()

    audio_path = Path(args.audio)
    if not audio_path.exists():
        print(f"Error: no se encuentra el archivo '{audio_path}'", file=sys.stderr)
        sys.exit(1)

    hf_token = args.hf_token or os.environ.get("HF_TOKEN")
    hf_offline = os.environ.get("HF_HUB_OFFLINE") in ("1", "true", "True", "yes")
    if args.diarize and not hf_token and not hf_offline:
        print(
            "Error: --diarize requiere un token de HuggingFace. Pasa --hf-token "
            "o define la variable de entorno HF_TOKEN. Ademas debes aceptar las "
            "condiciones de 'pyannote/speaker-diarization-3.1', "
            "'pyannote/segmentation-3.0' y 'pyannote/speaker-diarization-community-1' "
            "en huggingface.co con esa cuenta. Si vas a ejecutar en un equipo sin "
            "acceso a huggingface.co con los modelos ya cacheados, define "
            "HF_HUB_OFFLINE=1 en su lugar (ver DEPLOY_OFFLINE.md).",
            file=sys.stderr,
        )
        sys.exit(1)

    # El glosario es opcional: si no se pasa --glosario y no existe el fichero
    # por defecto, se transcribe sin initial_prompt (comportamiento anterior).
    # Pero si se pide uno explicitamente y no esta, es un error del usuario.
    initial_prompt = None
    if not args.sin_glosario:
        if args.glosario and not Path(args.glosario).exists():
            print(
                f"Error: no se encuentra el glosario '{args.glosario}'",
                file=sys.stderr,
            )
            sys.exit(1)
        contenido = glosario_mod.cargar(args.glosario)
        if contenido:
            initial_prompt = glosario_mod.prompt_whisper(contenido)

    import whisper

    device = detect_device(args.device)
    language = None if args.language == "auto" else args.language

    print(f"Modelo:     {args.model}")
    print(f"Dispositivo: {device}")
    print(f"Idioma:     {language or 'autodetectar'}")
    # Whisper solo inyecta `initial_prompt` en la PRIMERA ventana de 30s; a
    # partir de ahi el contexto de cada ventana es el texto ya transcrito
    # (condition_on_previous_text), asi que el glosario se diluye y solo
    # corrige a ratos. `carry_initial_prompt` lo reinyecta en cada ventana.
    # `carry_initial_prompt` existe desde openai-whisper 20240930; el servidor
    # offline puede tener una version anterior, en cuyo caso pasarlo seria un
    # TypeError. Se detecta en vez de asumirlo.
    import inspect

    from whisper.transcribe import transcribe as _whisper_transcribe

    carry_soportado = (
        "carry_initial_prompt" in inspect.signature(_whisper_transcribe).parameters
    )
    carry = bool(initial_prompt) and not args.sin_carry and carry_soportado
    if initial_prompt and not args.sin_carry and not carry_soportado:
        print(
            "Aviso: esta version de openai-whisper no soporta "
            "carry_initial_prompt (requiere 20240930 o posterior). El glosario "
            "solo afectara al primer fragmento del audio.",
            file=sys.stderr,
        )
    if carry:
        estado_glosario = "si (reinyectado en cada ventana)"
    elif initial_prompt:
        estado_glosario = "si (solo primera ventana)"
    else:
        estado_glosario = "no"
    print(f"Glosario:   {estado_glosario}")
    print(f"Cargando modelo Whisper '{args.model}' (puede tardar la primera vez, se descarga)...")

    model = whisper.load_model(args.model, device=device)

    print(f"Transcribiendo '{audio_path.name}'... (puede tardar varios minutos en CPU)")
    extra = {"carry_initial_prompt": True} if carry else {}
    result = model.transcribe(
        str(audio_path),
        language=language,
        verbose=False,
        initial_prompt=initial_prompt,
        **extra,
    )

    if not args.sin_limpieza:
        eliminados = colapsar_repeticiones(result["segments"])
        if eliminados:
            print(
                f"Limpieza: {eliminados} segmento(s) eliminado(s) por "
                f"repeticion en bucle (usa --sin-limpieza para conservarlos)."
            )

    out_dir = Path(args.output_dir) if args.output_dir else audio_path.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = audio_path.stem

    txt_path = out_dir / f"{stem}.txt"
    srt_path = out_dir / f"{stem}.srt"

    # Se guarda la transcripcion antes de diarizar: si la diarizacion falla
    # (fallo de red, token, modelo, etc.) no se pierde el trabajo de Whisper,
    # que en CPU puede haber tardado varios minutos.
    write_txt(result["segments"], txt_path)
    write_srt(result["segments"], srt_path)

    if args.diarize:
        try:
            turns = diarize_audio(audio_path, device, hf_token, args.num_speakers)
            assign_speakers(result["segments"], turns)
            write_txt(result["segments"], txt_path)
            write_srt(result["segments"], srt_path)
        except Exception as exc:
            print(
                f"\nAviso: la diarizacion fallo ({exc}). Se conserva la "
                f"transcripcion sin etiquetas de hablante.",
                file=sys.stderr,
            )
            args.diarize = False

    print(f"\nGuardado:\n  {txt_path}\n  {srt_path}")
    print("\n--- Transcripcion ---\n")
    if args.diarize:
        for seg in result["segments"]:
            print(_segment_label(seg))
    else:
        print(result["text"].strip())


if __name__ == "__main__":
    main()
