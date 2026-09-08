// Lo minimo para dibujar SVG a mano, que es la decision del plan: el timeline
// y las metricas son informacion bidimensional y en SVG salen nitidos,
// seleccionables e inspeccionables, sin cargar ninguna libreria en un servidor
// que no tiene internet.
//
// Se construye con `createElementNS` y no con innerHTML porque SVG vive en otro
// espacio de nombres: los elementos creados como HTML se parsean pero no se
// pintan.

const NS = "http://www.w3.org/2000/svg";

export function crear(etiqueta, atributos = {}, hijos = []) {
  const nodo = document.createElementNS(NS, etiqueta);
  for (const [clave, valor] of Object.entries(atributos)) {
    if (valor === null || valor === undefined) continue;
    if (clave === "texto") {
      nodo.textContent = valor;
    } else if (clave === "datos") {
      for (const [k, v] of Object.entries(valor)) nodo.dataset[k] = v;
    } else {
      nodo.setAttribute(clave, valor);
    }
  }
  for (const hijo of [].concat(hijos)) {
    if (hijo) nodo.appendChild(hijo);
  }
  return nodo;
}

// Tooltip nativo del navegador. No sustituye al panel flotante (aparece con
// retardo y no se puede estilar), pero es lo que ve quien navega con el
// teclado o con un lector de pantalla, asi que va en todas las formas.
export const titulo = (texto) => crear("title", { texto });

export function lienzo(ancho, alto, etiqueta) {
  return crear("svg", {
    viewBox: `0 0 ${ancho} ${alto}`,
    width: ancho,
    height: alto,
    role: "img",
    "aria-label": etiqueta,
  });
}

/** Escala lineal de un dominio numerico a un rango de pixeles. */
export function escala([min, max], [inicio, fin]) {
  // Dominio degenerado (un solo valor, o todos iguales): se centra en vez de
  // dividir por cero. Es el caso normal con una unica reunion en la base.
  if (max === min) return () => (inicio + fin) / 2;
  return (valor) => inicio + ((valor - min) / (max - min)) * (fin - inicio);
}

/** Ticks "redondos" para un eje de 0 a max, con un maximo de `cuantos`. */
export function pasos(max, cuantos = 4) {
  if (max <= 0) return [0];
  const bruto = max / cuantos;
  const magnitud = Math.pow(10, Math.floor(Math.log10(bruto)));
  const paso = [1, 2, 2.5, 5, 10].map((m) => m * magnitud).find((p) => p >= bruto);
  const valores = [];
  for (let v = 0; v <= max + paso / 2; v += paso) valores.push(Math.round(v));
  return valores;
}
