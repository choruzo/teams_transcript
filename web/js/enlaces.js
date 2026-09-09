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
