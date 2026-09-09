// El chat sobre el histórico (sección 3.5 del plan, fase I5).
//
// Solo pinta: `js/chat.js` decide qué se pregunta y cuándo. Lo único con
// enjundia de aquí es `conCitas()`, y es donde vive la propiedad que separa
// un chat útil de uno en el que no se puede confiar: cada afirmación lleva su
// fuente, y la fuente es un enlace al segmento exacto de la transcripción.

import { escapar, fechaCorta } from "../formato.js";
import { urlSegmento, urlReunion } from "../enlaces.js";

const PREGUNTAS_SUGERIDAS = [
  "¿Qué lleva más tiempo bloqueado?",
  "¿Qué se decidió en la última retro?",
  "¿Qué acciones siguen abiertas y de quién son?",
  "¿De qué se ha hablado esta semana?",
];

/** «Daily (3 sep)», o solo la fecha si esa reunión no tiene título. */
const nombreReunion = (fecha, titulo) =>
  titulo ? `${titulo} (${fechaCorta(fecha)})` : fechaCorta(fecha);

/** El destino de una cita: el segmento si lo hay, y si no la reunión. */
function destino(fuente) {
  if (!fuente || !fuente.uid) return null;
  return fuente.idx === null || fuente.idx === undefined
    ? urlReunion(fuente.uid)
    : urlSegmento(fuente.uid, fuente.idx);
}

/**
 * Convierte los `[3]` de la respuesta en enlaces.
 *
 * Mismo orden y mismo motivo que el resaltado de la búsqueda: **primero** se
 * escapa el HTML —el modelo repite lo que dijo la gente, y un `<script>` dicho
 * en voz alta tiene que seguir siendo texto— y **después** se sustituyen las
 * citas. Al revés, se escaparían las etiquetas que acabamos de generar.
 *
 * Un número que no corresponda a ninguna fuente se deja tal cual: el modelo se
 * ha inventado una cita y disfrazarla de enlace roto sería peor que enseñarla.
 */
export function conCitas(texto, fuentes) {
  const porNumero = new Map((fuentes || []).map((f) => [String(f.n), f]));
  return escapar(texto).replace(/\[(\d{1,3})\]/g, (todo, numero) => {
    const fuente = porNumero.get(numero);
    const url = destino(fuente);
    if (!url) return todo;
    const titulo = `${nombreReunion(fuente.fecha, fuente.titulo)}${
      fuente.persona ? ` · ${fuente.persona}` : ""
    }`;
    return `<a class="cita" href="${escapar(url)}" title="${escapar(titulo)}">${todo}</a>`;
  });
}

/** El texto de la respuesta con sus párrafos, sin meter un motor de Markdown. */
function parrafos(html) {
  return html
    .split(/\n{2,}/)
    .map((trozo) => trozo.trim())
    .filter(Boolean)
    .map((trozo) => `<p>${trozo.split("\n").join("<br>")}</p>`)
    .join("");
}

function fuente(f) {
  const url = destino(f);
  const donde = f.uid
    ? `${nombreReunion(f.fecha, f.titulo)}${
        f.idx !== null && f.idx !== undefined ? ` · línea ${f.idx}` : ""
      }`
    : "sin reunión asociada";
  const cuerpo = `
    <span class="n">[${f.n}]</span>
    <span class="clase c-${escapar(f.clase)}">${escapar(etiquetaClase(f.clase))}</span>
    <span class="donde">${escapar(donde)}</span>
    <span class="dicho">${escapar(recortar(f.texto))}</span>`;
  return url
    ? `<a class="fuente" href="${escapar(url)}">${cuerpo}</a>`
    : `<div class="fuente">${cuerpo}</div>`;
}

const CLASES = {
  transcripcion: "Transcripción",
  accion: "Acción",
  update: "Update",
  riesgo: "Riesgo",
};

const etiquetaClase = (clase) => CLASES[clase] || clase;

// `modo` llega de la API sin acentos (es un identificador, no una etiqueta);
// el castellano se pone aquí, que es donde se escribe para leer.
const MODOS = {
  hibrida: "búsqueda híbrida",
  lexica: "búsqueda literal",
  vacia: "sin coincidencias",
  agregada: "estado del histórico",
};

const nombreModo = (modo) => MODOS[modo] || "búsqueda literal";

const recortar = (texto, tope = 220) =>
  texto && texto.length > tope ? `${texto.slice(0, tope)}…` : texto || "";

/** La burbuja de la pregunta. */
export function dibujarPregunta(texto) {
  return `<article class="turno mio"><div class="burbuja">${escapar(texto)}</div></article>`;
}

