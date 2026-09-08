# Transcriptor de Teams (captura de audio local)

Graba localmente el audio de una llamada/reunión de Teams (tu micrófono + lo
que suena por los altavoces) y lo guarda en `.wav` para transcribirlo más
adelante con el motor que prefieras (Whisper, Azure Speech, etc.).

> ⚠️ **Aviso legal:** graba una reunión o llamada solo si cuentas con el
> consentimiento de todos los participantes, según la normativa que te aplique.

## Cómo funciona

- Captura tu **micrófono** (tu voz) con el dispositivo de entrada por defecto.
- Captura el **audio del sistema** (la reunión, el resto de participantes)
  usando el modo *loopback* de WASAPI sobre los altavoces por defecto — no
  requiere instalar ningún driver de audio virtual.
- Al terminar, genera 3 archivos en la carpeta de salida:
  - `<timestamp>_mic.wav` — tu voz
  - `<timestamp>_system.wav` — audio de la reunión
  - `<timestamp>_mixed.wav` — mezcla de ambos, lista para transcribir

## Instalación

Se recomienda usar un entorno virtual dentro de esta misma carpeta (evita
problemas de "rutas demasiado largas" de Windows con las dependencias de
PyTorch/Whisper):

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

A partir de aquí, usa `.\.venv\Scripts\python.exe` en vez de `python` en los
comandos siguientes.

## Uso: grabar

```powershell
# Ver los dispositivos de audio disponibles
.\.venv\Scripts\python.exe record_teams.py --list-devices

# Grabar hasta pulsar Ctrl+C (deja el script corriendo durante la reunión)
.\.venv\Scripts\python.exe record_teams.py --output-dir grabaciones

# Grabar con un límite de tiempo automático (segundos)
.\.venv\Scripts\python.exe record_teams.py --output-dir grabaciones --duration 3600
```

Durante la reunión de Teams, simplemente deja el script corriendo en segundo
plano; al terminar (Ctrl+C o al alcanzar `--duration`) se generan los `.wav`.

## Uso: transcribir

```powershell
.\.venv\Scripts\python.exe transcribe_teams.py grabaciones\20260907_090437_mixed.wav
```

Genera `<nombre>.txt` (texto plano) y `<nombre>.srt` (subtítulos con
tiempos) junto al audio. Opciones útiles:

```powershell
# Elegir tamaño de modelo (tiny/base/small/medium/large): a mayor tamaño,
# mejor calidad pero más lento (sobre todo en CPU).
.\.venv\Scripts\python.exe transcribe_teams.py archivo.wav --model medium

# Forzar CPU o GPU (por defecto se autodetecta si hay CUDA disponible)
.\.venv\Scripts\python.exe transcribe_teams.py archivo.wav --device cuda

# Autodetectar idioma en vez de forzar español
.\.venv\Scripts\python.exe transcribe_teams.py archivo.wav --language auto
```

En este equipo (sin GPU) se ejecuta en CPU y puede tardar bastante con
audios largos; con `--model tiny` o `--model base` va razonablemente rápido
para pruebas. Cuando uses una máquina con GPU NVIDIA, instala la build de
PyTorch con soporte CUDA y usa `--model medium`/`large` sin problema.

## Uso: identificar hablantes (diarización)

Con `--diarize`, además de transcribir, identifica qué segmentos pertenecen
a cada hablante usando [pyannote.audio](https://github.com/pyannote/pyannote-audio)
y añade una etiqueta `[SPEAKER_00]`, `[SPEAKER_01]`, etc. a cada línea del
`.txt`/`.srt`.

1. Instala la dependencia opcional (no incluida por defecto porque es
   pesada y requiere un token):

   ```powershell
   .\.venv\Scripts\python.exe -m pip install "pyannote.audio>=3.1"
   ```

2. Crea un token de acceso en <https://huggingface.co/settings/tokens> (rol
   "Read" es suficiente) y acepta las condiciones de uso de estos dos
   modelos con esa misma cuenta (son formularios rápidos, se aprueban al
   instante):
   - <https://huggingface.co/pyannote/speaker-diarization-3.1>
   - <https://huggingface.co/pyannote/segmentation-3.0>

3. Ejecuta la transcripción con `--diarize`, pasando el token con
   `--hf-token` o mediante la variable de entorno `HF_TOKEN`:

   ```powershell
   $env:HF_TOKEN = "hf_xxx"
   .\.venv\Scripts\python.exe transcribe_teams.py grabaciones\20260907_090437_mixed.wav --diarize

   # o sin variable de entorno:
   .\.venv\Scripts\python.exe transcribe_teams.py archivo.wav --diarize --hf-token hf_xxx

   # si conoces el numero exacto de participantes, mejora la precision:
   .\.venv\Scripts\python.exe transcribe_teams.py archivo.wav --diarize --num-speakers 3
   ```

La diarización es un paso adicional sobre el audio completo y en CPU puede
tardar tanto o más que la propia transcripción; en GPU (`--device cuda`) es
mucho más rápida. Las etiquetas (`SPEAKER_00`, ...) son genéricas: pyannote
no conoce los nombres reales, solo distingue voces distintas.

## Uso: resumir con un LLM local (LiteLLM)

