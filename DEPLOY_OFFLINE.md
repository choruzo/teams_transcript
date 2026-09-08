# Despliegue offline en servidor Ubuntu 24.04 + NVIDIA L4

Este proyecto tiene dos partes con requisitos distintos:

- **`record_teams.py`** (grabar): usa WASAPI loopback → **solo funciona en Windows**.
  Sigue grabando las reuniones en tu PC Windows como hasta ahora.
- **`transcribe_teams.py`** (transcribir): es Python + PyTorch + ffmpeg, **sí
  funciona en Linux/Ubuntu con GPU NVIDIA**.

Flujo recomendado: grabas la reunión en Windows → copias el `_mixed.wav`
(USB, red, etc.) al servidor Ubuntu con la L4 → transcribes ahí a máxima
velocidad, sin necesidad de internet en el servidor.

## 1. Preparar el "bundle" offline (en una máquina CON internet)

Necesitas 3 cosas para llevar al servidor:

### a) Wheels de Python para Linux x86_64 / Python 3.12 (el que trae Ubuntu 24.04)

```powershell
# En una maquina CON internet (puede ser esta misma, da igual el SO:
# pip download solo descarga paquetes, no los instala)
pip download torch --index-url https://download.pytorch.org/whl/cu121 `
  --platform manylinux2014_x86_64 --python-version 312 --implementation cp `
  --abi cp312 --only-binary=:all: -d wheelhouse

pip download numpy openai-whisper --platform manylinux2014_x86_64 `
  --python-version 312 --implementation cp --abi cp312 --only-binary=:all: -d wheelhouse
```

> Antes de instalar en el servidor, comprueba con `nvidia-smi` la versión de
> driver/CUDA soportada. `cu121` (CUDA 12.1) funciona con drivers >= 525 y es
> compatible con la L4. Si el driver es más reciente, puedes usar `cu124`
> cambiando el `--index-url`.

### b) ffmpeg estático para Linux (whisper lo necesita para leer el audio)

Descarga un build estático (no requiere instalar nada más) desde
https://johnvansickle.com/ffmpeg/ (release "linux-amd64", build "static"),
o el equivalente que prefieras. Solo necesitas el binario `ffmpeg`.

### c) El/los modelo(s) de Whisper ya descargados

Ya están cacheados en este PC en:
```
%USERPROFILE%\.cache\whisper\tiny.pt
%USERPROFILE%\.cache\whisper\small.pt
%USERPROFILE%\.cache\whisper\large-v3.pt
```
Cópialos tal cual (son ficheros `.pt`, independientes del SO).

### d) Los scripts y el glosario

Copia de este proyecto:

- `transcribe_teams.py`
- `glosario.py` — módulo que `transcribe_teams.py` importa. **Sin él, el
  script no arranca** (`ModuleNotFoundError: glosario`).
- `datos/glosario.md` — el glosario del proyecto. Es opcional (sin él se
  transcribe igual, solo que sin sesgo de vocabulario), pero **no está en
  git**, así que hay que copiarlo a mano en cada actualización.

## 2. Transferir al servidor

Copia (USB, `scp`, red interna...) al servidor Ubuntu:
- la carpeta `wheelhouse/` completa
- el binario `ffmpeg`
- los `.pt` de los modelos
- `transcribe_teams.py` y `glosario.py`, en el mismo directorio
- `datos/glosario.md`

`glosario.py` busca por defecto `datos/glosario.md` **relativo a su propia
ubicación**, así que si respetas esa estructura (`glosario.py` y la carpeta
`datos/` hermanas) se detecta solo. Si lo dejas en otro sitio, indícalo con
`--glosario /ruta/al/glosario.md`.

## 3. Instalar y configurar en el servidor (sin internet)

```bash
# Comprobar que el driver NVIDIA ve la L4
nvidia-smi

# Crear entorno virtual
python3 -m venv ~/whisper-env
source ~/whisper-env/bin/activate

# Instalar SOLO desde el wheelhouse local, sin tocar la red
pip install --no-index --find-links=/ruta/a/wheelhouse torch openai-whisper numpy

# Colocar ffmpeg en el PATH
sudo cp /ruta/a/ffmpeg /usr/local/bin/ffmpeg
sudo chmod +x /usr/local/bin/ffmpeg

# Colocar los modelos en la cache que espera whisper
mkdir -p ~/.cache/whisper
cp /ruta/a/*.pt ~/.cache/whisper/
```

## 4. Transcribir usando la GPU

```bash
source ~/whisper-env/bin/activate
python transcribe_teams.py mixed.wav --model large-v3 --device cuda --language es
```

`transcribe_teams.py` detecta automáticamente la GPU si no se indica
`--device`, así que también funciona sin especificarlo. Con una L4 y el
modelo `large-v3`, la transcripción será mucho más rápida que en CPU.

En el arranque el script imprime `Glosario: si/no`. Si dice `no` y esperabas
que sí, es que no encuentra `datos/glosario.md`: compruébalo con
`--glosario /ruta/explicita/glosario.md`. Para desactivarlo a propósito,
`--sin-glosario`.

## Notas

- Si `pip download` con `--platform` falla por incompatibilidades de
  metadatos (ocurre a veces con paquetes con extensiones nativas), es más
  fiable ejecutar los comandos `pip download` directamente desde una máquina
  Linux x86_64 con Python 3.12 y acceso a internet (por ejemplo una VM
  temporal), en vez de cruzar plataformas desde Windows.
- Verifica la versión exacta de CUDA soportada por el driver instalado en el
  servidor (`nvidia-smi` muestra "CUDA Version: X.Y" en la esquina superior
  derecha) antes de elegir el índice de PyTorch (`cu121`, `cu124`, etc.).

## 5. Diarización (`--diarize`) en el servidor sin acceso a huggingface.co

Si el servidor tiene acceso a PyPI pero no a `huggingface.co` (caso típico:
salida a internet restringida a repos de paquetes), puedes instalar
`pyannote.audio` normalmente con pip, pero los modelos hay que llevarlos a
mano porque no se podrán descargar en el servidor.

### a) Instalar pyannote.audio (con acceso a PyPI)

```bash
pip install "pyannote.audio>=3.1"
```

### b) Copiar la caché de modelos de HuggingFace ya descargada

En este PC Windows (con internet y con las condiciones de uso ya aceptadas
en huggingface.co para los 3 modelos gated), la caché está en:

```
%USERPROFILE%\.cache\huggingface\hub\
  models--pyannote--segmentation-3.0
  models--pyannote--speaker-diarization-3.1
  models--pyannote--speaker-diarization-community-1
  models--pyannote--wespeaker-voxceleb-resnet34-LM
```

Copia la carpeta `hub` completa (USB, `scp`, red interna...) a la ruta
equivalente en el servidor:

```
~/.cache/huggingface/hub/
```

### c) Ejecutar en modo offline

Con la caché ya en su sitio, no hace falta token: define
`HF_HUB_OFFLINE=1` para que `huggingface_hub` no intente contactar con
`huggingface.co` en ningún momento (ni para comprobar actualizaciones) y use
solo lo que ya tiene en caché.

```bash
export HF_HUB_OFFLINE=1
python transcribe_teams.py mixed.wav --diarize --device cuda
```

Si el servidor SÍ tuviera acceso a `huggingface.co`, la alternativa más
simple es no copiar nada y pasar `--hf-token`/`HF_TOKEN` como en Windows,
dejando que descargue los modelos directamente ahí.
