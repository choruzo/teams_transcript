"""
transcribe_teams.py

Transcribe un archivo de audio (por defecto el "_mixed.wav" generado por
record_teams.py) usando Whisper (openai-whisper) en local.

Genera junto al audio:
  <nombre>.txt   -> texto plano de la transcripcion
  <nombre>.srt   -> subtitulos con marcas de tiempo

Con --diarize, ademas identifica quien habla en cada segmento usando
pyannote.audio (diarizacion de hablantes) y antepone una etiqueta
[SPEAKER_00], [SPEAKER_01], etc. a cada linea. Si hay perfiles de voz
guardados (ver perfiles_voz.py), esa etiqueta pasa a ser el nombre real de la
persona, el mismo en todas las reuniones.

Uso:
    python transcribe_teams.py grabaciones\20260907_090437_mixed.wav
    python transcribe_teams.py grabaciones\20260907_090437_mixed.wav --model medium
    python transcribe_teams.py grabaciones\20260907_090437_mixed.wav --language es --device cuda
    python transcribe_teams.py grabaciones\20260907_090437_mixed.wav --diarize --hf-token hf_xxx
    python transcribe_teams.py grabaciones\20260907_090437_mixed.wav --diarize --enroll

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
  - Los perfiles de voz (--enroll para darlos de alta, --dry-run-voz para
    calibrar el umbral, --sin-perfiles para ignorarlos) no requieren ninguna
    dependencia ni modelo adicional: los embeddings salen del mismo pipeline
    de pyannote. Son datos biometricos; ver el aviso de perfiles_voz.py.
"""

import argparse
import os
import sys
import wave
from pathlib import Path

import numpy as np

import glosario as glosario_mod
import perfiles_voz

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


MODELO_DIARIZACION = "pyannote/speaker-diarization-3.1"


def _acepta_return_embeddings(pipeline) -> bool:
    """True si esta version de pyannote pide los embeddings por argumento."""
    import inspect

    for metodo in (getattr(pipeline, "apply", None), pipeline.__call__):
        if metodo is None:
            continue
        try:
            if "return_embeddings" in inspect.signature(metodo).parameters:
                return True
        except (TypeError, ValueError):
            continue
    return False


def _desempaquetar_salida(output):
    """Normaliza lo que devuelve el pipeline de pyannote a
    (annotation, embeddings_crudos). La forma cambia entre versiones y entre
    pedir o no los embeddings:

      - 3.1 sin embeddings: un `Annotation`.
      - 3.1 con `return_embeddings=True`: una tupla `(Annotation, ndarray)`.
      - 4.x: un `DiarizeOutput` con `.speaker_diarization` y
        `.speaker_embeddings`, que trae siempre (verificado en 4.0.7).

    Se detecta en vez de asumirlo porque el servidor offline no tiene por que
    llevar la misma version que el portatil.
    """
    embeddings = None
    if isinstance(output, tuple):
        if len(output) == 2:
            output, embeddings = output
        else:
            output = output[0]
    annotation = getattr(output, "speaker_diarization", output)
    if embeddings is None:
        for atributo in ("speaker_embeddings", "embeddings", "centroids"):
            valor = getattr(output, atributo, None)
            if valor is not None:
                embeddings = valor
                break
    return annotation, embeddings


def _embeddings_por_etiqueta(annotation, crudos) -> dict[str, list[float]]:
    """Convierte la matriz (num_hablantes, dimension) de pyannote en un dict
    etiqueta -> lista de float. La fila i corresponde a `annotation.labels()[i]`.

    Los vectores con NaN se descartan aqui mismo: pyannote los devuelve para
    hablantes de los que no pudo extraer muestras suficientes, y no son un
    fallo del pipeline sino un hablante del que no sabemos nada.
    """
    if crudos is None:
        return {}
    matriz = np.asarray(crudos, dtype=np.float64)
    if matriz.ndim != 2:
        return {}
    etiquetas = list(annotation.labels())
    if matriz.shape[0] != len(etiquetas):
        # Sin correspondencia fila-etiqueta, asignar por posicion seria
        # inventarse quien es quien; mejor quedarse sin perfiles.
        print(
            f"Aviso: pyannote devolvio {matriz.shape[0]} embedding(s) para "
            f"{len(etiquetas)} hablante(s); no se usan los perfiles de voz.",
            file=sys.stderr,
        )
        return {}

    salida: dict[str, list[float]] = {}
    for etiqueta, fila in zip(etiquetas, matriz):
        vector = [float(x) for x in fila]
        if perfiles_voz.es_vector_valido(vector):
            salida[etiqueta] = vector
    return salida


