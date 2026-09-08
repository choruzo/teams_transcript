# Plan de interfaz: de "scripts" a "memoria del equipo consultable"

Redactado el 2026-09-08. **Fase D0 implementada** (2026-09-08); el resto es
propuesta.

Documento complementario de [`PLAN_MEJORAS.md`](PLAN_MEJORAS.md). Aquel define
el *motor* (glosario, JSON estructurado, SQLite, arrastres, consulta en
lenguaje natural). Este define la *cara*: una aplicación web local que consuma
—y corrija— lo que el motor produce.

## 0. Decisiones tomadas (2026-09-08)

| Cuestión | Decisión | Consecuencia en el plan |
|---|---|---|
| Papel de Three.js | **Accesorio.** Timeline, métricas, tablero y vista de reunión en **SVG 2D**; Three.js solo en la Constelación 3D | La aplicación debe funcionar entera con la pestaña 3D desactivada. La fase I9 es prescindible y va la última |
| Exposición | **Solo `localhost` + túnel SSH** | Sin autenticación propia, sin certificados, sin gestión de usuarios. `ports: 127.0.0.1:8080:8000`. Si algún día se abre a la LAN, la autenticación es requisito previo, no un añadido |
| Correcciones humanas frente al reproceso | **Tabla `overrides`**, reaplicada tras cada pasada del LLM | Requiere migración de esquema real (deuda D6) y sube `ESQUEMA_VERSION` a 2. Deja rastro de qué es humano y qué es del modelo |
| Máquina | **El servidor Linux con GPU**, donde ya viven la BD, las transcripciones y el audio | Los volúmenes se montan directos; LiteLLM queda en la misma máquina. Coherente con la decisión ya tomada en el plan del motor |
| Ingesta desde la web | **Completa**: subir un `.wav`, transcribir y resumir desde el navegador | Se mantiene la sección 3.8 en su alcance ambicioso: cola de trabajos, trabajador **fuera de Docker** (por la GPU) y progreso por SSE. Es la fase con más piezas móviles y va la última |
| Riesgos sin estado (D3) | **Redefinir la métrica**, no el esquema | La interfaz muestra "riesgos mencionados en el periodo", que es lo que el dato dice de verdad. No se promete "riesgos abiertos" |
| Tests | **Sí**: `unittest` de la stdlib sobre `memoria.py` y la API, contra una BD temporal | Cambio en el modo de trabajo del repo. Sin pytest ni dependencias nuevas. Prioridad en las escrituras y los `overrides` |
| Primer paso | **D0**, la deuda del motor, antes de escribir una línea de web | `uid` estable, rutas de audio, conexión de solo lectura y WAL en `memoria.py` |
| Despliegue en el servidor cerrado | **La imagen se construye y prueba en el portátil y se exporta con `docker save`**; en el servidor solo `docker load` | Nada de `build:` en el `docker-compose.yml`, tag versionado, `pull_policy: never`, imagen base por digest y versiones exactas (sección 5) |
| Qué va dentro de la imagen | **Solo Python y las dependencias.** El código de la aplicación va por *bind mount* | Cambiar el front cuesta un `scp` de kilobytes, no reexportar 200 MB. La imagen solo se rehace si cambia `requirements-api.txt` |

## 1. Objetivo

Hoy toda la información útil está en `datos/meetings.db` y en ficheros `.md`
sueltos, y se accede por SSH ejecutando scripts. El objetivo es que un jefe de
equipo pueda abrir una pestaña del navegador y responder de un vistazo:

- ¿Qué se decidió y qué pasó en cada reunión de las últimas semanas?
- ¿Qué acciones siguen abiertas y cuáles llevan estancadas demasiado tiempo?
- ¿Qué dijo Fulano sobre X el mes pasado? (preguntándolo en castellano)
- ¿Dónde se está yendo el esfuerzo del equipo?

Y, no menos importante, que pueda **corregir lo que el modelo se ha inventado o
ha entendido mal** sin editar SQL a mano.

### Principios que se heredan del plan del motor

- **Todo local.** Nada sale de la red: la API habla con SQLite en disco y con
  LiteLLM en la red interna. Sin CDNs en tiempo de ejecución, sin telemetría,
  sin fuentes externas.
- **`memoria.py` sigue siendo la única capa de datos.** La API **no escribe SQL
  suelto**; si necesita una consulta nueva, la consulta se añade a `memoria.py`
  y ahí se queda disponible para `ask_teams.py`, `report_teams.py` y la API por
  igual.
- **`memoria.py`, `glosario.py`, `summarize_teams.py` y `transcribe_teams.py`
  siguen siendo solo stdlib.** FastAPI, uvicorn y compañía se declaran en un
  `requirements-api.txt` **aparte**: el pipeline de transcripción no puede
  depender de que el servidor web esté instalado.
- **La interfaz es aditiva.** Todo lo que hoy funciona por línea de comandos
  debe seguir funcionando exactamente igual con la web apagada.

### Lo que este plan NO pretende

- No sustituye a Jira/Planner. La decisión de la sección 0 del plan del motor
  sigue vigente: no hay integraciones ni exportadores.
- No es multi-equipo ni multi-tenant. Una instalación, un equipo, una BD.
- No es una app de grabación. Grabar sigue siendo `record_teams.py` en el
  portátil Windows.

## 2. Arquitectura

```
                    navegador (localhost por defecto)
                              |
                        HTTP / SSE
                              |
              +---------------v----------------+
              |   contenedor  api  (FastAPI)   |
              |   uvicorn, python 3.12         |
              |   / -> StaticFiles(web/)       |
              |   /api -> routers              |
              |   importa memoria.py,          |
              |   ask_teams.py, report_teams.py|
              +------+------------------+------+
                     |                  |
          volumen ro/rw|                  | HTTP
            datos/    |                  |
      +--------------v---+        +------v---------------+
      |  meetings.db     |        |  LiteLLM :4000       |
      |  glosario.md     |        |  (fuera del compose) |
      |  grabaciones/    |        +----------------------+
      +------------------+
```

