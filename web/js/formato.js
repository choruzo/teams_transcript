// Formato de textos y numeros. Vive aparte porque lo usan las tres vistas y
// porque el castellano tiene mas casos particulares de los que parece: el
// plural de "acción", los rangos de fechas, la semana ISO.

export function escapar(texto) {
  const nodo = document.createElement("div");
  nodo.textContent = texto ?? "";
  return nodo.innerHTML;
}

// "1 reunión" / "2 reuniones". Se pasa el plural entero y no un sufijo porque
// en castellano no basta con añadir una "s" (reunión → reuniones).
export function plural(cantidad, singular, plural_) {
  return `${cantidad} ${cantidad === 1 ? singular : plural_}`;
}

export function duracion(segundos) {
  if (!segundos) return null;
  const minutos = Math.round(segundos / 60);
  if (minutos < 60) return `${minutos} min`;
  const horas = Math.floor(minutos / 60);
  return `${horas} h ${String(minutos % 60).padStart(2, "0")} min`;
}

// Segundos -> "4:07" o "1:04:07". Es la marca de la transcripción, y va en
// monoespaciada: una columna de tiempos con anchos distintos no se lee.
export function marcaTiempo(segundos) {
  if (segundos === null || segundos === undefined) return "";
  const total = Math.max(0, Math.round(segundos));
  const dos = (n) => String(n).padStart(2, "0");
  const horas = Math.floor(total / 3600);
  const resto = total % 3600;
  const min = Math.floor(resto / 60);
  return horas
    ? `${horas}:${dos(min)}:${dos(resto % 60)}`
    : `${min}:${dos(resto % 60)}`;
}

export function minutos(total) {
  if (!total) return "0 min";
  if (total < 60) return `${total} min`;
  return `${Math.floor(total / 60)} h ${String(total % 60).padStart(2, "0")} min`;
}

// Las fechas de la base son `YYYY-MM-DD` sin hora. Se parten a mano en vez de
// pasar por `new Date(cadena)`: esa forma las interpreta como UTC y, al oeste
// de Greenwich, `2026-09-07` se muestra como el 6.
export function aFecha(iso) {
  const [anio, mes, dia] = String(iso).split("-").map(Number);
  return new Date(anio, (mes || 1) - 1, dia || 1);
}

const CORTA = new Intl.DateTimeFormat("es-ES", { day: "numeric", month: "short" });
const LARGA = new Intl.DateTimeFormat("es-ES", {
  weekday: "short",
  day: "numeric",
  month: "short",
  year: "numeric",
});

export const fechaCorta = (iso) => CORTA.format(aFecha(iso));
export const fechaLarga = (iso) => LARGA.format(aFecha(iso));

export function iso(fecha) {
  const dos = (n) => String(n).padStart(2, "0");
  return `${fecha.getFullYear()}-${dos(fecha.getMonth() + 1)}-${dos(fecha.getDate())}`;
}

// "2026-W37" -> "8 sep" (el lunes de esa semana). Una etiqueta con el numero
// de semana no le dice nada a nadie mirando un grafico.
export function lunesDeSemana(clave) {
  const [anio, semana] = clave.split("-W").map(Number);
  // El 4 de enero cae siempre en la semana 1 (definicion ISO 8601).
  const cuatro = new Date(anio, 0, 4);
  const lunesUno = new Date(cuatro);
  lunesUno.setDate(cuatro.getDate() - ((cuatro.getDay() + 6) % 7));
  const lunes = new Date(lunesUno);
  lunes.setDate(lunesUno.getDate() + (semana - 1) * 7);
  return lunes;
}

export const etiquetaSemana = (clave) => CORTA.format(lunesDeSemana(clave));

// Los estados de accion de memoria.py, en castellano legible.
export const ESTADOS = {
  abierta: "Abierta",
  en_progreso: "En progreso",
  bloqueada: "Bloqueada",
  completada: "Completada",
  abandonada: "Abandonada",
};

export const nombreEstado = (estado) => ESTADOS[estado] || estado;