/**
 * La burbuja de la respuesta, que se repinta en cada trozo que llega.
 *
 * `estado` es 'escribiendo' | 'listo' | 'error'. Se pinta entera cada vez en
 * vez de ir añadiendo nodos porque una cita puede quedar partida entre dos
 * trozos (`[` en uno y `12]` en el siguiente) y solo se puede enlazar
 * mirando el texto completo.
 */
export function dibujarRespuesta(turno) {
  const clases = ["turno", "suyo"];
  if (turno.estado === "escribiendo") clases.push("escribiendo");

  let cuerpo;
  if (turno.estado === "error") {
    cuerpo = `<div class="aviso error"><strong>No se ha podido responder.</strong>
      <code>${escapar(turno.error)}</code></div>`;
  } else if (!turno.texto) {
    cuerpo = `<p class="tenue">${
      turno.fuentes ? "Redactando la respuesta…" : "Buscando en el histórico…"
    }</p>`;
  } else {
    cuerpo = parrafos(conCitas(turno.texto, turno.fuentes));
  }

  return `
    <article class="${clases.join(" ")}">
      <div class="burbuja">
        ${cuerpo}
        ${dibujarFuentes(turno)}
      </div>
    </article>`;
}

function dibujarFuentes(turno) {
  if (!turno.fuentes || !turno.fuentes.length) return "";
  const aviso = turno.aviso
    ? `<p class="tenue">${escapar(turno.aviso)}</p>`
    : "";
  return `
    <details class="fuentes" ${turno.estado === "error" ? "open" : ""}>
      <summary>${turno.fuentes.length} fuentes · ${escapar(
        nombreModo(turno.modo)
      )}</summary>
      ${aviso}
      <div class="lista-fuentes">${turno.fuentes.map(fuente).join("")}</div>
    </details>`;
}

/**
 * La ayuda con preguntas sugeridas, en el hueco de la conversación.
 *
 * No repite el estado de la búsqueda: eso ya lo dice la nota bajo el
 * encabezado, y verlo dos veces en la misma pantalla no informa el doble.
 */
export function dibujarAyuda(contenedor) {
  const botones = PREGUNTAS_SUGERIDAS.map(
    (p) => `<button type="button" class="sugerencia">${escapar(p)}</button>`
  ).join("");

  contenedor.innerHTML = `
    <div class="aviso ayuda">
      <p>Pregunta en castellano sobre lo que ha pasado en las reuniones. La
        respuesta cita sus fuentes: cada <code>[n]</code> es un enlace a la
        frase exacta de la transcripción.</p>
      <div class="sugerencias">${botones}</div>
    </div>`;
}

/**
 * Decir con qué se está respondiendo, que es la regla de siempre: la
 * búsqueda literal no encuentra «bloquear» buscando «bloqueado», así que una
 * respuesta pobre puede ser culpa del índice y no del histórico.
 */
export function descripcionDelEstado(estado) {
  if (!estado) return "Comprobando el estado del chat…";
  if (!estado.disponible) return estado.motivo || "El chat no está disponible.";
  if (estado.busqueda === "hibrida") {
    const n = estado.indice?.chunks;
    return `Búsqueda híbrida (literal y semántica) sobre ${n} fragmentos indexados.`;
  }
  return (
    "Sin índice semántico: se responde solo con búsqueda literal, que no " +
    "reconoce variantes de una palabra. Constrúyelo con " +
    "`python indexar_teams.py`."
  );
}

/** El aviso permanente bajo el encabezado. */
export function pintarEstado(nodo, estado) {
  nodo.textContent = descripcionDelEstado(estado);
  nodo.classList.toggle(
    "mal",
    Boolean(estado && (!estado.disponible || estado.busqueda !== "hibrida"))
  );
}

/** La conversación entera a Markdown, para el botón de exportar. */
export function aMarkdown(turnos) {
  const lineas = ["# Consulta al histórico del equipo", ""];
  for (const turno of turnos) {
    if (turno.papel === "user") {
      lineas.push(`## ${turno.texto}`, "");
      continue;
    }
    lineas.push(turno.texto || turno.error || "(sin respuesta)", "");
    for (const f of turno.fuentes || []) {
      const donde = f.uid ? nombreReunion(f.fecha, f.titulo) : "sin reunión";
      const linea = f.idx === null || f.idx === undefined ? "" : `, línea ${f.idx}`;
      lineas.push(`- [${f.n}] ${etiquetaClase(f.clase)} — ${donde}${linea}`);
    }
    lineas.push("");
  }
  return lineas.join("\n");
}