### Estructura de ficheros propuesta

```
teams_transcript/
  memoria.py               (+ consultas nuevas para la API)
  ask_teams.py             (Fase 4 del motor; la API lo importa, no lo duplica)
  report_teams.py          (Fase 5 del motor)
  api/                     NUEVO
    __init__.py
    main.py                app FastAPI, CORS, montaje de routers
    deps.py                conexión SQLite por petición, config
    modelos.py             modelos Pydantic (contrato de la API)
    rutas/
      reuniones.py
      acciones.py
      personas.py
      metricas.py
      busqueda.py
      chat.py              SSE, delega en ask_teams
      trabajos.py          ingesta y estado del pipeline (fase tardía)
  web/                     NUEVO
    index.html
    css/
    js/
      api.js               único punto que hace fetch
      vistas/              timeline.js, acciones.js, reunion.js, chat.js
      lib/                 three.min.js y demás, vendorizados (sin CDN)
  docker/                  NUEVO
    Dockerfile
    exportar.ps1           construye y empaqueta para el servidor cerrado
    DESPLIEGUE.md          los comandos a ejecutar alli
  docker-compose.yml       NUEVO
  dist/                    NUEVO  - .gitignore: el .tar de la imagen
  .gitattributes           NUEVO  - LF forzado en *.sh, Dockerfile, *.yml
  requirements-api.txt     NUEVO
  tests/                   NUEVO  - unittest de la stdlib, sin pytest
    test_memoria.py
    test_overrides.py
    test_api.py
```

### Decisiones técnicas y su porqué

| Decisión | Por qué |
|---|---|
| **Front sin framework** (HTML + CSS + JS con módulos ES) | Ya es la elección del usuario y encaja: sin build system, sin `node_modules`, sin cadena de suministro npm en una herramienta que maneja datos internos. El coste es escribir a mano el enrutado y el renderizado; a esta escala es asumible |
| **Librerías vendorizadas en `web/js/lib/`** | El despliegue objetivo es un servidor **sin acceso a internet** (ver `DEPLOY_OFFLINE.md`). Un `<script src="https://cdn...">` deja la aplicación en blanco allí |
| **SQLite en modo WAL** | Permite lecturas concurrentes de la API mientras `summarize_teams.py` escribe. Hay que activarlo (`PRAGMA journal_mode=WAL`) en `memoria.conectar()` |
| **La API abre la BD en solo lectura por defecto** | Los endpoints de escritura usan una conexión distinta y explícita. Reduce a cero la posibilidad de corromper el histórico con un `GET` mal escrito |
| **SSE y no WebSocket para el chat** | Unidireccional, sobre HTTP normal, atraviesa el proxy nginx sin configuración especial y se implementa en 20 líneas por ambos lados |
| **Un solo contenedor** (FastAPI sirviendo también los estáticos con `StaticFiles`) | Es lo coherente con el principio de dependencias mínimas del proyecto. nginx delante comprime mejor y separa ciclos de vida, pero es una pieza más que mantener y configurar para un puñado de ficheros estáticos servidos en la red local. Se añade **cuando haya un problema medido**, no antes |

### Nota sobre Three.js

Un timeline con métricas es información **bidimensional** (tiempo × categoría).
Llevarlo a WebGL lo hace más vistoso y objetivamente peor: el texto se ve
borroso, la accesibilidad desaparece, el zoom y el desplazamiento hay que
reimplementarlos, y en un portátil sin GPU decente va a tirones.

La recomendación de este plan es:

1. **La base es 2D**: SVG para el timeline y los gráficos (nítido, seleccionable,
   inspeccionable, ~200 líneas de JS sin librerías) o Canvas 2D si el volumen de
   elementos lo pidiera.
2. **Three.js se reserva para una vista donde la tercera dimensión aporte de
   verdad**: la *Constelación* de la sección 3.7 —personas, temas y acciones
   como nodos de un grafo de fuerzas en 3D, donde las agrupaciones se ven
   girando la escena y no se verían en un diagrama plano—. Es una vista
   secundaria, prescindible, y la aplicación debe funcionar entera con esa
   pestaña desactivada.

**Decidido así** (sección 0): Three.js es accesorio. La consecuencia práctica
es que la Constelación no puede ser el sitio donde vive ninguna información
única: todo lo que muestre debe poder consultarse también desde las vistas 2D.

## 3. Vistas y funcionalidad

### 3.1 Timeline de reuniones (vista principal)

Eje horizontal de tiempo, una marca por reunión, coloreada por tipo
(daily/workshop/retro/planning) y dimensionada por duración. Encima, bandas
apiladas con las métricas del periodo.

- Zoom y desplazamiento (semana / mes / trimestre).
- Al pasar el ratón: fecha, tipo, duración, número de acciones nacidas y
  cerradas, resumen en dos líneas.
- Al hacer clic: se abre la vista de reunión (3.3).
- **Carriles de acción**: cada acción abierta se dibuja como una barra
  horizontal que va desde la reunión donde nació hasta la última en que se
  mencionó, con las menciones como marcas intermedias. Es la representación
  visual directa de la Fase 2 del motor, y hace que "esto lleva cinco dailys
  abierto" se vea sin leer nada.
- Filtros persistentes en la URL (`?desde=&hasta=&persona=&tipo=`), para poder
  compartir un enlace a una vista concreta.

### 3.2 Métricas y salud del equipo

Panel con números que salen **de SQL, no del LLM** (deterministas, no
alucinables — mismo principio que la Fase 5 del motor):

