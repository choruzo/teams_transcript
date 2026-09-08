# Estilo de la interfaz

Contrato visual de `web/`. Cualquier vista nueva (I1 en adelante) parte de aquí
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
del `<head>` de `index.html`: `js/tema.js` es un módulo y por tanto diferido, así
que sin ese fragmento la página parpadearía en oscuro antes de pasar a claro.
Los accesos a `localStorage` van en `try/catch` porque en modo incógnito o con
las cookies bloqueadas lanzan; en ese caso se sigue al sistema y no se recuerda.
