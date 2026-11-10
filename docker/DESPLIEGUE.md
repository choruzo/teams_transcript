# Desplegar la interfaz web en el servidor sin internet

El servidor Ubuntu **no tiene salida a internet**, así que allí nunca se
ejecuta `docker build`: no podría descargar ni la imagen base ni las
dependencias. La imagen se construye en el portátil y se lleva como fichero.

Requisitos en el servidor: Docker Engine y el plugin `compose` v2, y el
proyecto en la misma estructura de directorios que en el portátil.

## 1. En el portátil (con internet)

```powershell
.\docker\exportar.ps1
```

Deja en `dist\` la imagen (`teams-transcript-api-0.1.0.tar`, unos 52 MB) y
`SHA256SUMS.txt`.

## 2. Transferir

Dos cosas distintas, con ritmos distintos:

```powershell
# La imagen: solo cuando cambia requirements-api.txt. Rara vez.
scp dist\teams-transcript-api-0.1.0.tar dist\SHA256SUMS.txt servidor:~/teams_transcript/dist/

# El código: cada vez que se toca el front o la API. Son kilobytes.
scp -r api web autenticacion.py memoria.py summarize_teams.py glosario.py report_teams.py docker-compose.yml servidor:~/teams_transcript/
```

## 3. En el servidor

```bash
cd ~/teams_transcript

# Verificar que el fichero llegó entero
cd dist && sha256sum -c SHA256SUMS.txt && cd ..

# Cargar la imagen (no descarga nada)
docker load -i dist/teams-transcript-api-0.1.0.tar

# Configuración local: la ruta del proyecto tal como aparece dentro de la BD
cp .env.ejemplo .env
nano .env            # TEAMS_RAIZ_ORIGEN=/home/tu_usuario/teams_transcript

# Arrancar
docker compose up -d
docker compose logs -f api
```

## 4. Comprobar

```bash
curl -s localhost:8080/api/salud | python3 -m json.tool
```

Debe responder `"base_accesible": true` y el número de reuniones que hay en la
base. Si dice que **la base está en un esquema anterior**, migrarla:

```bash
# Antes, una copia
cp datos/meetings.db datos/meetings.db.bak

# Desde el contenedor: usa el mismo volumen y el mismo usuario que la API
docker compose exec -T api python memoria.py --migrar
```

(El comando también funciona en el host mientras el usuario tenga escritura en
`datos/`; si falla con `attempt to write a readonly database`, usa la forma del
contenedor, que es la que siempre tiene permiso.)

## 5. Acceder desde el portátil

La aplicación escucha **solo en `127.0.0.1`**, así que desde el servidor no se
ve nada por la red. Se accede por túnel SSH:

```powershell
ssh -L 8080:127.0.0.1:8080 servidor
```

Y luego <http://localhost:8080> en el navegador del portátil. La primera vez
pide entrar: el usuario inicial es `admin` (la clave sale de `TEAMS_ADMIN_CLAVE`
en `.env`; sin definirla es `admin` y el log avisa). Cámbiala sin arrancar la
API con:

```bash
docker compose exec api python autenticacion.py --cambiar-clave admin
```

La base de usuarios vive en `datos/usuarios.db`, junto a `meetings.db` y dentro
del volumen: no viaja en la imagen ni en las copias del histórico.

Abrirlo a la red interna **exige autenticación primero** (`PLAN_INTERFAZ.md`,
sección 6): el login ya existe, pero sin él cualquiera de la red leería la
transcripción literal de todas las reuniones.

## 5 bis. Verificación hecha en producción (2026-11-10)

Estado comprobado en el servidor tras desplegar la Fase 8 y I11: imagen
`teams-transcript-api:0.2.0` (con `sqlite-vec` dentro), esquema **5** en
`meetings.db`, login activo y **139 acciones con vector** en `indice.db`
(`bge-m3`, 1024 dimensiones) para las sugerencias de duplicados. La API
responde con sesión `GET /api/sugerencias` → `disponible: true` y 6 parejas, y
el portal sirve `/app5/`, `/app5/acciones.html`, `/app5/reunion.html` y
`/login.html` por el 8443.

Antes de migrar o indexar se dejó una copia en
`backups/despliegue-20261110/` (`meetings.db` e `indice.db`). Los dos comandos
son idempotentes y hoy no tienen nada que hacer:

```bash
cd ~/teams_transcript