| Métrica | Fuente | ¿Calculable hoy? |
|---|---|---|
| Acciones abiertas / en progreso / bloqueadas | `actions.estado` | Sí |
| Acciones **estancadas** (≥ 3 menciones sin cerrar) | `actions.menciones` + estado | Sí |
| Tasa de cierre por semana | `actions.cerrada_en` agrupado | Sí, con la salvedad D4 |
| Reparto de carga por persona | `updates` + `actions` por `persona_id` | Sí |
| Minutos de reunión por semana | `meetings.duracion_seg` | Parcial: NULL si no hubo `.srt` (D9) |
| Tiempo medio de cierre de una acción | `cerrada_en` − fecha de la reunión de origen | **No**: `cerrada_en` es la fecha de *proceso* (D4) |
| Riesgos **mencionados en el periodo**, por severidad y área | `risks` | Sí, con ese enunciado: los riesgos no tienen estado ni continuidad (D3) |
| Personas sin actualización reciente | `updates` vs. `meetings` recientes | **No**: no hay roster del equipo (D7) |
| Bloqueos recurrentes | `updates.bloqueos` repetidos entre reuniones | **No** de forma determinista: es texto libre (D8) |

Las tres últimas dependen de arreglar la deuda de la sección 10; no deben
prometerse en la interfaz hasta entonces. La de riesgos sí se muestra, pero
**con el enunciado exacto que el dato soporta**: "mencionados en el periodo",
nunca "abiertos" (decisión de la sección 0). Un riesgo que nadie volvió a
nombrar desaparece del recuento, y eso es información honesta sobre lo que se
habla en las reuniones, no sobre lo que sigue vigente.

**Diseñar para poco dato.** Hoy la base tiene un puñado de reuniones. Un
timeline con tres puntos y una media con n=3 no informan de nada: cada métrica
necesita un estado vacío digno y un mínimo de muestras por debajo del cual se
muestra el dato crudo en vez de una tendencia inventada.

**Alertas** en la parte alta: acciones estancadas, riesgos de severidad alta sin
mencionar en N reuniones, personas ausentes del histórico. Son las tres cosas
que justifican abrir la herramienta un lunes por la mañana.

### 3.3 Vista de reunión

- Cabecera: fecha, tipo, duración, modelos usados, asistentes detectados.
- El resumen renderizado (el mismo contenido del `.md`, generado desde
  `meetings.datos_json`, no releyendo el fichero).
- Secciones por persona, acciones nacidas, arrastres, riesgos.
- **Transcripción completa** navegable, con el hablante por segmento y las
  marcas de tiempo de `segments`.
- **Reproductor de audio sincronizado**: al hacer clic en un segmento, el audio
  salta a `segments.inicio`. Requiere servir el `.wav`/`.mp3` desde
  `grabaciones/` con soporte de *range requests*. Es la función que convierte
  "creo que el modelo entendió mal" en "escúchalo".
  - **Ojo**: `meetings.audio_path` guarda una **ruta absoluta del host** donde
    se ejecutó `summarize_teams.py` (`.resolve()` en `summarize_teams.py`), que
    dentro del contenedor no existe. Hace falta un remapeo de prefijos
    configurable (`AUDIO_RAIZ_HOST` → `AUDIO_RAIZ_CONTENEDOR`) o pasar a
    guardar rutas relativas. Lo mismo aplica a `transcript_path`.
- Enlace de descarga del `.md` y del `.srt`.

### 3.4 Tablero de acciones

La vista que hoy no existe en ninguna parte y donde vive el valor de gestión.

- Columnas por estado (abierta / en progreso / bloqueada / completada /
  abandonada), o tabla ordenable — a decidir al maquetar.
- Filtros por persona, antigüedad, texto.
- Cada tarjeta: descripción, responsable, reunión de origen, número de
  menciones, badge **ESTANCADA**, y el historial de qué reunión la tocó y con
  qué comentario.
- **Edición humana** (ver 3.6): cambiar estado, reasignar, editar la
  descripción, fusionar duplicados, archivar.

### 3.5 Chat sobre el histórico

Interfaz conversacional sobre la Fase 4 del motor (`ask_teams.py`).

- La API **importa** `ask_teams.py` y llama a su función de respuesta; no
  reimplementa la interpretación de la pregunta ni la recuperación. Si esa
  lógica acaba duplicada, el plan ha fallado.
- Respuesta en *streaming* (SSE) para que se vea escribir en vez de esperar 30
  segundos en blanco.
- **Citas navegables**: cada afirmación de la respuesta lleva su reunión y
  fecha (ya es un requisito del prompt de la Fase 4); en la web esas citas son
  enlaces que abren la vista de reunión posicionada en el segmento concreto.
  Esto es lo que separa un chat útil de uno en el que no se puede confiar.
- Historial de conversación en el cliente; sugerencias de preguntas frecuentes
  ("¿qué lleva más tiempo bloqueado?", "resumen de la semana").
- Botón para exportar la respuesta a Markdown.

### 3.6 Corrección humana (el LLM se equivoca)

Endpoints de escritura, cada uno respaldado por una función de `memoria.py`:

- **Reasignar hablantes**: cambiar el `persona_id` de un `SPEAKER_XX` en una
  reunión, propagándolo a los `segments` de esa reunión.
- **Personas y alias**: fusionar "Javi" y "Javier M.", dar de baja a alguien
  (`activo = 0`), añadir alias — hoy solo posible desde Python.
- **Acciones**: cambiar estado, editar descripción, reasignar responsable,
  fusionar dos acciones que son la misma, crear una a mano.
- **Glosario**: editar `datos/glosario.md` desde la web, con vista previa del
  bloque `<!-- whisper -->` y **aviso al superar los 700 caracteres** que
  `glosario.py` trunca. Añadir un término desde la vista de reunión al ver una
  palabra mal transcrita es el flujo natural, y hoy implica un SSH.

