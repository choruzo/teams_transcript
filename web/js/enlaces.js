// Las URL *del front*, en un solo sitio. api.js hace lo propio con las del
// servidor; mezclarlas sería confundir dos cosas que cambian por motivos
// distintos.
//
// Se direcciona siempre por `uid` y nunca por `id`: el `id` de una reunión
// cambia al reprocesarla, así que un enlace guardado o una cita del chat
// apuntarían a otra reunión (o a ninguna). Ver deuda D1 del plan.

export const urlReunion = (uid) => `/reunion.html?uid=${encodeURIComponent(uid)}`;

/** Ancla de un segmento dentro de la vista de reunión: `…?uid=x#s-12`. */
export const urlSegmento = (uid, idx) => `${urlReunion(uid)}#${idSegmento(idx)}`;

export const idSegmento = (idx) => `s-${idx}`;

/**
 * El tablero de acciones, con sus filtros en la dirección.
 *
 * Los filtros van en la URL y no en memoria de la página por la misma razón
 * que el periodo del timeline: «las tres cosas que llevan un mes paradas» es
 * algo que se pega en un chat, y una vista que no se puede enlazar no sirve
 * para eso. `estado` es repetible.
 */
export function urlAcciones(filtros = {}) {
  const parametros = new URLSearchParams();
  for (const [clave, valor] of Object.entries(filtros)) {
    if (valor === null || valor === undefined || valor === "") continue;
    if (Array.isArray(valor)) valor.forEach((uno) => parametros.append(clave, uno));
    else parametros.set(clave, valor);
  }
  const consulta = parametros.toString();
  return consulta ? `/acciones.html?${consulta}` : "/acciones.html";
}

/**
 * La búsqueda, con su consulta y sus filtros en la dirección (I4).
 *
 * Mismo criterio que el tablero: una búsqueda que no se puede pegar en un
 * chat no sirve para «mira dónde se habló de esto». Comparte la forma de
 * `urlAcciones` a propósito, y no se unifican en una sola función porque lo
 * único que tienen en común es el bucle: las claves, la página y el
 * significado de cada filtro son distintos.
 */
export function urlBuscar(filtros = {}) {
  const parametros = new URLSearchParams();
  for (const [clave, valor] of Object.entries(filtros)) {
    if (valor === null || valor === undefined || valor === "") continue;
    parametros.set(clave, valor);
  }
  const consulta = parametros.toString();
  return consulta ? `/buscar.html?${consulta}` : "/buscar.html";
}

/**
 * El chat (I5). Admite una pregunta en la dirección para que «pregúntale
 * esto» se pueda enlazar; el historial, en cambio, vive solo en la pestaña.
 */
export function urlChat(pregunta = "") {
  return pregunta ? `/chat.html?q=${encodeURIComponent(pregunta)}` : "/chat.html";
}