# 1. Esquema: responde "ya estaba en el esquema 5, nada que hacer."
docker compose exec -T api python memoria.py --migrar

# 2. Indice: responde "14 al dia" y "139 acciones al dia, 0 nuevas, 0 retiradas"
docker compose exec -T api python - --acciones < indexar_teams.py
```

El paso 2 va por `stdin` a proposito: `indexar_teams.py` **no** está montado en
el contenedor (no lo necesita la API), y pasarlo así evita copiarlo dentro. El
directorio de trabajo del contenedor es `/app`, que es donde viven `memoria.py`,
`rag.py` y `llm.py`, así que los imports resuelven. `-T` es lo que impide que
Compose asigne una TTY y rompa la redirección.

Para repetir la verificación funcional hace falta sesión:

```bash
PW=$(sed -n 's/^TEAMS_ADMIN_CLAVE=//p' .env)
curl -s -c /tmp/ck.txt -H 'Content-Type: application/json' \
     -d "{\"usuario\":\"admin\",\"clave\":\"$PW\"}" localhost:8080/api/login
curl -s -b /tmp/ck.txt localhost:8080/api/sugerencias
curl -s -b /tmp/ck.txt -X POST localhost:8080/api/logout

# Lo mismo a través del portal (certificado autofirmado)
for p in /app5/ /app5/acciones.html /app5/reunion.html /login.html; do
  printf '%s -> ' "$p"; curl -sk -o /dev/null -w '%{http_code}\n' "https://127.0.0.1:8443$p"
done
```

## 6. Subidas desde la web (I10b)

La web puede aceptar un `.wav` y lanzar el pipeline, pero **el trabajador no va
en Docker**: necesita la GPU, el venv de Whisper y `systemctl` para parar el
`llama-server`. Corre como servicio de usuario en el host.

### 6.1 Preparar el volumen y encender el flag

El contenedor corre como **uid 1000 (agent)**, así que el directorio de staging
tiene que existir antes y pertenecer a ese usuario. Si lo crea Docker al vuelo,
queda de root y la API no puede escribir:

```bash
cd ~/teams_transcript
mkdir -p subidas
ls -ld subidas          # debe ser agent:agent
```

En `.env`, encender las subidas:

```bash
TEAMS_SUBIDAS=1
```

`docker-compose.yml` ya monta `./subidas:/app/subidas` y
`./trabajos.py:/app/trabajos.py:ro`, y define `TEAMS_TRABAJOS_DB`. Como cambian
los volúmenes, hay que recrear el contenedor (no basta `restart`):

```bash
docker compose up -d
docker compose exec -T api python -c "import os;print(os.access('/app/subidas', os.W_OK))"
# debe imprimir True
```

### 6.2 Nginx: el 413 y el streaming

Sin esto, cualquier trozo de 16 MB muere con **413 Request Entity Too Large**
(el límite por defecto de nginx es 1 MB) y el SSE del progreso llega de golpe.
`proxy/portal` ya trae el bloque preparado; hay que copiarlo y recargar:

```bash
sudo cp proxy/portal /etc/nginx/sites-available/portal
sudo nginx -t && sudo systemctl reload nginx
```

### 6.3 El trabajador

Variables del host (no las del contenedor): `localhost` y `127.0.0.1` son el
servidor; `host.docker.internal` solo existe dentro de Docker.

```bash
cp .env.trabajador.ejemplo .env.trabajador
nano .env.trabajador       # rellena LITELLM_API_KEY y revisa la URL de embeddings
```

Instalación como **servicio de usuario** (`agent` ya tiene `Linger=yes`, así que
arranca al boot sin sesión abierta):

```bash
mkdir -p ~/.config/systemd/user
cp trabajador-teams.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now trabajador-teams
systemctl --user status trabajador-teams
journalctl --user -u trabajador-teams -f
```

La unidad pasa `--servicio llama-qwen3-8.service`: el pipeline lo para antes de
Whisper y lo vuelve a arrancar en su `finally`. `sudo -n` solo tiene permiso
para `start`/`stop` de esa unidad, que es exactamente lo que se usa.

### 6.4 Comprobar

1. `curl -s localhost:8080/api/trabajos/estado` (con sesión) debe devolver
   `disponible: true` y `trabajador: true`.
2. Subir un `.wav` de un minuto desde la página **Subir**.
3. Seguir el progreso: estado, paso y log. Al terminar debe aparecer el enlace
   a la reunión.
4. Comprobar que el chat vuelve a responder después del trabajo
   (`systemctl status llama-qwen3-8.service`).

### 6.5 Apagar las subidas

```bash
# .env: TEAMS_SUBIDAS=0
docker compose up -d
systemctl --user disable --now trabajador-teams
```

Los trabajos que ya estén en la cola se quedan ahí; la página lo dice. No se
borra ningún audio.

### 6.6 Verificación hecha en producción (2026-11-10)

- **Corte y reanudación**: 19,2 MB por trozos, 12 KB de basura añadidos al
  `.part` a propósito, reanudación y `sha256` final idéntico al original. La
  cancelación en cola responde y borra el parcial.
- **Pipeline real**: clip de 60 s → 12 segmentos, 3 acciones, 1 riesgo,
  `_resumen.md` y 1 fragmento indexado; `llama-qwen3-8` quedó activo. El
  resumen de un clip tan corto tardó varios minutos con `qwen3.8-27b`; es
  normal. La reunión de prueba se retiró después con copia en
  `backups/pre-cleanup-i10b/`.
- **Bug de rutas corregido**: la cola guarda solo el nombre del `.part`.
  Reinicia el contenedor y el trabajador tras actualizar `trabajos.py` y
  `trabajador_teams.py`.
- **Nginx recargado y verificado (2026-11-10)**: `/app5/`,
  `/app5/subir.html` y `/subir.html` responden 200 con sesión, y un `PUT` de
  16 MB por `https://…:8443/api/trabajos/.../partes/0` se acepta con
  `recibidos: 16777216`. Queda pendiente abrirlo en un navegador real; el JS
  está comprobado con Node y la API/SSE con `curl`.

