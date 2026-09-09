# Estilo de la interfaz

Contrato visual de `web/`. Cualquier vista nueva (I2 en adelante) parte de aquí
en vez de inventar colores o espaciados.

## Reglas

1. **Los colores viven solo en `css/tokens.css`.** `estilo.css` y cualquier hoja
   futura usan `var(--…)` y ningún valor literal. Si hace falta un color que no
   existe, se añade a `tokens.css` con su comentario de uso, no en el sitio donde
   se necesita.
2. **Dos temas, los mismos nombres de token.** Ningún componente sabe en qué
   tema está: usa `var(--panel)`, `var(--text)`… y el valor cambia solo. El tema
   efectivo es el atributo `data-tema` de `<html>` (`claro`, u oscuro cuando no
   hay atributo o vale otra cosa), que pone `js/tema.js`.
3. **Sin framework, sin build, sin fuentes descargadas.** El servidor no tiene
   internet: nada de CDN, `node_modules` ni ficheros de fuentes. Tipografía del
   sistema (`--fuente`) y monoespaciada del sistema (`--mono`) para
   identificadores, rutas, cifras técnicas y el pie de salud.
4. **`color-scheme` por tema**, para que el navegador pinte en juego los
   controles nativos que no se pueden estilar: `input type="date"`, `select` y
   las barras de desplazamiento.
5. **Una hoja por capa:** `tokens.css` (el contrato) y `estilo.css` (los
   componentes), enlazada la segunda con `@import` de la primera para que
   enlazar `estilo.css` baste. Mientras el front quepa en unas pocas vistas no
   se parte más.

## Paleta

| Token | Oscuro | Claro | Uso |
| --- | --- | --- | --- |
| `--bg` | `#0a1214` | `#eef4f2` | Fondo de página; los degradados van en `--halos`. |
| `--panel` | `#0f1b1e` | `#ffffff` | Superficie de tarjetas, tablas y cabecera. |
| `--panel2` | `#122226` | `#e6efec` | Superficie secundaria: hover de filas, campos de formulario, insignia neutra. |
| `--line` | `#1b3237` | `#d2e2dc` | Bordes y separadores. |
| `--text` | `#cfe3de` | `#10262b` | Texto principal. |
| `--dim` | `#6f8b86` | `#5b7873` | Texto secundario, metadatos, etiquetas. |
| `--verde` | `#34d399` | `#0a8467` | Acento primario, éxito, marca. |
| `--verde2` | `#0d9488` | `#0d7d72` | Verde de superficie: botón primario, borde de énfasis. |
| `--azul` | `#38bdf8` | `#0b7fae` | Enlaces, información, «en curso». |
| `--azul2` | `#155e75` | `#d7ebf5` | Fondo de insignia informativa, borde de citas. |
| `--ambar` | `#e8b545` | `#a4650a` | Advertencia, INCONCLUSO, pendiente de revisión. |
| `--rojo` | `#e77070` | `#c0392b` | Error, FAIL. |
| `--sobre-acento` | `= --bg` | `#ffffff` | Texto encima de una superficie de acento (botón primario). |
| `--halos` | dos radiales | ídem, más suaves | `background-image` del `body`. |

El claro **no** es el oscuro con los mismos acentos: `#34d399` sobre blanco se
queda en 1,7:1 y es ilegible. Cada acento se oscurece hasta pasar de 4,5:1 sobre
`--panel` conservando el tono, de ahí los valores de la tercera columna. Al
tocar un acento, comprobar el contraste en el tema donde se usa como **texto**
(insignias, cifras, pie), que es el caso exigente.

Para mezclas puntuales (bordes de énfasis, fondos al 8 %) se usa `color-mix(in
srgb, var(--token) N%, …)` en lugar de un `rgba()` con el color escrito a mano;
así el token sigue siendo la única fuente.

## Convenciones de componente