**Requisito transversal:** toda escritura manual debe quedar marcada como tal y
**sobrevivir al reproceso**. Hoy `crear_reunion(reemplazar=True)` borra y
reinserta la reunión, y `_deshacer_arrastres` revierte estados: una corrección
humana se perdería en el siguiente reprocesado.

**Decidido (sección 0): tabla `overrides`.** Cada corrección manual se guarda
como una fila independiente —qué entidad, qué campo, qué valor, quién y
cuándo— y se **reaplica automáticamente al final de cada pasada del LLM**, ya
venga de la web o de la línea de comandos. Consecuencias:

- El punto de reaplicación vive en `memoria.py` y lo invoca
  `summarize_teams.guardar_en_bd` después de insertar, no la API: si solo lo
  hiciera la web, procesar por SSH seguiría borrando las correcciones.
- Las anulaciones se referencian por identidad **estable**, no por el `id`
  autoincremental que cambia al reprocesar (deuda D1). Para una reunión, su
  `uid`; para una acción, hace falta decidir la clave —la más razonable es el
  par (uid de la reunión de origen, descripción normalizada)—.
- Sube `ESQUEMA_VERSION` a 2 y obliga a escribir el primer mecanismo de
  migración real del proyecto (deuda D6).
- Una anulación puede quedar **huérfana** si el reproceso hace desaparecer la
  fila que corregía (el modelo ya no detecta esa acción). No se borra en
  silencio: se conserva y se muestra como pendiente de revisión.

**Es el punto más delicado de todo el plan** y debe estar resuelto antes de
escribir el primer endpoint de escritura.

### 3.7 Constelación 3D (opcional, Three.js)

Grafo de fuerzas en tres dimensiones: personas, temas/áreas y acciones como
nodos; aristas por co-ocurrencia en reuniones. Girando la escena se ven las
agrupaciones reales del equipo (quién trabaja con quién, qué áreas están
aisladas), que en un diagrama plano se solapan hasta ser ilegibles.

- Vista secundaria: la aplicación funciona entera sin ella.
- Rendimiento acotado: unos cientos de nodos como máximo, con simulación de
  fuerzas parada tras converger.
- Alternativa si se descarta: un mapa de calor persona × área en SVG da el 80 %
  de la información con el 10 % del código.

### 3.8 Ingesta desde la web

**Decidido: alcance completo.** Se sube un `.wav` desde el navegador, se
transcribe y se resume, sin tocar la línea de comandos. Grabar sigue siendo
`record_teams.py` en el portátil.

Dos niveles, y conviene implementarlos en este orden porque el primero es
trivial y el segundo no:

1. **Re-resumir** una transcripción existente: botón en la vista de reunión que
   relanza `summarize_teams.py` sobre el mismo `.txt`, pudiendo cambiar
   `--tipo` y `--arrastres`. Segundos de LLM, sin GPU. Es además donde está la
   iteración real del día a día (afinar prompts y glosario).
2. **Subir y transcribir**: `.wav` → `transcribe_teams.py --diarize` →
   `summarize_teams.py`. Minutos de GPU.

Cómo:

- **Cola de trabajos en SQLite** (tabla `jobs`: estado, script, argumentos,
  código de salida, log). Un trabajo en curso a la vez: la GPU no se comparte.
- **El trabajador no va en Docker.** Corre en el host como servicio systemd,
  junto a los modelos, la caché de HuggingFace y el ffmpeg estático que ya
  configura `DEPLOY_OFFLINE.md`. Contenedorizar Whisper y pyannote significaría
  `--gpus all`, la imagen de CUDA y duplicar el entorno offline entero: mucho
  coste para cero beneficio.
- **La API solo escribe en `jobs`**; nunca lanza el subproceso ella misma. Una
  transcripción de una hora bloquearía el bucle de peticiones.
- **Progreso por SSE**, con el log en vivo y el resultado enlazado a la reunión
  creada. Los ficheros subidos aterrizan en `grabaciones/` con el mismo patrón
  de nombre (`AAAAMMDD_HHMMSS_mixed.wav`) del que depende `inferir_fecha`.
- **Límite de tamaño de subida** explícito y comprobación de que es un `.wav`
  decodificable antes de encolar: un fichero de una hora ronda los 300-600 MB.
- Depende de la deuda D1 (ids estables): hoy re-resumir cambia el id de la
  reunión y dejaría al usuario mirando un 404 de la página que acaba de pulsar.

**Es la fase con más piezas móviles del plan** —cola, proceso externo,
streaming, subida de ficheros grandes, GPU compartida— y por eso va la última:
todo lo demás tiene valor sin ella.

## 4. La API

Contrato REST, JSON, prefijo `/api`. Todos los endpoints de lectura aceptan
`desde`, `hasta` y paginación donde tenga sentido.

| Método | Ruta | Devuelve |
|---|---|---|
| GET | `/api/reuniones` | lista con filtros (`desde`, `hasta`, `tipo`, `persona`) |
| GET | `/api/reuniones/{id}` | reunión completa con `datos_json` renderizado |
| GET | `/api/reuniones/{id}/segmentos` | transcripción con tiempos y hablantes |
| GET | `/api/reuniones/{id}/audio` | el `.wav`, con *range requests* |
| GET | `/api/acciones` | filtros por estado, persona, estancamiento |
| PATCH | `/api/acciones/{id}` | estado, descripción, responsable |
| POST | `/api/acciones/{id}/fusionar` | absorbe otra acción duplicada |
| GET | `/api/personas` | con alias y actividad |
| PATCH | `/api/personas/{id}` | nombre, alias, alta/baja |
| POST | `/api/reuniones/{id}/hablantes` | corrige el mapeo etiqueta → persona |
| GET | `/api/metricas` | los agregados de 3.2, en un solo objeto |
| GET | `/api/buscar?q=` | FTS5 sobre `segments`, con resaltado y contexto |
| GET | `/api/glosario` / PUT | contenido del glosario, con validación de tamaño |
| POST | `/api/chat` | SSE; delega en `ask_teams.py` |
| GET | `/api/informes?desde=&hasta=` | Markdown de `report_teams.py` |
| GET | `/api/salud` | estado de la BD, de LiteLLM y versión del esquema |

