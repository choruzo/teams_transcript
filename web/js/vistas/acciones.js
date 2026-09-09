// La acción como tarjeta, y el tablero que las lista (sección 3.4, fase I3).
//
// La tarjeta vive aquí y no en `vistas/reunion.js` porque las pintan las dos
// páginas: dentro de una reunión y en el tablero. Si cada una tuviera la suya,
// el día que cambie el significado de una insignia —el umbral de ESTANCADA, el
// color de «bloqueada»— cambiaría en un sitio y no en el otro, y el mismo dato
// se leería distinto según por dónde se llegara.
//
// El tablero es **solo lectura**: cambiar estados, reasignar o fusionar es la
// fase I6, y hacerlo antes de que exista la tabla `overrides` significaría que
// la siguiente pasada del LLM se lleva la corrección por delante.

import { escapar, fechaCorta, nombreEstado, plural } from "../formato.js";
import { urlReunion } from "../enlaces.js";

// Los dos estados que cierran una acción, como en `memoria.ESTADOS_CERRADOS`.
// Aquí solo deciden si tiene sentido decir «lleva N días sin tocar»: de algo
// abandonado hace un mes no se está esperando nada.
const CERRADOS = ["completada", "abandonada"];
const esCerrada = (estado) => CERRADOS.includes(estado);

const chip = (texto, clase = "") =>
  `<span class="chip ${clase}">${escapar(texto)}</span>`;

/** Las insignias de una acción: estado, veces mencionada y estancamiento. */
export function marcasDeAccion(a) {
  const marcas = [chip(nombreEstado(a.estado), `e-${a.estado}`)];
  if (a.menciones > 1) marcas.push(chip(`×${a.menciones}`));
  if (a.estancada) marcas.push(chip("ESTANCADA", "s-alta"));
  return marcas;
}

/** «Daily del 3 sep», o solo la fecha si esa reunión no tiene título. */
function nombreReunion(fecha, titulo) {
  return titulo ? `${titulo} (${fechaCorta(fecha)})` : fechaCorta(fecha);
}

function enlaces(a, uidActual) {
  const partes = [];
  if (a.origen_uid !== uidActual) {
    partes.push(
      `nace en <a href="${urlReunion(a.origen_uid)}">${escapar(
        nombreReunion(a.origen_fecha, a.origen_titulo)
      )}</a>`
    );
  }
  // Si la última mención es otra reunión, el estado de arriba es el de *esa*
  // pasada y no el de la que se está mirando. Decirlo evita la lectura
  // equivocada más fácil de la vista de reunión.
  if (a.ultima_uid !== uidActual) {
    partes.push(
      `última mención en <a href="${urlReunion(a.ultima_uid)}">${escapar(
        nombreReunion(a.ultima_fecha, a.ultima_titulo)
      )}</a>`
    );
  }
  return partes;
}

/**
 * Una acción.
 *
 * `uidActual` evita enlazar a la reunión que ya se está mirando; en el tablero
 * es `null` y salen las dos. `dias` añade el tiempo sin tocar, que solo tiene
 * sentido fuera de una reunión concreta: dentro de una, la referencia es la
 * fecha de esa reunión y no hoy.
 */
export function tarjetaAccion(a, { uidActual = null, dias = false } = {}) {
  const detalle = enlaces(a, uidActual);
  if (dias && !esCerrada(a.estado) && a.dias_sin_tocar >= 0) {
    // Negativo significa que la reunión que la mencionó está fechada en el
    // futuro: pasa con una fecha mal puesta en el resumen, y "-370 días sin
    // tocar" solo confunde. Se calla el dato en vez de inventar uno.
    detalle.push(
      a.dias_sin_tocar === 0
        ? "mencionada hoy"
        : `${plural(a.dias_sin_tocar, "día", "días")} sin tocar`
    );
  }
  if (a.cerrada_en) detalle.push(`cerrada el ${escapar(fechaCorta(a.cerrada_en))}`);

  return `
    <li class="accion">
      <p class="descripcion">${escapar(a.descripcion)}</p>
      <div class="meta">
        <span class="persona">${escapar(a.persona || "sin responsable")}</span>
        ${marcasDeAccion(a).join("")}
      </div>
      ${a.comentario ? `<p class="comentario">${escapar(a.comentario)}</p>` : ""}
      ${detalle.length ? `<p class="tenue">${detalle.join(" · ")}</p>` : ""}
    </li>`;
}

// --------------------------------------------------------------------------
// El tablero
// --------------------------------------------------------------------------

/**
 * Los recuentos por estado, como botones que filtran.
 *
 * Vienen calculados **sin** el filtro de estado, así que un chip apagado dice
 * cuántas acciones aparecerían al pulsarlo. Se pintan todos los estados,
 * incluidos los que están a cero: «no hay nada bloqueado» es información y un
 * hueco no lo es.
 */
export function dibujarEstados(contenedor, pagina, activos) {
  contenedor.innerHTML = Object.entries(pagina.por_estado)
    .map(([estado, n]) => {
      const pulsado = activos.includes(estado);
      return `
        <button type="button" class="filtro-estado e-${escapar(estado)}"
                data-estado="${escapar(estado)}"
                aria-pressed="${pulsado}"
                ${n === 0 && !pulsado ? "disabled" : ""}>
          ${escapar(nombreEstado(estado))} <b>${n}</b>
        </button>`;
    })
    .join("");
}

export function dibujarTablero(contenedor, pagina, cargadas) {
  if (pagina.total === 0) {
    contenedor.innerHTML =
      '<div class="aviso">Ninguna acción coincide con el filtro. ' +
      "Prueba a quitar alguno o pulsa <b>Limpiar</b>.</div>";
    return;
  }
  const faltan = pagina.total - cargadas.length;
  contenedor.innerHTML = `
    <ul class="acciones tablero">
      ${cargadas.map((a) => tarjetaAccion(a, { dias: true })).join("")}
    </ul>
    ${
      faltan > 0
        ? `<p class="mas"><button type="button" id="mas">Cargar ${plural(
            faltan,
            "acción más",
            "acciones más"
          )}</button></p>`
        : ""
    }`;
}
