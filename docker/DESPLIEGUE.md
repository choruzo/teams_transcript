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
scp -r api web autenticacion.py memoria.py summarize_teams.py glosario.py docker-compose.yml servidor:~/teams_transcript/
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
base. Si dice que **la base está en el esquema 1**, migrarla:

```bash
python3 memoria.py --migrar
```

(Antes, una copia: `cp datos/meetings.db datos/meetings.db.bak`.)

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

## Actualizar

| Qué cambió | Qué hay que hacer |
|---|---|
| `web/`, `api/`, `memoria.py`, `autenticacion.py` | `scp` de los ficheros + `docker compose restart api` |
| `summarize_teams.py`, `glosario.py` | igual: la API los monta porque reutiliza `renderizar_markdown` y los títulos de `TIPOS` |
| `requirements-api.txt` | `exportar.ps1` con una versión nueva, `scp`, `docker load`, actualizar el tag en `docker-compose.yml`, `docker compose up -d` |
| `docker-compose.yml` | `scp` + `docker compose up -d` |

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