Detalles no negociables:

- **Paginación obligatoria** en `segmentos` y `buscar`: una reunión de una hora
  son ~350 segmentos y crecerá.
- **Errores con cuerpo útil**, no solo un 500: el front tiene que poder decir
  "LiteLLM no responde" en vez de "algo ha fallado".
- **Sin CORS abierto**: mismo origen vía el proxy de nginx.

## 5. Docker y despliegue en un servidor sin internet

**Restricción de partida:** el servidor Ubuntu **no tiene salida a internet**.
La imagen se construye y se prueba en el portátil Windows (que sí la tiene) y
se exporta como fichero. En el servidor **nunca se ejecuta `docker build`**: no
podría descargar ni la imagen base ni las dependencias.

Condiciones confirmadas: el servidor tiene **Docker Engine y compose v2**, hay
**red interna** para `scp` (aunque no salida a internet) y **la estructura del
proyecto es la misma** en ambas máquinas, así que las rutas relativas del
`docker-compose.yml` valen tal cual. El portátil construye `linux/x86_64`, que
es la arquitectura del servidor.

### 5.1 El reparto: imagen estable, código móvil

La decisión que hace esto llevadero es **no meter el código en la imagen**:

| Qué | Dónde vive | Cómo se actualiza | Cada cuánto |
|---|---|---|---|
| Python + dependencias (FastAPI, uvicorn…) | dentro de la imagen | `docker save` → `scp` → `docker load` (~200 MB) | casi nunca: solo si cambia `requirements-api.txt` |
| `api/`, `web/`, `memoria.py`, `ask_teams.py`… | bind mount desde el disco del servidor | `scp` de ficheros de texto + reiniciar | a diario |
| `datos/`, `grabaciones/` | bind mount, nunca en la imagen | los genera el pipeline en el servidor | continuamente |

Así, cambiar una línea de CSS o un endpoint cuesta un `scp` de unos kilobytes,
no reexportar y transferir la imagen entera. **Nada del código de la aplicación
se hornea en la imagen**, ni siquiera como copia de respaldo: dos versiones del
mismo fichero (una dentro, otra montada encima) es una fuente de confusión
garantizada el día que el bind mount falle en silencio.

### 5.2 `docker-compose.yml`

Un solo servicio. Sin clave `build:`, que en el servidor solo puede fallar:

```yaml
# esbozo, no definitivo
services:
  api:
    image: teams-transcript-api:0.1.0   # tag fijo y versionado, nunca :latest
    pull_policy: never                  # no intentes ir a ningun registro
    environment:
      TEAMS_DB: /app/datos/meetings.db
      TEAMS_API_SOLO_LECTURA: "1"
      LITELLM_BASE_URL: http://host.docker.internal:4000/v1
      LITELLM_API_KEY: ${LITELLM_API_KEY}
      # La BD guarda rutas absolutas de la maquina que proceso la reunion;
      # dentro del contenedor el proyecto esta en /app (deuda D2, resuelta).
      TEAMS_RAIZ_ORIGEN: ${TEAMS_RAIZ_ORIGEN}
      TEAMS_RAIZ_LOCAL: /app
    volumes:
      - ./api:/app/api:ro
      - ./web:/app/web:ro
      - ./memoria.py:/app/memoria.py:ro
      - ./datos:/app/datos                # lectura y escritura: WAL, y I6
      - ./grabaciones:/app/grabaciones:ro
    ports: ["127.0.0.1:8080:8000"]
    extra_hosts: ["host.docker.internal:host-gateway"]   # LiteLLM, en Linux
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request;urllib.request.urlopen('http://localhost:8000/api/salud')"]
      interval: 30s
```

`pull_policy: never` y el tag versionado son deliberados: sin ellos, un
despliegue en el que la imagen no se cargó bien no falla claramente, sino que
se queda intentando contactar con Docker Hub hasta agotar el tiempo de espera.

**`:latest` está prohibido.** Con `docker load` no hay forma de saber qué
versión trajo el tar si todas se llaman igual, y una imagen vieja que se queda
en el servidor pasa desapercibida.

### 5.3 El paquete de exportación

Un script (`docker/exportar.ps1`) que produce, en `dist/` (fuera del control de
versiones):

```
teams-transcript-api-0.1.0.tar     imagen: docker save
SHA256SUMS.txt                     para verificar la transferencia
DESPLIEGUE.md                      los cuatro comandos a ejecutar en el servidor
```

Y en el servidor, todo el procedimiento:

```bash
sha256sum -c SHA256SUMS.txt
docker load -i teams-transcript-api-0.1.0.tar
docker compose up -d
docker compose logs -f api        # comprobar que arranca
```

Detalles que evitan sorpresas:

- **`--platform linux/amd64` explícito** en el build, sin depender del valor
  por defecto del Docker Desktop del portátil.
- **Imagen base fijada por digest** (`python:3.12-slim@sha256:…`), no por
  etiqueta: `3.12-slim` apunta a una imagen distinta cada mes, y en un entorno
  cerrado la reproducibilidad es lo único que permite reconstruir con garantías
  seis meses después.
- **Versiones exactas** en `requirements-api.txt` (`==`, no `>=`), por lo
  mismo.
- **Sin `apt-get install`** en el `Dockerfile` si se puede evitar: la API solo
  necesita Python y SQLite, que ya vienen en la imagen base.
- **Imagen sin GPU ni PyTorch**: la API lee SQLite y hace HTTP. Meter `torch`
  multiplicaría por veinte el tamaño del fichero a transferir.
