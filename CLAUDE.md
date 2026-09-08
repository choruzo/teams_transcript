# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Qué es este proyecto

Herramienta local en dos scripts independientes para grabar reuniones de Teams y transcribirlas con Whisper. No hay build system, tests, ni linter configurado: es Python puro ejecutado directamente.

- `record_teams.py` — graba micrófono + audio del sistema (loopback WASAPI) y genera `_mic.wav`, `_system.wav`, `_mixed.wav`. **Solo funciona en Windows** (depende de `pyaudiowpatch`/WASAPI).
- `transcribe_teams.py` — transcribe un `.wav` con Whisper (`openai-whisper`), generando `.txt` y `.srt`. Multiplataforma (Windows/Linux), usa GPU CUDA si está disponible. Con `--diarize` añade identificación de hablantes vía `pyannote.audio` (dependencia opcional, requiere token de HuggingFace).
- `glosario.py` — módulo compartido que carga el glosario del proyecto (`datos/glosario.md`) y prepara sus dos usos: `initial_prompt` de Whisper y contexto del LLM. Solo stdlib.
- `summarize_teams.py` — resume un `.txt` de transcripción usando un LLM local servido vía LiteLLM (API compatible con OpenAI, sin dependencias externas más allá de la stdlib), generando `<nombre>_resumen.md` y volcando el resultado en el histórico SQLite.
- `memoria.py` — capa de acceso a `datos/meetings.db` (SQLite + FTS5, solo stdlib). Ningún otro script escribe SQL suelto.

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

Tests (solo `memoria.py` de momento; no hay linter):

```powershell
python -m unittest discover -s tests -v
```

Son `unittest` de la stdlib, sin pytest ni dependencias: tienen que poder ejecutarse en el servidor offline. Lo que no cubren —la transcripción y la llamada al LLM— se sigue verificando ejecutando los scripts a mano contra un `.wav` de prueba.

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
- **El LLM devuelve JSON, no Markdown** (`summarize_teams.py`, Fase 1 del plan): se pide un objeto JSON con esquema fijo (`temperature` 0.1) y el `.md` se **renderiza en Python** desde ese JSON (`renderizar_markdown`), de modo que una sola llamada alimenta el fichero y la BD. `extraer_json` acepta el JSON envuelto en bloque de código o rodeado de prosa; si falla, hay **un** reintento devolviéndole al modelo su propia respuesta, y si vuelve a fallar se guarda la respuesta cruda como `.md`, se avisa por stderr y se sale con código 2 sin tocar la BD. `normalizar()` tolera claves ausentes o del tipo equivocado, pero nada sin validar entra en SQLite.
- **Tipos de reunión** (`--tipo {daily,workshop,retro,planning}`, por defecto `daily`): diccionario `TIPOS` en `summarize_teams.py`. Cada tipo cambia el enfoque del prompt y aporta **una clave JSON propia** (`retro`, `planning`, `workshop`) que se renderiza como secciones extra; el resto del esquema es común, para que la BD y los futuros informes no tengan que saber de tipos. `daily` no añade clave: es el comportamiento de siempre.
- **Almacén SQLite** (`memoria.py`): esquema idempotente (`CREATE TABLE IF NOT EXISTS` + `PRAGMA user_version`, hoy versión 2). Ruta: `--db` > variable `TEAMS_DB` > `datos/meetings.db`. `segments_fts` es una tabla FTS5 de *contenido externo*, sincronizada con tres triggers (insert/delete/update); si se toca `segments` por otra vía hay que mantenerlos. Tokenizador `unicode61 remove_diacritics 2`, así que la búsqueda ignora acentos pero **no** hace stemming.
- **Arrastres entre reuniones** (Fase 2): antes de llamar al LLM,
  `cargar_acciones_abiertas` lee de la BD las acciones sin cerrar de las
  últimas N reuniones (`--arrastres N`, por defecto 5; `--sin-arrastres` lo
  desactiva, igual que `--sin-bd`) y las inyecta en el *user prompt* con su
  `action_id`; el *system prompt* solo lleva las instrucciones de arrastre
  (`ARRASTRES_PROMPT`) si esa lista existe. La respuesta se valida en
  `normalizar()` **contra esa misma lista**: sin lista no se acepta ningún
  arrastre, y con ella se descartan los ids inventados, los duplicados y los
  estados fuera de `ESTADOS_ACCION` (incluido `sin_mencion`, que el modelo sí
  puede devolver pero no cambia nada). Los que sobreviven se enriquecen con la
  descripción y la persona reales para el Markdown, y se marcan `ESTANCADA` a
  partir de `UMBRAL_ESTANCAMIENTO` (3) menciones sin cerrarse.