- **Jerarquía de botones dentro de un formulario:** `button[type="submit"]` es
  la acción principal (relleno `--verde2`, texto `--sobre-acento`);
  `button[type="button"]` es secundaria (fantasma con borde `--line`). No hay
  clases: la semántica del formulario ya lo dice. Las reglas van acotadas a
  `.filtros` para no pisar botones de chrome como el conmutador de tema, que
  tiene la misma especificidad; una vista nueva con formulario acota igual.
- **Insignia de tipo de reunión** (`.etiqueta[data-tipo]`): un color por tipo de
  `summarize_teams.py` — `daily` verde, `planning` azul, `retro` ámbar,
  `workshop` neutro. Al añadir un tipo allí, añadir aquí su regla; sin ella la
  insignia sale neutra, que es una degradación aceptable.
- **Semántica del color de estado:** verde correcto/cerrado, azul en curso o
  informativo, ámbar pendiente o dudoso, rojo error o fallo. La misma escala vale
  para los estados de acción del histórico; no reutilizar estos colores como
  decoración.
- **Foco visible siempre:** `:focus-visible` pinta un anillo verde
  (`--foco`). No sustituir por `outline: none` a secas en ningún control.
- **Radios:** `--radio` para tarjetas y avisos, `--radio-s` para controles.

## Gráficos en SVG (I1)

El timeline y las métricas se dibujan con `js/svg.js`, sin librería: son
información bidimensional y en SVG salen nítidos, seleccionables y accesibles.
Reglas propias de esta capa:

1. **El color va en el CSS, no en el JS.** El SVG se construye con clases
   (`t-daily`, `e-en_progreso`, `estancada`) y `estilo.css` las pinta con
   `fill`/`stroke` sobre los mismos tokens. Ningún `setAttribute("fill", …)`.
2. **Los tipos de reunión repiten los colores de la insignia** del listado, y
   los estados de acción siguen la escala de esta guía: azul en curso, ámbar
   pendiente o estancado, rojo bloqueado, verde cerrado, gris apagado lo
   abandonado. La leyenda bajo el timeline reutiliza esas mismas clases, pero
   en HTML el color se aplica con `color` (el `fill` no afecta a un `<i>`).
3. **Toda forma lleva `<title>`**: es el tooltip nativo y lo que leen los
   lectores de pantalla. Lo que solo se ve al pasar el ratón no puede ser la
   única forma de acceder a un dato.
4. **Las medidas van en píxeles reales**, no en unidades relativas, para que el
   texto no se deforme; por eso el dibujo se rehace en `resize` y hay que
   descontar el relleno del contenedor al medirlo (`clientWidth` lo incluye, y
   sin restarlo aparece una barra de desplazamiento horizontal).
5. **Nada de zoom ni pan propios.** El periodo se cambia con los botones de la
   barra de filtros, que escriben `desde`/`hasta` en la URL: un estado de vista
   que no se puede enlazar no vale para compartir.

## Vista de reunión (I2)

La segunda página del front (`reunion.html`). No hay enrutado del lado del
cliente: son ficheros estáticos (cinco desde I5) y el navegador ya sabe ir de
uno a otro; un router propio solo tendría sentido si hubiera estado que
conservar entre vistas. Las direcciones del front viven en `js/enlaces.js`, como las del
servidor viven en `js/api.js`, y siempre llevan el `uid` —nunca el `id`—
porque el `id` cambia al reprocesar.

1. **Ficha arriba, paneles debajo, transcripción al final.** La ficha
   (`.ficha`) responde a "qué reunión es esto"; los paneles reutilizan
   `.paneles`/`.panel` de las métricas, así que una vista nueva con bloques
   hereda el mismo ritmo sin CSS propio.
2. **Los estados de acción son insignias con la escala de siempre**: verde
   cerrado, azul en curso, rojo bloqueado, ámbar estancado. Se escriben
   `.chip.e-<estado>` con doble clase porque `.chip` se define después que las
   clases de los carriles y, con una sola, ganaría el gris neutro.
