// Los resultados de la búsqueda (sección 3.5 del contrato de la API, fase I4).
//
// Lo que se pinta son **segmentos**, no reuniones: la pregunta es «dónde se
// dijo esto» y la respuesta útil es la frase, con su hablante, su momento y un
// enlace que abre la transcripción justo ahí. Una lista de reuniones que hay
// que abrir una por una no responde a nada.

import { escapar, fechaCorta, marcaTiempo, plural } from "../formato.js";
import { urlSegmento } from "../enlaces.js";

/**
 * El fragmento resaltado, en HTML seguro.
 *
 * El orden importa y es la única razón por la que la API devuelve marcadores
 * dentro del texto: **primero** se escapa todo lo que dijo la gente (un
 * `<script>` mencionado en voz alta es texto) y **después** se sustituyen los
 * dos caracteres de control por `<mark>`. Al revés, el resaltado se escaparía
 * y saldría en pantalla como `&lt;mark&gt;`.
 */
export function resaltar(fragmento, inicio, fin) {
  const escapado = escapar(fragmento);
  return escapado.split(inicio).join("<mark>").split(fin).join("</mark>");
}

/** «Daily (3 sep)», o solo la fecha si esa reunión no tiene título. */
const nombreReunion = (fecha, titulo) =>
  titulo ? `${titulo} (${fechaCorta(fecha)})` : fechaCorta(fecha);

function resultado(c, pagina) {
  const quien = c.persona || c.etiqueta;
  const meta = [
    `<span class="etiqueta" data-tipo="${escapar(c.tipo)}">${escapar(c.tipo)}</span>`,
    `<b>${escapar(nombreReunion(c.fecha, c.titulo))}</b>`,
  ];
  if (quien) {
    // La etiqueta cruda de la diarización no se disfraza de persona, igual
    // que en la transcripción.
    meta.push(
      `<span class="quien${c.persona ? "" : " sin-nombre"}">${escapar(quien)}</span>`
    );
  }
  // Sin `.srt` no hay marcas de tiempo (D9); el enlace al segmento sigue
  // funcionando, solo que no se puede decir en qué minuto fue.
  if (c.inicio !== null && c.inicio !== undefined) {
    meta.push(`<span class="tiempo">${escapar(marcaTiempo(c.inicio))}</span>`);
  }

  return `
    <li class="resultado">
      <a class="ir" href="${urlSegmento(c.uid, c.idx)}">
        <p class="dicho">${resaltar(c.fragmento, pagina.marca_inicio, pagina.marca_fin)}</p>
        <div class="meta">${meta.join("")}</div>
      </a>
    </li>`;
}

/**
 * Las reuniones donde aparece el término, como filtro.
 *
 * Vienen calculadas sin el filtro de reunión —igual que los chips de estado
 * del tablero—, así que acotar a una no borra la lista desde la que se acota.
 * Con una sola reunión el desglose no dice nada que no diga ya el resultado:
 * en ese caso no se pinta.
 */
export function dibujarReunionesDeBusqueda(contenedor, pagina, uidActivo) {
  if (!pagina || pagina.reuniones.length < 2) {
    contenedor.innerHTML = "";
    return;
  }
  contenedor.innerHTML = pagina.reuniones
    .map((r) => {
      const pulsada = r.uid === uidActivo;
      return `
        <button type="button" class="filtro-reunion" data-uid="${escapar(r.uid)}"
                aria-pressed="${pulsada}">
          ${escapar(nombreReunion(r.fecha, r.titulo))} <b>${r.n}</b>
        </button>`;
    })
    .join("");
}

/** La ayuda que se ve antes de buscar nada. Es la sintaxis, no un adorno. */
export function dibujarAyuda(contenedor) {
  contenedor.innerHTML = `
    <div class="aviso ayuda">
      <p>Busca sobre <b>todas las transcripciones</b> del histórico. Los acentos
        dan igual en los dos sentidos: <code>sesion</code> encuentra
        «sesión».</p>
      <ul>
        <li><code>despliegue certificado</code> — las líneas que contienen
          las dos palabras.</li>
        <li><code>"entorno de pre"</code> — la frase seguida.</li>
        <li><code>refact*</code> — todo lo que empiece así. Hace falta, porque
          no hay raíces: <code>reunion</code> no encuentra «reuniones».</li>
      </ul>
      <p class="tenue">Atajo: <kbd>/</kbd> o <kbd>Ctrl</kbd>+<kbd>K</kbd> desde
        cualquier página.</p>
    </div>`;
}

export function dibujarResultados(contenedor, pagina, cargadas) {
  if (pagina.total === 0) {
    contenedor.innerHTML = `
      <div class="aviso">No aparece <b>${escapar(pagina.q)}</b> en ninguna
        transcripción con estos filtros. Recuerda que se busca por palabras
        completas: prueba con <code>${escapar(pagina.q.split(/\s+/)[0])}*</code>.</div>`;
    return;
  }
  const faltan = pagina.total - cargadas.length;
  contenedor.innerHTML = `
    <ul class="resultados">
      ${cargadas.map((c) => resultado(c, pagina)).join("")}
    </ul>
    ${
      faltan > 0
        ? `<p class="mas"><button type="button" id="mas">Cargar ${plural(
            faltan,
            "resultado más",
            "resultados más"
          )}</button></p>`
        : ""
    }`;
}
