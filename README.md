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