- El `.tar` y `dist/` van a `.gitignore`.

### 5.4 Probar aquí lo que correrá allí

El objetivo es que el portátil sea un entorno de pruebas fiel, y hay dos
diferencias que no lo son:

1. **SQLite en modo WAL sobre un bind mount de Windows.** Docker Desktop expone
   el disco de Windows al contenedor a través de una capa de red (9p/virtiofs)
   que no da las mismas garantías de bloqueo que un bind mount nativo de Linux.
   Puede fallar aquí y funcionar allí, o —peor— parecer que funciona. **Las
   pruebas de concurrencia con la base de datos no son concluyentes en
   Windows**; hay que repetirlas en el servidor tras el primer despliegue.
2. **Fin de línea.** El repo se edita en Windows y git convierte a CRLF al
   sacar los ficheros a disco. Un `.sh` con CRLF dentro del contenedor falla
   con un `bad interpreter` incomprensible. Conviene un `.gitattributes` que
   fuerce LF en `*.sh`, `Dockerfile` y `*.yml`.

Y una que sí es fiel y conviene aprovechar: **la base de datos real está en
este portátil** (una reunión, 145 segmentos), con rutas de audio en formato
Windows. Es el caso de prueba perfecto para el remapeo `TEAMS_RAIZ_*`, porque
es exactamente lo que la API se encontrará al leer una fila procesada en otra
máquina.

### 5.5 Seguridad del paquete

- **`LITELLM_API_KEY` nunca en la imagen ni en el repo**: fichero `.env` en el
  servidor, en `.gitignore`.
- **`datos/` jamás dentro de una imagen.** Contiene el texto literal de las
  reuniones. Una imagen es un fichero que se copia, se comparte y se olvida en
  un disco; la base de datos se monta, no se hornea.
- Publicar en `127.0.0.1:8080`, no en `0.0.0.0` (sección 6).

## 6. Seguridad y privacidad

Este plan **amplía significativamente el riesgo** respecto al estado actual:
hoy los datos están en ficheros de un servidor al que se accede por SSH;
después estarán en un puerto HTTP.

- **Decidido: se escucha solo en `localhost`** y se accede por túnel SSH
  (`ssh -L 8080:127.0.0.1:8080 servidor`). No hay autenticación propia,
  certificados ni gestión de usuarios: la barrera es la misma que ya protege el
  servidor. Es la opción segura y no cuesta nada.
- **Abrirlo a la LAN más adelante no es cambiar un puerto.** Requeriría
  autenticación (*basic auth* sobre HTTPS con certificado interno, como mínimo)
  antes de tocar el `bind`. Sin ella, cualquiera de la red leería la
  transcripción literal de todas las reuniones del equipo y podría escuchar el
  audio. Queda anotado aquí para que la tentación de "solo por probar" tenga
  una respuesta escrita.
- **Un único flag global de solo lectura** (`TEAMS_API_SOLO_LECTURA=1`) que
  desactive todos los endpoints de escritura, en vez de un sistema de roles por
  usuario: con un equipo y una instalación, los roles son sobreingeniería.
- **Copias de seguridad.** En cuanto la interfaz permite editar, `meetings.db`
  pasa a ser un punto único de fallo con datos irrecuperables: el audio original
  puede haberse borrado ya por la política de retención. Una copia diaria del
  fichero (`VACUUM INTO`, que es consistente aunque haya escrituras en curso) es
  el mínimo.
- **Aplica lo dicho en la sección 6 del plan del motor**: consentimiento de los
  participantes, política de retención, y la utilidad `--olvidar <persona>`
  —que en esta interfaz debería tener botón—.
- El audio servido por `/api/reuniones/{id}/audio` es el contenido más sensible
  del sistema. Debe requerir la misma autenticación que el resto y no ser
  cacheable por intermediarios.

## 7. Cuestiones abiertas

Las ocho cuestiones de partida están resueltas en la sección 0. Quedan
pendientes de decidir, y ninguna bloquea el arranque:

1. **Clave estable de una acción** para la tabla `overrides` (sección 3.6). El
   `uid` resuelve la identidad de una reunión; el de una acción hay que
   definirlo. Se decide al implementar I6, con datos reales delante.
2. **Retención y `--olvidar <persona>`**: sigue abierta desde la sección 6 del
   plan del motor. Con una interfaz, ese borrado debería tener botón.
3. **Formato de las tarjetas del tablero** (3.4): columnas tipo kanban o tabla
   ordenable. Se decide maquetando, no en un documento.
4. **Idioma de la interfaz**: castellano, en coherencia con el resto del
   proyecto, salvo indicación contraria.

## 8. Orden de implementación recomendado

| Orden | Fase | Contenido | Esfuerzo | Depende de |
|---|---|---|---|---|
| ~~0~~ | ~~**D0 — Deuda previa**~~ **hecha** | D1 (uid estable), D2 (remapeo de rutas), D5 (conexión de solo lectura), WAL y los primeros tests | Bajo | Nada |
| 1 | **I0 — Esqueleto** | FastAPI + Docker + `/api/salud` + `/api/reuniones` + una página que las liste | Bajo | D0 |
| 2 | **I1 — Timeline y métricas** | 3.1 y 3.2 en SVG, filtros en la URL | Medio | I0 |
| 3 | **I2 — Vista de reunión** | 3.3 sin audio: resumen, secciones, transcripción | Bajo | I0 |
| 4 | **I3 — Tablero de acciones** | 3.4 en solo lectura, con badge de estancamiento | Bajo | I0 |
| 5 | **I4 — Búsqueda** | FTS5 con resaltado, atajo global de teclado | Bajo | I0 |
| 6 | **I5 — Chat** | 3.5 con SSE y citas navegables | Medio | **Fase 4 del motor (`ask_teams.py`)** |
| 7 | **I6 — Corrección humana** | 3.6 completo: tabla `overrides` reaplicada, migración a esquema 2, y sus tests | **Alto** | I2, I3, D0 |
| 8 | **I7 — Audio sincronizado** | reproductor de 3.3 | Bajo | I2 |
| 9 | **I8 — Informes** | 3.2 ampliado + descarga de Markdown | Bajo | **Fase 5 del motor** |
| 10 | **I9 — Constelación 3D** | 3.7, si se decide hacerla | Medio | I1 |
| 11 | **I10a — Re-resumir** | primer nivel de 3.8: relanzar el resumen sobre un `.txt` existente | Bajo | I2, D0 |
| 12 | **I10b — Subir y transcribir** | segundo nivel de 3.8: cola `jobs`, trabajador systemd en el host, SSE de progreso | **Alto** | I10a |