def diarize_audio(
    audio_path: Path,
    device: str,
    hf_token: str,
    num_speakers: int | None,
    con_embeddings: bool = False,
) -> tuple[list[tuple[float, float, str]], dict[str, list[float]]]:
    """Ejecuta pyannote.audio sobre el audio y devuelve una lista de turnos
    (start, end, speaker) ordenados por tiempo de inicio, junto con el
    embedding de voz de cada hablante si se piden (dict vacio si no).

    Los embeddings salen del mismo pipeline (`return_embeddings=True`), asi
    que la Fase 3 no añade ninguna dependencia ni ningun modelo nuevo: el
    `wespeaker-voxceleb-resnet34-LM` que los produce ya esta en la cache.
    """
    from pyannote.audio import Pipeline

    print(f"Cargando modelo de diarizacion '{MODELO_DIARIZACION}'...")
    pipeline = Pipeline.from_pretrained(MODELO_DIARIZACION, token=hf_token)
    if device == "cuda":
        import torch

        pipeline.to(torch.device("cuda"))

    print(f"Diarizando '{audio_path.name}'... (puede tardar varios minutos en CPU)")
    waveform, sample_rate = _load_waveform(audio_path)
    kwargs = {"num_speakers": num_speakers} if num_speakers else {}
    entrada = {"waveform": waveform, "sample_rate": sample_rate}

    # pyannote 3.x pide los embeddings con `return_embeddings=True` y devuelve
    # una tupla; 4.x los trae siempre en `output.speaker_embeddings` y ese
    # argumento ya no existe (pasarlo solo suelta un "Ignoring unexpected
    # keyword arguments"). Se mira la firma en vez de asumir la version,
    # igual que con `carry_initial_prompt`.
    if con_embeddings and _acepta_return_embeddings(pipeline):
        kwargs["return_embeddings"] = True

    embeddings: dict[str, list[float]] = {}
    output = pipeline(entrada, **kwargs)

    annotation, crudos = _desempaquetar_salida(output)
    if con_embeddings:
        embeddings = _embeddings_por_etiqueta(annotation, crudos)

    turns = [
        (segment.start, segment.end, speaker)
        for segment, _, speaker in annotation.itertracks(yield_label=True)
    ]
    turns.sort(key=lambda t: t[0])
    return turns, embeddings


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


def _preguntar_nombres(stats: dict[str, dict], sugerencias: dict[str, dict]) -> dict[str, str]:
    """Modo --enroll: unico punto interactivo de todo el pipeline. Lista los
    hablantes detectados con su tiempo de habla y un fragmento de lo que
    dijeron, y pide el nombre de cada uno. Enter deja el hablante sin nombre
    (seguira siendo SPEAKER_XX y lo resolvera el LLM por contexto)."""
    nombres: dict[str, str] = {}
    etiquetas = sorted(stats, key=lambda e: stats[e]["segundos"], reverse=True)
    print(
        "\n--- Alta de hablantes (--enroll) ---\n"
        "Escribe el nombre de cada hablante, o pulsa Enter para dejarlo sin "
        "identificar.\nRecuerda: un perfil de voz es un dato biometrico; no lo "
        "crees sin consentimiento."
    )
    for etiqueta in etiquetas:
        info = stats[etiqueta]
        sugerido = (sugerencias.get(etiqueta) or {}).get("nombre")
        print(
            f"\n{etiqueta} — habla {perfiles_voz.formato_duracion(info['segundos'])} "
            f"en {info['turnos']} turno(s)"
        )
        if info.get("muestra"):
            print(f'  "{info["muestra"]}"')
        aviso = f" [{sugerido}]" if sugerido else ""
        try:
            respuesta = input(f"  Nombre{aviso}: ").strip()
        except EOFError:
            respuesta = ""
        if not respuesta and sugerido:
            respuesta = sugerido
        if respuesta and perfiles_voz.nombre_valido(respuesta):
            nombres[etiqueta] = perfiles_voz.limpiar_nombre(respuesta)
        elif respuesta:
            print(f"  (ignorado: '{respuesta}' no identifica a nadie)")
    return nombres