3. **La transcripción se agrupa por hablante consecutivo** y cada segmento
   conserva su ancla `#s-<idx>`. La marca de tiempo va en monoespaciada y de
   ancho fijo para que las líneas queden alineadas; en I7 será además el punto
   por el que salte el audio, y en I5 el destino de las citas del chat. Una
   etiqueta cruda de la diarización (`SPEAKER_01`) se pinta apagada y en
   monoespaciada: no se disfraza de persona.
4. **Lo que se descarga se regenera desde la base**, no se sirve el fichero del
   disco. El `.md` y el `.srt` originales viven en la máquina que procesó la
   reunión y, en cuanto I6 permita corregir datos, quedarán obsoletos sin que
   nadie los regenere.

## Tablero de acciones (I3)

La tercera página (`acciones.html`). Comparte cabecera, filtros y pie con las
otras dos; lo propio de esta vista son tres cosas:

1. **La tarjeta de acción vive en `js/vistas/acciones.js`, no en cada vista.**
   La pintan el tablero y la vista de reunión, y una insignia que significara
   cosas distintas según por dónde se llegue sería el peor fallo posible de una
   herramienta cuyo valor es que todos miren el mismo dato. `vistas/reunion.js`
   la importa de ahí y solo le pasa el `uid` de la reunión que se está mirando,
   para que no se enlace a sí misma.
2. **Los recuentos por estado son el filtro.** Un chip por estado con su
   número, pulsable, calculado *sin* el filtro de estado: apagado dice cuántas
   aparecerían al pulsarlo. Se pintan también los que están a cero —«no hay
   nada bloqueado» es información— y solo se deshabilitan si además no están
   pulsados, o no habría forma de soltar el filtro. Es la versión honesta de
   las «columnas por estado» del plan: con cinco columnas paginadas por
   separado, el tablero deja de leerse de un vistazo, que es lo único que
   tiene que hacer.
3. **Filtrar es navegar.** Cada cambio escribe los filtros en la URL y recarga:
   «lo de Ana que lleva un mes parado» se pega en un chat y se abre igual.
   Por eso los desplegables filtran al cambiar (valor de una lista cerrada) y
   el campo de texto solo al enviar, y por eso el orden por defecto **no** va
   en la dirección: es ruido en un enlace que se comparte.

La lista se pagina acumulando (`Cargar N acciones más`, como la transcripción)
y no con páginas numeradas: un tablero se recorre de arriba abajo. El orden lo
decide el servidor, nunca un `sort` del navegador, que solo reordenaría la
página cargada y mentiría sobre el resto.

Y lo que esta vista **no** hace: cambiar estados, reasignar o fusionar
duplicados. Eso es I6 y necesita la tabla `overrides`; sin ella, la siguiente
pasada de `summarize_teams.py` se llevaría la corrección por delante. La nota
bajo el encabezado lo dice en pantalla, junto con el alcance del dato: el
estado es el de hoy y el umbral de ESTANCADA es el mismo del `.md`.

## Búsqueda (I4)

La cuarta página (`buscar.html`). Lo propio de esta vista:

1. **El resultado entero es el enlace.** Se llega aquí para saltar a la
   transcripción, así que el blanco de clic es la tarjeta y no una palabra
   dentro de ella. Cada uno lleva su reunión, su hablante y su minuto, y
   apunta al ancla `#s-<idx>` que puso I2.
2. **El resaltado es un fondo tenue, no un rotulador.** Con cincuenta
   resultados en pantalla, medio párrafo en amarillo cansa la vista; `mark`
   usa `--verde2` al 28 %. El HTML se escapa **antes** de sustituir los
   marcadores que manda la API: el orden inverso pintaría `&lt;mark&gt;` y, lo
   importante, escaparlo después no protegería de nada.
3. **Las reuniones donde aparece el término son el filtro**, con el mismo
   componente que los chips de estado del tablero y por el mismo motivo: se
   calculan sin el filtro de reunión, así que acotar a una no borra la lista
   desde la que se acota. Con una sola reunión no se pintan.
