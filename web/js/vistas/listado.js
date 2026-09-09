// Listado de reuniones. Nacio en I0 como la vista entera y ahora es la parte
// de abajo: el timeline responde a "qué ha pasado" y esto a "qué reunión fue".
// Desde I2 cada tarjeta es un enlace a su vista de reunión; el resalte que
// hacía el timeline sobre la tarjeta ya no existe, porque pulsar una reunión
// lleva a verla y no a señalarla en una lista.

import { duracion, escapar } from "../formato.js";
import { urlReunion } from "../enlaces.js";

function cifra(n, singular, plural_) {
  return `<span><b>${n}</b> ${n === 1 ? singular : plural_}</span>`;
}

function tarjeta(reunion) {
  const partes = [];
  const dur = duracion(reunion.duracion_seg);
  if (dur) partes.push(`<span>${dur}</span>`);
  partes.push(cifra(reunion.n_segmentos, "segmento", "segmentos"));
  partes.push(cifra(reunion.n_acciones, "acción", "acciones"));
  if (reunion.n_riesgos) partes.push(cifra(reunion.n_riesgos, "riesgo", "riesgos"));
  if (reunion.tiene_audio) partes.push("<span>con audio</span>");

  return `
    <article class="reunion">
      <div class="reunion-cabecera">
        <h2><a href="${urlReunion(reunion.uid)}">${escapar(
          reunion.titulo || reunion.fecha
        )}</a></h2>
        <span class="etiqueta" data-tipo="${escapar(reunion.tipo)}">${escapar(reunion.tipo)}</span>
      </div>
      <div class="uid">${escapar(reunion.fecha)} · ${escapar(reunion.uid)}</div>
      ${reunion.resumen ? `<p class="resumen">${escapar(reunion.resumen)}</p>` : ""}
      <div class="cifras">${partes.join("")}</div>
    </article>`;
}

export function dibujarListado(contenedor, pagina, hayFiltros) {
  if (pagina.total === 0) {
    // Distinguir "no hay nada" de "el filtro no encuentra nada": con una base
    // casi vacía, lo primero es lo normal y no un error.
    contenedor.innerHTML = `<div class="aviso">${
      hayFiltros
        ? "Ninguna reunión coincide con el filtro."
        : "Todavía no hay reuniones. Procesa una con summarize_teams.py."
    }</div>`;
    return;
  }
  contenedor.innerHTML = pagina.reuniones.map(tarjeta).join("");
}