def aplicar_perfiles_voz(
    turns: list[tuple[float, float, str]],
    segments,
    embeddings: dict[str, list[float]],
    ruta_perfiles: str | None,
    umbral: float,
    dry_run: bool,
    enroll: bool,
    aprender: bool,
) -> list[tuple[float, float, str]]:
    """Sustituye las etiquetas de pyannote por nombres de persona segun los
    perfiles guardados, y devuelve los turnos renombrados.

    Todo lo que se puede probar sin GPU (similitud, umbral, exclusividad,
    media incremental) vive en `perfiles_voz.py`; aqui solo se decide que
    hacer con el resultado.
    """
    perfiles = perfiles_voz.cargar(ruta_perfiles)
    if embeddings:
        perfiles_voz.comprobar_dimension(
            perfiles, len(next(iter(embeddings.values())))
        )

    stats = perfiles_voz.estadisticas_hablantes(turns, segments)
    clasificacion = perfiles_voz.clasificar(embeddings, perfiles, umbral)
    print()
    print(perfiles_voz.informe_clasificacion(clasificacion, stats, umbral))

    if enroll:
        nombres = _preguntar_nombres(stats, clasificacion)
    else:
        nombres = {
            etiqueta: datos["nombre"]
            for etiqueta, datos in clasificacion.items()
            if datos["nombre"]
        }

    if dry_run:
        print(
            "\n--dry-run-voz: no se aplican los nombres ni se actualizan los "
            "perfiles."
        )
        return turns

    # El centroide se actualiza como media incremental, de modo que el perfil
    # mejora con cada reunion. Se aprende de --enroll siempre (es una
    # confirmacion humana) y de las asignaciones automaticas salvo
    # --sin-aprender: una asignacion equivocada contamina el centroide, y con
    # un umbral aun sin calibrar conviene poder desactivarlo.
    if nombres and (enroll or aprender):
        cambios = 0
        for etiqueta, nombre in nombres.items():
            vector = embeddings.get(etiqueta)
            if not vector:
                continue
            # Un centroide creado con 12 s de habla queda guardado para
            # siempre y contamina las reuniones siguientes; identificar con
            # esa muestra si vale, porque solo afecta a esta.
            habla = stats.get(etiqueta, {}).get("segundos", 0.0)
            if habla < perfiles_voz.MINIMO_SEGUNDOS_MUESTRA:
                print(
                    f"Aviso: {etiqueta} ({nombre}) habla solo "
                    f"{perfiles_voz.formato_duracion(habla)}; no se guarda esa "
                    f"muestra en su perfil (minimo "
                    f"{perfiles_voz.MINIMO_SEGUNDOS_MUESTRA:.0f}s).",
                    file=sys.stderr,
                )
                continue
            try:
                perfiles_voz.actualizar(perfiles, nombre, vector, MODELO_DIARIZACION)
                cambios += 1
            except (ValueError, perfiles_voz.PerfilesIncompatibles) as exc:
                print(f"Aviso: perfil de '{nombre}' sin actualizar ({exc}).", file=sys.stderr)
        if cambios:
            destino = perfiles_voz.guardar(perfiles, ruta_perfiles)
            print(f"Perfiles de voz actualizados ({cambios}) en {destino}.")

    if nombres:
        print(
            "Hablantes identificados: "
            + ", ".join(f"{e} -> {n}" for e, n in sorted(nombres.items()))
        )
    return perfiles_voz.renombrar_turnos(turns, nombres)


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
    parser.add_argument(
        "--perfiles",
        default=None,
        help="Ruta al fichero de perfiles de voz (por defecto: TEAMS_PERFILES_VOZ "
        "o datos/perfiles_voz.json)",
    )
    parser.add_argument(
        "--sin-perfiles",
        action="store_true",
        help="No usar los perfiles de voz aunque existan: los hablantes se "
        "etiquetan SPEAKER_XX, como antes de la Fase 3",
    )
    parser.add_argument(
        "--umbral-voz",
        type=float,
        default=perfiles_voz.UMBRAL_POR_DEFECTO,
        help=f"Similitud coseno minima para dar por identificado a un hablante "
        f"(por defecto: {perfiles_voz.UMBRAL_POR_DEFECTO}). Calibralo con "
        "--dry-run-voz sobre grabaciones reales.",
    )
    parser.add_argument(
        "--dry-run-voz",
        action="store_true",
        help="Muestra las similitudes calculadas contra cada perfil pero no "
        "aplica los nombres ni actualiza los centroides (para calibrar el umbral)",
    )
    parser.add_argument(
        "--enroll",
        action="store_true",
        help="Pregunta interactivamente el nombre de cada hablante detectado y "
        "guarda su perfil de voz (alta del equipo; solo hace falta una vez)",
    )
    parser.add_argument(
        "--sin-aprender",
        action="store_true",
        help="No actualizar los centroides con las asignaciones automaticas de "
        "esta reunion (--enroll si actualiza: ahi hay confirmacion humana)",
    )
    args = parser.parse_args()

    if (args.enroll or args.dry_run_voz) and not args.diarize:
        print(
            "Error: --enroll y --dry-run-voz solo tienen sentido con --diarize.",
            file=sys.stderr,
        )
        sys.exit(1)

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
        con_voz = args.enroll or not args.sin_perfiles
        try:
            turns, embeddings = diarize_audio(
                audio_path,
                device,
                hf_token,
                args.num_speakers,
                con_embeddings=con_voz,
            )
            if con_voz:
                # Los perfiles de voz son opcionales dentro de una diarizacion
                # que ya funciono: si fallan, se conservan las etiquetas
                # genericas en vez de perder tambien la diarizacion.
                try:
                    if embeddings:
                        turns = aplicar_perfiles_voz(
                            turns,
                            result["segments"],
                            embeddings,
                            args.perfiles,
                            args.umbral_voz,
                            args.dry_run_voz,
                            args.enroll,
                            not args.sin_aprender,
                        )
                    else:
                        print(
                            "Aviso: pyannote no devolvio embeddings de voz; los "
                            "hablantes quedan como SPEAKER_XX.",
                            file=sys.stderr,
                        )
                except Exception as exc:  # noqa: BLE001 - degradacion elegante
                    print(
                        f"Aviso: los perfiles de voz no se pudieron aplicar "
                        f"({exc}). Se conservan las etiquetas genericas.",
                        file=sys.stderr,
                    )
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
