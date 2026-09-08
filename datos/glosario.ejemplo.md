# Glosario del proyecto

Plantilla. Copia este fichero a `datos/glosario.md` y rellénalo con el
vocabulario real de tu equipo. `datos/glosario.md` **no se versiona** (contiene
nombres de personas y jerga interna); esta plantilla sí.

Sirve para dos cosas:

1. La sección marcada entre `<!-- whisper -->` y `<!-- /whisper -->` se pasa a
   Whisper como `initial_prompt`, lo que sesga el reconocimiento hacia estos
   términos y evita errores del tipo "Goverity" por "Coverity".
2. El documento **entero** se inyecta en el prompt del LLM que resume, para que
   corrija en el resumen lo que Whisper haya transcrito mal.

La sección de Whisper está limitada a unos 700 caracteres (~224 tokens, tope
del `initial_prompt`). Deja ahí solo lo más importante: nombres del equipo y
las siglas que más se repiten. El resto del glosario no tiene límite.

<!-- whisper -->
Nombre Apellido, Otro Nombre, Tercera Persona
Coverity, Jira, Planner, non-regression, path traversal, pipeline
SIGLA1, SIGLA2, SIGLA3
<!-- /whisper -->

## Personas

- **Nombre Apellido** — rol en el equipo. Se le suele llamar "Nombre" a secas.
- **Otro Nombre** — rol.

## Productos y componentes

- **COMPONENTE_A** — qué es, en una línea.
- **COMPONENTE_B** — qué es.

## Siglas

- **VCD** — documento de X.
- **SBR** — revisión de Y.

## Errores frecuentes de transcripción

Pares `lo que transcribe Whisper -> lo correcto`, para que el LLM los corrija:

- "Goverity" -> Coverity
- "el son" -> JSON