## Actualizar

| Qué cambió | Qué hay que hacer |
|---|---|
| `web/`, `api/`, `memoria.py`, `autenticacion.py` | `scp` de los ficheros + `docker compose restart api` |
| `trabajos.py`, `web/subir.html`, `web/js/subir.js` | `scp` + `docker compose restart api`. El trabajador los lee del host; si cambia `trabajos.py`, reinicia también `systemctl --user restart trabajador-teams` |
| `trabajador_teams.py`, `trabajador-teams.service`, `.env.trabajador.ejemplo` | `scp` al host; si cambia la unidad, `cp` a `~/.config/systemd/user/`, `daemon-reload` y `restart` |
| `summarize_teams.py`, `glosario.py` | igual: la API los monta porque reutiliza `renderizar_markdown` y los títulos de `TIPOS` |
| `report_teams.py` | igual: `/api/informes` es una capa fina sobre él. Sin el montaje, la página del informe sigue abriéndose pero el endpoint da un 503 explicativo |
| `requirements-api.txt` | `exportar.ps1` con una versión nueva, `scp`, `docker load`, actualizar el tag en `docker-compose.yml`, `docker compose up -d` |
| `docker-compose.yml` | `scp` + `docker compose up -d` |
| Esquema de la BD (`ESQUEMA_VERSION`) | copia + `docker compose exec -T api python memoria.py --migrar`; después, `docker compose exec -T api python - --acciones < indexar_teams.py` |


Al subir de versión, la imagen antigua se queda ocupando disco:
`docker image rm teams-transcript-api:0.1.0` cuando la nueva funcione.

## Si algo va mal

```bash
docker compose logs api                  # el error suele estar aquí
docker compose exec api ls -la /app/datos   # ¿está montado el volumen?
curl -s localhost:8080/api/salud         # diagnostica sin abrir una shell
```

`/api/salud` responde **aunque la base no se pueda abrir**, y explica por qué:
está pensado justo para depurar a ciegas en un servidor sin navegador.

Errores frecuentes:

- **`pull access denied` / se queda intentando descargar**: la imagen no se
  cargó. Comprueba `docker image ls | grep teams-transcript-api` y que el tag
  coincida exactamente con el del `docker-compose.yml`.
- **`base_accesible: false`**: el volumen no está donde se cree. Comprueba que
  `datos/meetings.db` existe en el host y que `docker compose exec api ls
  /app/datos` lo ve.
- **La página carga pero no hay audio (fase I7)**: `TEAMS_RAIZ_ORIGEN` no
  coincide con el prefijo real de las rutas guardadas en la base. Míralo con
  `python3 memoria.py --info` y con una consulta a `meetings.audio_path`.