**Camino más corto a algo útil:** D0 + I0 + I1 + I2 + I3. Con eso ya se ve el
histórico completo, las métricas y el estado real de las acciones, que es el
90 % del valor de gestión. El chat (I5) es lo más llamativo pero depende de una
fase del motor que aún no existe, y la corrección humana (I6) es lo más
valioso a medio plazo y lo más difícil de hacer bien.

**Dependencia crítica con el otro plan:** I5 no debe empezarse antes que la
Fase 4 del motor. Si se hace al revés, la lógica de interpretación y
recuperación acabará dentro de `api/rutas/chat.py` y `ask_teams.py` nacerá
duplicándola.

## 9. Criterios de aceptación por fase

- **D0**: reprocesar una transcripción ya registrada **conserva su `uid`**
  (aunque el `id` cambie), la API puede leer la BD mientras
  `summarize_teams.py` escribe, y un `GET` no puede modificar el fichero. Con
  sus tests.
- **I0**: en el portátil, `docker compose up` levanta la aplicación y la página
  lista las reuniones reales de `datos/meetings.db`. Y, lo que de verdad
  importa: el paquete exportado se carga en el servidor con `docker load` y
  arranca **sin ejecutar `build` ni contactar con ningún registro** —
  comprobable desconectando la red del portátil antes de probarlo—, sirviendo
  el audio de una reunión cuya fila lleva rutas de otra máquina.
- **I1**: el timeline muestra las reuniones del último mes con sus carriles de
  acción; una acción con tres menciones se ve claramente como una barra que
  cruza tres reuniones.
- **I2**: desde el timeline se llega a una reunión y se lee la transcripción
  completa con hablantes, sin abrir ningún fichero.
- **I3**: el tablero muestra las acciones abiertas con su marca `ESTANCADA`,
  coincidiendo con lo que produce hoy la sección "Arrastres" del `.md`.
- **I4**: buscar un término del glosario devuelve los segmentos donde aparece,
  con acentos indiferentes, y enlaza a su reunión.
- **I5**: una pregunta sobre el histórico se responde en streaming, con citas
  que enlazan al segmento exacto, y responde "no consta" cuando no lo sabe.
- **I6**: corregir un hablante y cerrar una acción a mano; **reprocesar esa
  misma reunión y comprobar que ambas correcciones siguen ahí**.
- **I7**: hacer clic en una frase de la transcripción reproduce ese instante
  del audio.
- **I10b**: subir un `.wav` desde el navegador produce, sin tocar la consola,
  una reunión completa en la base con su transcripción, su resumen y sus
  arrastres, y el progreso se ha podido seguir en vivo.

## 9 bis. Resultado de D0 (2026-09-08)

Ficheros tocados: `memoria.py` y `tests/test_memoria.py` (nuevo). Ni
`summarize_teams.py` ni `transcribe_teams.py` han necesitado cambios: la
identidad estable se resuelve entera dentro de `crear_reunion`.

Resuelto: **D1** (`meetings.uid`), **D2** (`ruta_local`), **D5**
(`conectar(solo_lectura=True)`), **D6** (mecanismo de migración, estrenado con
la 1 → 2) y WAL. Quedan abiertas D3, D4, D7, D8 y D9, según lo decidido en la
sección 0.

Decisiones tomadas al implementar:

1. **Se conserva también el `id`, no solo el `uid`.** El plan solo pedía el
   `uid`, pero reinsertar la fila con el `id` anterior cuesta una línea y evita
   que cualquier referencia interna quede colgando. La identidad de una reunión
   pasa a sobrevivir al reprocesado por dos vías independientes.
2. **El `uid` se deriva del nombre del fichero, no de la ruta completa**
   (`20260907_090437_mixed`). Mover `grabaciones/` de sitio, o procesar la
   misma transcripción en otra máquina, no debería cambiar la identidad de la
   reunión. El precio es que dos ficheros con el mismo nombre en carpetas
   distintas colisionan; `_uid_disponible` los desempata con un hash corto de
   la ruta.
3. **`PRAGMA query_only` en vez de la URI `mode=ro`.** Es el detalle que habría
   costado una tarde de depuración más adelante: con la base en WAL, una
   conexión abierta como `mode=ro` no puede crear el fichero `-shm` que SQLite
   necesita para leer, y falla **justo cuando nadie más está escribiendo**, que
   es el caso normal. `query_only` da la misma garantía sin ese problema. Hay
   un test dedicado a ese escenario.
4. **La migración va antes de `_ESQUEMA`, no después.** Lo destapó el propio
   test: `_ESQUEMA` crea el índice UNIQUE sobre `meetings.uid`, y en una base
   v1 esa columna todavía no existe.

Verificado:

- **19 tests, todos en verde** (`python -m unittest discover -s tests`).
  Cubren identidad estable, colisión de nombres, solo lectura, WAL, remapeo de
  rutas Windows→Linux, migración e idempotencia de los arrastres.
