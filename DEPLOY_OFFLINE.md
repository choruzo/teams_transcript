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

---

## 6. Índice semántico del chat (`sqlite-vec` + bge-m3) sin internet

El chat de la web (fase I5) busca de dos formas a la vez: FTS5, que ya
funciona sin nada más, y búsqueda vectorial sobre `datos/indice.db`. Esta
segunda parte necesita dos piezas que hay que llevar al servidor a mano.

**Sin ellas el chat sigue funcionando**, con búsqueda solo literal, y lo dice
en pantalla. No es un requisito para desplegar: es una mejora de calidad.

### a) El wheel de `sqlite-vec` (en una máquina CON internet)

```bash
# Es un wheel por plataforma: hay que pedir el de Linux x86_64.
pip download sqlite-vec==0.1.9 \
    --only-binary=:all: --platform manylinux2014_x86_64 \
    --python-version 3.12 -d bundle/wheelhouse-vec
```

En el servidor, dentro del venv que usa el pipeline:

```bash
pip install --no-index --find-links bundle/wheelhouse-vec sqlite-vec
python -c "import sqlite_vec, sqlite3; c=sqlite3.connect(':memory:'); \
c.enable_load_extension(True); sqlite_vec.load(c); print(c.execute('select vec_version()').fetchone())"
```

Si esa comprobación falla con `AttributeError: enable_load_extension`, el
Python del sistema se compiló sin extensiones cargables y hay que usar otro
intérprete; el resto del proyecto no se ve afectado.

**La API también lo necesita**, pero ahí va dentro de la imagen Docker
(`requirements-api.txt`), así que basta con reexportarla: ver
`docker/DESPLIEGUE.md`.

### b) El modelo de embeddings

**Puede que no haya proxy LiteLLM.** El servidor de este proyecto tiene dos
`llama-server` (llama.cpp) sueltos, cada uno con su modelo y su puerto, y
ambos exponen `/v1` compatible con OpenAI. Los dos montajes funcionan; solo
cambia cómo se configuran.

Averiguar qué hay escuchando:

```bash
ss -ltnp | grep -E "llama|4000"
curl -s http://localhost:8000/v1/models   # chat
curl -s http://localhost:8085/v1/models   # embeddings
```

**Con `llama-server` directo** (el caso de este servidor): dos URL distintas y
ninguna clave. `TEAMS_EMBED_BASE_URL` existe precisamente para esto; sin ella
habría que elegir cuál de los dos modelos funciona.

```bash
LITELLM_BASE_URL=http://localhost:8000/v1     # chat
TEAMS_EMBED_BASE_URL=http://localhost:8085/v1 # embeddings
TEAMS_LLM_MODELO=qwen3.8-27b
TEAMS_EMBED_MODELO=bge-m3
LITELLM_API_KEY=                              # vacía: no pide ninguna
```

El nombre del modelo da igual con `llama-server`: sirve el que tenga cargado,
así que `bge-m3` vale aunque su `/v1/models` devuelva la ruta del `.gguf`.

**Ojo con `localhost` frente a `host.docker.internal`**: el pipeline corre en
el host y usa `localhost`; la API corre en el contenedor, donde `localhost` es
el propio contenedor. En el `.env` (que lee Docker Compose) van las URL con
`host.docker.internal`, que el compose resuelve con `extra_hosts`. Y el
`llama-server` tiene que escuchar en `0.0.0.0`, no solo en `127.0.0.1`, o el
contenedor no llegará.

**Con un proxy LiteLLM delante**: una sola URL para los dos, y
`TEAMS_EMBED_BASE_URL` no hace falta.

#### Dar de alta bge-m3 en LiteLLM

Se usa **bge-m3** (1024 dimensiones, multilingüe). El detalle que importa:
`nomic-embed-text-v1.5` **es solo inglés** y con transcripciones en castellano
la recuperación se degrada mucho sin dar ningún error, así que no vale.

```yaml
# config.yaml de LiteLLM, junto al modelo de chat
model_list:
  - model_name: bge-m3
    litellm_params:
      model: openai/bge-m3           # el backend expone /v1/embeddings
      api_base: http://localhost:8081/v1
      api_key: none
```

Comprobar que responde antes de indexar:

```bash
curl http://localhost:4000/v1/embeddings -H "Authorization: Bearer $LITELLM_API_KEY" \
     -H "Content-Type: application/json" \
     -d '{"model": "bge-m3", "input": "prueba"}' | head -c 200
```

### c) Construir el índice

```bash
export LITELLM_API_KEY=...      # o pásala con --api-key
python indexar_teams.py --reconstruir
python indexar_teams.py --info  # debe decir "Busqueda: hibrida"
```

Desde entonces se mantiene solo: `summarize_teams.py` indexa cada reunión al
terminar, y `python indexar_teams.py` (sin flags) recupera lo que falte.

Detalles que ahorran una tarde:

- **`datos/indice.db` es desechable.** No contiene nada que no se pueda
  recalcular desde `meetings.db`; borrarlo y reconstruirlo es seguro.
- **Cambiar de modelo de embeddings obliga a `--reconstruir`**, y el CLI se
  niega a hacer otra cosa. Vectores de dos modelos en el mismo índice dan
  resultados sin sentido **sin dar ningún error**.
- Si el modelo que elijas exige prefijos de tarea (nomic, e5), ponlos en
  `TEAMS_EMBED_PREFIJO` y `TEAMS_EMBED_PREFIJO_CONSULTA`. bge-m3 no los lleva.
- La API abre el índice en **solo lectura**: el contenedor no lo escribe nunca,
  lo construye el pipeline en el host.