4. **La ayuda de sintaxis ocupa el hueco de los resultados** antes de buscar
   nada. No es decoración: FTS5 no tiene raíces, `reunion` no encuentra
   «reuniones», y quien no lo sepa concluirá que la herramienta no funciona.

El atajo global (`/`, o `Ctrl`/`Cmd`+`K`) vive en `js/buscador.js` y lo
importan las cinco páginas. `/` se ignora si el foco está en un campo de
texto —ahí es una barra—; el de modificador no, porque se pulsa a propósito.
En la propia página de búsqueda enfoca el campo y selecciona lo que hubiera,
en vez de navegar a donde ya se está.

## Chat (I5)

La quinta página (`chat.html`). Lo propio de esta vista:

- **La cita es el producto.** Cada `[n]` de la respuesta se convierte en un
  enlace al segmento exacto (`urlSegmento`, el ancla `#s-<idx>` que dejó I2).
  Se escapa el HTML **primero** y se sustituyen las citas **después**, igual
  que el resaltado de la búsqueda y por el mismo motivo. Un `[7]` que no
  corresponda a ninguna fuente se deja como texto: el modelo se la ha
  inventado, y disfrazarla de enlace roto sería peor que enseñarla.
- **La respuesta se repinta entera en cada trozo**, no se van añadiendo nodos.
  Una cita puede llegar partida entre dos trozos (`[` en uno y `12]` en el
  siguiente) y solo se puede enlazar mirando el texto completo.
- **Filtrar no es navegar**, al revés que en el tablero y en la búsqueda. Lo
  que se comparte aquí es la respuesta (botón de exportar a Markdown), y
  recargar a mitad de una conversación la perdería. El historial vive en
  `sessionStorage`, no en la base: el chat es de solo lectura.
- **Se dice con qué se está respondiendo** bajo el encabezado: búsqueda
  híbrida o solo literal. Una respuesta pobre puede ser culpa de que falte el
  índice semántico y no de que el histórico no lo tenga.
- Las fuentes van en un `<details>` plegado: acompañan a la respuesta, no
  compiten con ella. Se emiten **antes** que el texto, así que si el modelo se
  cae a mitad se ve igualmente de dónde habría salido.
- El compositor hereda la jerarquía de botones de `.filtros` (submit relleno,
  `button` fantasma) sin clases propias, como manda la sección de formularios.

## Decir lo que el dato soporta

La interfaz no promete más de lo que hay en la base: los riesgos son
«mencionados en el periodo» y nunca «abiertos», los cierres por semana llevan
su advertencia sobre `cerrada_en`, y las reuniones sin duración se cuentan
aparte en vez de sumar cero en silencio. Con menos de tres semanas de datos no
se dibuja una serie: se muestra la tabla cruda. Al añadir una métrica, decir
también su alcance —histórico completo o periodo filtrado—, que es lo que
separa un número útil de uno que nadie sabe interpretar.

## Conmutador de tema

Un solo botón en la cabecera con tres estados de hecho:

- Sin nada guardado se sigue `prefers-color-scheme` y se cambia con él (los
  escritorios que alternan claro de día y oscuro de noche arrastran la página).
- Al pulsar, la elección manda y se guarda en `localStorage` bajo `tema`. Para
  volver a seguir al sistema se borra esa clave; no merece un tercer botón.
- Muestra el icono del tema **al que se va a cambiar**, con `aria-label`: un
  texto («Claro»/«Oscuro») no dice si nombra el estado actual o el destino.

La lectura de `localStorage` está duplicada a propósito en un `<script>` inline
del `<head>` de **cada página** (las cinco: `index.html`, `reunion.html`, `acciones.html`, `buscar.html` y `chat.html`): `js/tema.js` es un módulo y por tanto diferido, así
que sin ese fragmento la página parpadearía en oscuro antes de pasar a claro.
Los accesos a `localStorage` van en `try/catch` porque en modo incógnito o con
las cookies bloqueadas lanzan; en ese caso se sigue al sistema y no se recuerda.