- **La prueba de extremo a extremo de la Fase 2 sigue dando lo mismo**: dos
  dailys con el LLM simulado, arrastres aplicados, reproceso idempotente y
  `--sin-arrastres` revirtiendo. La reunión reprocesada tres veces conserva
  `id = 2` y su `uid`; antes de D0 habría ido a parar a los ids 3 y 4.
- **Migración probada sobre una copia de la base real** (1 reunión, 17
  acciones, 145 segmentos): pasa a `user_version = 2`, todas las filas
  intactas, `uid` asignado, FTS5 sincronizada (145) e `integrity_check` a `ok`.

**Pendiente, menor:** las **acciones** no tienen todavía identidad estable —al
reprocesar se borran y se reinsertan con id nuevo—. No hace falta hasta I6, y
la clave adecuada (probablemente el par `uid` de la reunión de origen +
descripción normalizada) se decide entonces, con datos reales delante. Es la
cuestión abierta 1.

---

## 10. Deuda que este plan destapa en el motor

Al contrastar las vistas propuestas contra el esquema real de
`datos/meetings.db` aparecen nueve puntos que **no son trabajo de interfaz**:
son limitaciones del motor que la línea de comandos disimula y una web deja al
descubierto. Conviene arreglarlos en `memoria.py` / `summarize_teams.py`, no
parchearlos en la API.

| # | Problema | Dónde | Bloquea |
|---|---|---|---|
| ~~D1~~ | ~~**Los ids de reunión no son estables**~~ **resuelta**: `meetings.uid`, y el `id` también se conserva | `memoria.crear_reunion` | I2, I5, 3.8 |
| ~~D2~~ | ~~**Rutas absolutas del host**~~ **resuelta**: `memoria.ruta_local` con `TEAMS_RAIZ_ORIGEN`/`TEAMS_RAIZ_LOCAL` | `summarize_teams.guardar_en_bd` | I7 |
| D3 | **Los riesgos no tienen estado ni continuidad** | tabla `risks`: sin `estado`, sin arrastres, borrada en cascada | nada: **se asume**, redefiniendo la métrica (sección 0) |
| D4 | **`cerrada_en` es la fecha de proceso, no la del cierre** | `memoria.insertar_acciones` usa `date('now')` | tiempo medio de cierre |
| ~~D5~~ | ~~**No hay conexión de solo lectura**~~ **resuelta**: `conectar(solo_lectura=True)` con `query_only` | `memoria.conectar` | I0 |
| ~~D6~~ | ~~**No hay migraciones reales**~~ **resuelta**: `_migrar`, estrenada con la 1 → 2 | `memoria._inicializar` | I6 (tabla `overrides`) |
| D7 | **No hay roster del equipo** | `personas` se puebla sola desde el LLM; `personas.activo` no lo usa nadie | "personas sin actualización" |
| D8 | **`updates.bloqueos` es texto libre** | detectar recurrencia exige el LLM, y deja de ser un número determinista | "bloqueos recurrentes" |
| D9 | **`duracion_seg` es NULL sin `.srt`** | sale del último `fin` de los segmentos | minutos/semana |

**D1 era el más importante y el menos evidente** (ya resuelto, ver 9 bis). Toda
la interfaz cuelga de la URL de una reunión: enlaces guardados, vistas
filtradas compartidas por chat y, sobre todo, las citas que el chat genera para
justificar sus respuestas. Si reprocesar una transcripción cambiara el
identificador, todo eso apuntaría a un 404 —y las citas del chat, que son lo
que hace fiable la Fase 4, se convertirían en enlaces rotos—.

**D4, D7 y D8 no bloquean nada visual**, pero invalidan tres de las métricas de
la sección 3.2. La decisión honesta es no mostrarlas hasta que el dato exista,
en vez de calcular algo aproximado y presentarlo como si fuera firme. **D3 es
distinto: se asume tal cual** y la métrica se reescribe para decir exactamente
lo que el dato soporta (sección 0). Si algún día los riesgos merecen el mismo
tratamiento que las acciones —estado, menciones y arrastres—, es una fase
hermana de la Fase 2 en el plan del motor, no trabajo de interfaz.

Cambios menores del mismo grupo:

- ~~**`journal_mode=WAL`**~~ **hecho en D0**: la API puede leer mientras
  `summarize_teams.py` escribe. Válido con un *bind mount* local; **no** si
  `datos/` acabara en NFS.
- **`date('now')` en SQLite es UTC**: en UTC+2, una acción cerrada a las 00:30
  se fecha el día anterior.
- **`meetings.fecha` no tiene hora**: dos reuniones el mismo día no se pueden
  ordenar entre sí en el eje temporal del timeline.
- **La fuente de verdad es la base de datos**, y el `.md` pasa a ser un
  artefacto de exportación. Conviene declararlo ahora: en cuanto la web permita
  corregir datos, el `.md` generado antes queda obsoleto y no se regenera solo.
- **Tests (decidido).** El repo no tiene ninguno y hasta ahora era defendible:
  scripts que se ejecutan a mano y se verifican mirando la salida. Una API con
  endpoints de escritura sobre el histórico cambia el cálculo. Se introduce
  `tests/` con `unittest` de la **stdlib** —sin pytest, en coherencia con el
  resto del proyecto— contra una BD temporal, en la línea de lo que ya se hizo
  a mano para verificar las fases 1 y 2. Prioridad, por este orden:
  1. `memoria.py`: arrastres, idempotencia del reproceso y `overrides`.
  2. Los endpoints de escritura de la API.
  3. La migración de esquema 1 → 2, sobre una copia de una BD real.

  Se escriben **con** cada fase, no al final: retroajustar tests a `memoria.py`
  entero es un proyecto en sí mismo y no se hará. **Estrenado en D0** con
  `tests/test_memoria.py` (19 pruebas), que ya destapó un fallo de orden en la
  migración antes de que llegara a una base real.
