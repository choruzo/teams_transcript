// Listado de reuniones. Nacio en I0 como la vista entera y ahora es la parte
// de abajo: el timeline responde a "qué ha pasado" y esto a "qué reunión fue".
// Cada tarjeta lleva su `id` para que el timeline pueda señalarla; cuando
// exista la vista de reunión (I2), la tarjeta será un enlace.

import { duracion, escapar } from "../formato.js";

export const idTarjeta = (uid) => `r-${uid}`;

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
    <article class="reunion" id="${escapar(idTarjeta(reunion.uid))}" tabindex="-1">
      <div class="reunion-cabecera">
        <h2>${escapar(reunion.titulo || reunion.fecha)}</h2>
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

/** Lleva la vista a la tarjeta de esa reunión y la resalta un momento. */
export function señalar(uid) {
  const nodo = document.getElementById(idTarjeta(uid));
  if (!nodo) return;
  nodo.scrollIntoView({ behavior: "smooth", block: "center" });
  nodo.classList.remove("señalada");
  // Reiniciar la animación exige forzar un reflow: sin esto, pulsar dos veces
  // la misma reunión no vuelve a resaltarla.
  void nodo.offsetWidth;
  nodo.classList.add("señalada");
  nodo.focus({ preventScroll: true });
}
