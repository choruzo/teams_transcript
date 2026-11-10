// El informe (I8) llega del servidor **ya en Markdown**, generado por
// `report_teams.renderizar_markdown`. Esta vista no calcula ni una cifra: pinta
// ese texto y lo descarga. Si el front recompusiera las secciones por su
// cuenta, el informe de la pantalla, el que se descarga y el que imprime
// `python report_teams.py` podrian contar cosas distintas del mismo periodo.
//
// El pintor entiende solo el subconjunto que produce `renderizar_markdown`:
// `# `, `## `, listas con un nivel de anidamiento y enfasis en linea. Es
// deliberadamente pequeno; no es un interprete de Markdown general.

import { escapar } from "../formato.js";

// Negrita primero: `**x**` tambien casaria con `*x*` si se aplicara al reves.
const NEGRITA = /\*\*([^*]+)\*\*/g;
const CURSIVA = /\*([^*]+)\*/g;

function enLinea(texto) {
  return texto.replace(NEGRITA, "<strong>$1</strong>").replace(CURSIVA, "<em>$1</em>");
}

/**
 * Markdown -> HTML. El texto se escapa **antes** de aplicar el enfasis, igual
 * que el resaltado de la busqueda y las citas del chat: al reves, un `<script>`
 * dictado en una reunion se ejecutaria.
 */
export function aHtml(markdown) {
  const lineas = escapar(markdown).split("\n");
  const salida = [];
  let parrafo = [];
  // Cada elemento es el interior de un `<li>` sin cerrar, para poder colgarle
  // una sublista. La lista se cierra al llegar a otro bloque o al final.
  let lista = [];

  const cerrarParrafo = () => {
    if (!parrafo.length) return;
    salida.push(`<p>${enLinea(parrafo.join(" "))}</p>`);
    parrafo = [];
  };
  const cerrarLista = () => {
    if (!lista.length) return;
    salida.push(`<ul>${lista.map((item) => `<li>${item}</li>`).join("")}</ul>`);
    lista = [];
  };

  for (const linea of lineas) {
    if (!linea.trim()) {
      cerrarParrafo();
      cerrarLista();
      continue;
    }
    if (linea.startsWith("# ")) {
      cerrarParrafo();
      cerrarLista();
      salida.push(`<h1>${enLinea(linea.slice(2))}</h1>`);
      continue;
    }
    if (linea.startsWith("## ")) {
      cerrarParrafo();
      cerrarLista();
      salida.push(`<h2>${enLinea(linea.slice(3))}</h2>`);
      continue;
    }
    if (/^ {2}- /.test(linea)) {
      // Las sublistas del informe son las semanas de actividad. Van dentro del
      // `<li>` anterior; si no lo hubiera, se tratan como un item mas.
      const texto = enLinea(linea.replace(/^ {2}- /, ""));
      if (lista.length) lista[lista.length - 1] += `<ul><li>${texto}</li></ul>`;
      else lista.push(texto);
      continue;
    }
    if (linea.startsWith("- ")) {
      cerrarParrafo();
      lista.push(enLinea(linea.slice(2)));
      continue;
    }
    cerrarLista();
    parrafo.push(linea);
  }
  cerrarParrafo();
  cerrarLista();
  return salida.join("\n");
}

/**
 * El Markdown que se ve y se descarga, con la lectura del modelo dentro.
 *
 * La lectura va justo debajo de la cabecera y por encima de la primera
 * seccion, que es donde la pone `renderizar_markdown` cuando el CLI recibe
 * `--narrativa`. Es el unico trozo de composicion que se hace en el navegador,
 * porque el modelo responde despues de que el informe ya este en pantalla.
 */
export function conLectura(markdown, narrativa) {
  const texto = (narrativa || "").trim();
  if (!texto) return markdown;
  const bloque = `## Lectura\n\n${texto}\n\n`;
  const corte = markdown.indexOf("\n## ");
  if (corte === -1) return `${markdown.replace(/\s*$/, "")}\n\n${bloque}`;
  return markdown.slice(0, corte + 1) + bloque + markdown.slice(corte + 1);
}

/** `informe-2026-11-01_2026-11-30.md`, o `informe-historico.md`. */
export function nombreDeArchivo(informe) {
  let sufijo = "historico";
  if (informe.desde && informe.hasta) sufijo = `${informe.desde}_${informe.hasta}`;
  else if (informe.desde) sufijo = `desde_${informe.desde}`;
  else if (informe.hasta) sufijo = `hasta_${informe.hasta}`;
  return `informe-${sufijo}.md`;
}

/** Descarga lo que hay en pantalla, no una segunda generacion en el servidor. */
export function descargar(markdown, nombre) {
  const blob = new Blob([markdown], { type: "text/markdown;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const enlace = document.createElement("a");
  enlace.href = url;
  enlace.download = nombre;
  document.body.appendChild(enlace);
  enlace.click();
  enlace.remove();
  // Revocar en el mismo tick puede cortar la descarga en algunos navegadores.
  setTimeout(() => URL.revokeObjectURL(url), 0);
}
