# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Qué es este proyecto

Herramienta local en dos scripts independientes para grabar reuniones de Teams y transcribirlas con Whisper. No hay build system, tests, ni linter configurado: es Python puro ejecutado directamente.

- `record_teams.py` — graba micrófono + audio del sistema (loopback WASAPI) y genera `_mic.wav`, `_system.wav`, `_mixed.wav`. **Solo funciona en Windows** (depende de `pyaudiowpatch`/WASAPI).
- `transcribe_teams.py` — transcribe un `.wav` con Whisper (`openai-whisper`), generando `.txt` y `.srt`. Multiplataforma (Windows/Linux), usa GPU CUDA si está disponible. Con `--diarize` añade identificación de hablantes vía `pyannote.audio` (dependencia opcional, requiere token de HuggingFace).
- `summarize_teams.py` — resume un `.txt` de transcripción usando un LLM local servido vía LiteLLM (API compatible con OpenAI, sin dependencias externas más allá de la stdlib), generando `<nombre>_resumen.md`.

Estos dos scripts se ejecutan en máquinas distintas en el flujo típico: se graba en un PC Windows y se transcribe en un servidor Linux con GPU (ver `DEPLOY_OFFLINE.md`).

## Comandos habituales

Entorno virtual (recomendado, evita problemas de rutas largas de Windows con PyTorch):

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Grabar:

```powershell
.\.venv\Scripts\python.exe record_teams.py --list-devices
.\.venv\Scripts\python.exe record_teams.py --output-dir grabaciones
.\.venv\Scripts\python.exe record_teams.py --output-dir grabaciones --duration 3600
```

Transcribir:

```powershell
.\.venv\Scripts\python.exe transcribe_teams.py grabaciones\<timestamp>_mixed.wav
.\.venv\Scripts\python.exe transcribe_teams.py archivo.wav --model medium --device cuda --language auto
```

No hay suite de tests ni linter en el repo; verificar cambios ejecutando los scripts manualmente contra un `.wav` de prueba.

## Arquitectura y puntos a tener en cuenta

- **Captura dual con callback no bloqueante** (`record_teams.py`): el dispositivo loopback WASAPI no produce datos cuando no suena nada por los altavoces, así que un `stream.read()` bloqueante se colgaría en silencio. Por eso la grabación usa `stream_callback` + una cola (`queue.Queue`) por cada fuente (mic y sistema), volcada a `.wav` al terminar en `drain_queue_to_wav`.
- **Dispositivos por defecto en tiempo de arranque**: `get_default_loopback` resuelve el dispositivo loopback asociado a los altavoces por defecto de Windows en el momento de iniciar; si el usuario cambia de dispositivo de audio a mitad de reunión, no se reflejará.
- **Mezcla con resample** (`mix_wavs`): mic y sistema pueden tener sample rates distintos; se resamplean ambos a la frecuencia más alta antes de sumarlos, y se normaliza si hay clipping (pico > 32767).
- **Detección de dispositivo en transcripción**: `detect_device` en `transcribe_teams.py` autodetecta CUDA vía `torch.cuda.is_available()` si no se pasa `--device` explícito; degrada a CPU si `torch` no está disponible o no hay GPU.
- **Diarización de hablantes** (`--diarize`): `diarize_audio` ejecuta el pipeline `pyannote/speaker-diarization-3.1` sobre el audio completo (independiente de Whisper) y `assign_speakers` asigna a cada segmento de Whisper el hablante de pyannote con mayor solapamiento temporal (`start`/`end` en segundos). Es un import perezoso (`from pyannote.audio import Pipeline` dentro de la función) para no exigir la dependencia si no se usa el flag. Requiere aceptar las condiciones de **tres** repos gated en HuggingFace con la cuenta del token: `pyannote/speaker-diarization-3.1`, `pyannote/segmentation-3.0` y `pyannote/speaker-diarization-community-1` (este último solo se pide al cargar el pipeline completo, no al comprobar acceso a los dos primeros).
- **Carga de audio sin torchcodec**: `_load_waveform` decodifica el `.wav` a mano con el módulo estándar `wave` (PCM 16-bit) y se lo pasa a pyannote como `{"waveform": tensor, "sample_rate": rate}` en memoria, en vez de pasar la ruta del fichero. Esto evita que `pyannote.audio` 4.x use `torchcodec`, que en Windows requiere DLLs de FFmpeg "full-shared" no instaladas por defecto y falla con `OSError: Could not load this library`.
- **Ejecución offline de la diarización**: si `HF_HUB_OFFLINE=1` está definida, `--diarize` no exige `--hf-token`/`HF_TOKEN` (asume que los modelos ya están en la caché local de `huggingface_hub`). Útil para el servidor Ubuntu con GPU sin acceso a huggingface.co (ver `DEPLOY_OFFLINE.md`, sección 5): hay que copiar a mano `~/.cache/huggingface/hub/` con los 4 modelos (los 3 gated + `wespeaker-voxceleb-resnet34-LM`).
- La diarización en CPU es notablemente más lenta que la transcripción de Whisper sobre el mismo audio; en GPU es mucho más rápida. La transcripción (`.txt`/`.srt`) siempre se guarda **antes** de intentar diarizar, y si la diarización falla se conserva sin etiquetas de hablante en vez de perder el trabajo de Whisper (ver manejo de excepciones en `main()`).
- **Resumen con LLM local** (`summarize_teams.py`): llama al endpoint `/chat/completions` de un proxy LiteLLM en `http://localhost:4000/v1` (configurable con `--base-url`) usando solo `urllib` de la stdlib, sin añadir dependencias. Requiere `--api-key`/`LITELLM_API_KEY`. El nombre de modelo (`--model`, por defecto `qwen3.6-35b-a3b`) debe coincidir con un `model_name` configurado en ese LiteLLM; para listar los disponibles: `curl http://localhost:4000/v1/models -H "Authorization: Bearer <key>"`.
- **Codificación de consola**: `transcribe_teams.py` fuerza `utf-8` en `sys.stdout` porque la consola de Windows (cp1252/cp850) no soporta todos los caracteres que puede emitir Whisper.
- **Despliegue offline con GPU** (`DEPLOY_OFFLINE.md`): describe cómo preparar un "bundle" (wheels de PyTorch/whisper para Linux x86_64/Python 3.12, ffmpeg estático, modelos `.pt` cacheados) para transcribir en un servidor Ubuntu sin acceso a internet. Relevante solo si se toca el flujo de transcripción en un entorno sin conexión.
- `grabaciones/` (salida de audio) y `.venv/` están en `.gitignore`; no versionar `.wav`/`.pt`.

## Aviso legal

Grabar una reunión/llamada requiere el consentimiento de todos los participantes según la normativa aplicable — este aviso está impreso en el propio `record_teams.py` y en el `README.md`.