Con `summarize_teams.py` puedes generar un resumen estructurado (Markdown:
resumen general, por hablante, acciones pendientes, bloqueos) a partir de
una transcripción `.txt`, usando un LLM servido en local a través de
[LiteLLM](https://docs.litellm.ai/) (API compatible con OpenAI).

```powershell
$env:LITELLM_API_KEY = "sk-..."   # pega la key en tu terminal, no en el chat
.\.venv\Scripts\python.exe summarize_teams.py grabaciones\20260907_090437_mixed.txt

# Elegir modelo (por defecto: qwen3.6-35b-a3b) o URL de LiteLLM
.\.venv\Scripts\python.exe summarize_teams.py archivo.txt --model qwen3.8-27b
.\.venv\Scripts\python.exe summarize_teams.py archivo.txt --base-url http://localhost:4000/v1

# Dar pistas de los nombres reales del equipo (ayuda a mapear SPEAKER_XX)
.\.venv\Scripts\python.exe summarize_teams.py archivo.txt --attendees "Pablo, Javi, Jose Javier"

# Otros tipos de reunión (por defecto: daily)
.\.venv\Scripts\python.exe summarize_teams.py archivo.txt --tipo retro
```

Genera `<nombre>_resumen.md` junto a la transcripción, con una sección de
"Mapeo de hablantes" (con nivel de confianza alta/media/baja), resumen
general, estado por persona, acciones pendientes y bloqueos/riesgos.
Funciona mejor sobre transcripciones generadas con `--diarize` (ver arriba),
ya que puede diferenciar qué dijo cada hablante (`SPEAKER_00`, `SPEAKER_01`,
...); el propio modelo intenta inferir el nombre real a partir del contexto
de la conversación (o de `--attendees` si se lo indicas) — conviene
revisar el mapeo, puede equivocarse o marcar "no identificado" si no hay
contexto suficiente.

`--tipo {daily,workshop,retro,planning}` cambia el prompt y las secciones del
resumen: una retro produce "qué fue bien / qué no / acciones de mejora", un
planning "alcance comprometido / estimaciones / dudas", y un workshop "temas /
decisiones / preguntas sin resolver". El resto de secciones es común a todos.

### Histórico en SQLite

Además del `.md`, cada resumen se guarda en `datos/meetings.db` (SQLite, sin
dependencias externas): la reunión, sus segmentos de transcripción con marcas
de tiempo, el estado por persona, las acciones y los riesgos. Es la base para
poder consultar el histórico más adelante en vez de releer ficheros sueltos.

```powershell
# Otra ruta para la base (o define la variable de entorno TEAMS_DB)
.\.venv\Scripts\python.exe summarize_teams.py archivo.txt --db D:\datos\meetings.db

# Solo Markdown, sin tocar la base
.\.venv\Scripts\python.exe summarize_teams.py archivo.txt --sin-bd
```

La fecha de la reunión se deduce del nombre del fichero
(`20260907_090437_mixed.txt` → `2026-09-07`) y, si no, de su fecha de
modificación; se puede forzar con `--fecha` y ponerle nombre con `--titulo`.
Volver a procesar la misma transcripción **sustituye** la reunión anterior en
vez de duplicarla, así que se puede reintentar sin ensuciar la base.

El fichero contiene el texto literal de reuniones internas: vive en `datos/`,
que está en `.gitignore`, y conviene tratarlo con el mismo cuidado que las
grabaciones.

## Glosario del proyecto (opcional, muy recomendable)

Whisper destroza sistemáticamente las siglas y nombres propios que no conoce
("Goverity" por Coverity). Para evitarlo, copia `datos/glosario.ejemplo.md` a
`datos/glosario.md` y rellénalo con el vocabulario de tu equipo:

```powershell
copy datos\glosario.ejemplo.md datos\glosario.md
```

A partir de ahí, `transcribe_teams.py` y `summarize_teams.py` lo usan solos:

- La sección entre `<!-- whisper -->` y `<!-- /whisper -->` se pasa a Whisper
  como `initial_prompt` y sesga el reconocimiento hacia esos términos. Está
  limitada a unos 700 caracteres (~224 tokens, tope del parámetro), así que
  deja ahí solo lo más importante.
- El documento **entero** se inyecta en el prompt del LLM al resumir, para que
  corrija los términos que Whisper haya deformado.

Opciones en ambos scripts: `--glosario RUTA` para usar otro fichero,
`--sin-glosario` para desactivarlo.

`datos/` está en `.gitignore`: el glosario contiene nombres de personas y jerga
interna, y no debe versionarse. Si transcribes en otra máquina (ver
`DEPLOY_OFFLINE.md`), cópialo también allí.

## Notas técnicas

- La captura del audio del sistema solo produce datos mientras el motor de
  audio de Windows está activo (es decir, mientras algo suena). Esto es
  normal y no afecta a una llamada real de Teams, donde el audio se mantiene
  continuo durante toda la reunión.
- Si tienes varios dispositivos de audio (auriculares, altavoces del PC,
  etc.), el script usa siempre el **dispositivo por defecto** de Windows en
  el momento de arrancar. Configura el dispositivo correcto en el
  "Mezclador de volumen" de Windows antes de grabar si usas varios.

## Siguiente paso: transcripción

La transcripción ya está integrada (`transcribe_teams.py`, ver más arriba),
usando [Whisper](https://github.com/openai/whisper) en local:

- **Whisper local vía `openai-whisper`** (offline, gratis): es lo que usa
  este proyecto.
- Alternativas si en el futuro quieres otro motor: Azure AI Speech, OpenAI
  Whisper API, `faster-whisper`, `whisper.cpp`, etc.