- **`acciones_abiertas(excluir_meeting_id=...)` simula el deshacer**: al
  reprocesar una transcripción ya registrada (su id se obtiene con
  `id_reunion_por_transcripcion`), esa reunión se excluye y las acciones
  *ajenas* que aquella pasada tocó se devuelven con el estado y las menciones
  que tenían **antes** — lo mismo que hará `_deshacer_arrastres` al borrarla
  después. Sin eso, una acción que la pasada anterior dio por completada no
  volvería a ofrecerse al modelo y el reproceso no sería idempotente.
- **`meetings.uid` es la identidad estable de una reunión**, y `meetings.id` no lo es: reprocesar borra e inserta la fila. `uid_transcripcion` lo deriva del **nombre** del `.txt` (no de la ruta, para que mover `grabaciones/` no cambie la identidad), normalizado a `[a-z0-9_-]`; `_uid_disponible` desempata con un hash corto de la ruta si dos transcripciones distintas comparten nombre. `crear_reunion` conserva al reemplazar **tanto el `uid` como el `id` anterior** (lo reinserta explícitamente), porque los enlaces de la futura interfaz y las citas del chat cuelgan de ese identificador. Resolver un `uid`: `reunion_por_uid`.
- **`conectar(solo_lectura=True)`** para todo lo que solo consulta (la API web): no toca el esquema y activa `PRAGMA query_only`. **No** usa la URI `mode=ro` a propósito: con la base en WAL, una conexión `mode=ro` no puede crear el fichero `-shm` que SQLite necesita para leer y falla justo cuando nadie más está escribiendo, que es el caso normal.
- **WAL activado** en `conectar()`, para que la API pueda leer mientras el pipeline escribe. Es una propiedad persistente de la base. No sirve si `datos/` acabara en un sistema de ficheros en red.
- **Migraciones reales** (`_migrar`): `CREATE TABLE IF NOT EXISTS` no añade columnas a una tabla que ya existe, así que las bases anteriores pasan por `ALTER TABLE`. Se migra **antes** de ejecutar `_ESQUEMA`, porque el script crea el índice UNIQUE sobre `meetings.uid` y esa columna aún no existe en una base v1.
- **Rutas entre máquinas** (`ruta_local`): `audio_path` y `transcript_path` se guardan absolutas y tal como las vio la máquina que procesó la reunión. Quien lea la base desde otro sitio (la API en un contenedor) define `TEAMS_RAIZ_ORIGEN`/`TEAMS_RAIZ_LOCAL` y se sustituye el prefijo, comparando con separadores normalizados porque la ruta pudo escribirse en Windows y leerse en Linux. Sin esas variables, la ruta se devuelve tal cual.
- **Tests**: `python -m unittest discover -s tests -v`. Solo stdlib, sin pytest, para que funcionen en el servidor offline sin instalar nada. Cubren la identidad estable, la conexión de solo lectura, WAL, el remapeo de rutas, la migración 1→2 y —como red de seguridad— la idempotencia de los arrastres de la Fase 2.
- **Reprocesar una reunión la sustituye**: `crear_reunion(..., reemplazar=True)` borra la fila anterior con el mismo `transcript_path` (cascada a segmentos, updates, riesgos y acciones nacidas en ella). Antes llama a `_deshacer_arrastres`, que devuelve a su origen las acciones *ajenas* que esa pasada actualizó: sin eso, la FK `actions.meeting_id_ultima` (sin `ON DELETE`) impediría el borrado y las menciones quedarían infladas. El estado previo no se guarda, así que las cerradas por esa reunión vuelven a `abierta`.
- **Los segmentos entran por `summarize_teams.py`, no por `transcribe_teams.py`**: se leen del `.srt` hermano (mismo nombre, otra extensión) porque el `.txt` no lleva marcas de tiempo; si no existe el `.srt` se cae al `.txt` con `inicio`/`fin` a NULL. Decisión deliberada para no tocar la parte frágil del pipeline (Whisper/pyannote/GPU) en la Fase 1.
- **Nombres genéricos no crean personas**: `obtener_o_crear_persona` devuelve `None` para `SPEAKER_XX`, "no identificado" y similares; las filas quedan con `persona_id` NULL en vez de inventar gente. El emparejamiento es por nombre sin distinguir mayúsculas y por la lista de alias (`personas.alias`, JSON).
- **Glosario del proyecto** (`glosario.py` + `datos/glosario.md`): vocabulario, siglas y nombres del equipo, usado en dos puntos para que Whisper no destroce la jerga interna. La sección entre `<!-- whisper -->` y `<!-- /whisper -->` alimenta el `initial_prompt` de Whisper; el documento completo se inyecta en el system prompt de `summarize_teams.py` para que corrija los términos deformados. Ambos scripts lo cargan solos si existe `datos/glosario.md`, con `--glosario RUTA` y `--sin-glosario` para controlarlo. **Ojo con el límite**: Whisper recorta `initial_prompt` a `n_ctx // 2 - 1` tokens (~224) quedándose con los **últimos**, así que pasarse borra el principio del glosario, no el final; por eso `glosario.py` trunca antes en 700 caracteres y avisa. `datos/` no se versiona (contiene datos de reuniones); la plantilla `datos/glosario.ejemplo.md` sí.
- **`carry_initial_prompt` es imprescindible con el glosario**: Whisper inyecta `initial_prompt` solo en la **primera ventana de 30 s**; a partir de ahí `condition_on_previous_text` hace que el contexto sea el texto ya transcrito, y el glosario se diluye (medido: 3 de 5 aciertos sin carry, 5 de 5 con carry). Por eso `transcribe_teams.py` pasa `carry_initial_prompt=True` por defecto cuando hay glosario (`--sin-carry` lo desactiva), detectando antes con `inspect.signature` si la versión instalada lo soporta (existe desde `openai-whisper` 20240930; el servidor offline podría llevar una anterior).
- **Limpieza de bucles de repetición** (`colapsar_repeticiones`): el `carry` realimenta las alucinaciones repetitivas de Whisper sobre el silencio final. La función distingue dos casos para no destruir habla legítima: textos cortos ("Vale.", "Gracias.") solo se recortan si se repiten más de 2 veces **seguidas**; frases de 4+ palabras se descartan si ya aparecen en los 10 segmentos anteriores (la racha suele venir rota por líneas sueltas, por eso ventana y no comparación con el anterior). Se aplica antes de escribir `.txt`/`.srt`; `--sin-limpieza` lo desactiva.
- **Codificación de consola**: `transcribe_teams.py` fuerza `utf-8` en `sys.stdout` porque la consola de Windows (cp1252/cp850) no soporta todos los caracteres que puede emitir Whisper.
- **Despliegue offline con GPU** (`DEPLOY_OFFLINE.md`): describe cómo preparar un "bundle" (wheels de PyTorch/whisper para Linux x86_64/Python 3.12, ffmpeg estático, modelos `.pt` cacheados) para transcribir en un servidor Ubuntu sin acceso a internet. Relevante solo si se toca el flujo de transcripción en un entorno sin conexión.
- `grabaciones/` (salida de audio) y `.venv/` están en `.gitignore`; no versionar `.wav`/`.pt`.

## Aviso legal

Grabar una reunión/llamada requiere el consentimiento de todos los participantes según la normativa aplicable — este aviso está impreso en el propio `record_teams.py` y en el `README.md`.
