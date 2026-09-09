// Orquestador de la vista de reunión (`/reunion.html?uid=…`).
//
// Mismo reparto que en la página principal: aquí se decide qué se pide y las
// vistas de js/vistas/ solo pintan. La reunión se direcciona por `uid` en la
// URL —nunca por `id`— para que el enlace siga siendo válido después de
// reprocesar la transcripción (deuda D1 del plan).
//
// No hay enrutado del lado del cliente: son dos páginas estáticas y el
// navegador ya sabe ir de una a otra. Un router propio solo tendría sentido si
// hubiera estado que conservar entre vistas, y no lo hay.

import { api } from "./api.js";
import { iniciarTema } from "./tema.js";
import { escapar, plural } from "./formato.js";
import { dibujarReunion } from "./vistas/reunion.js";
import { dibujarTranscripcion, irAlSegmento } from "./vistas/transcripcion.js";
import { pintarSalud } from "./vistas/salud.js";

const $ = (selector) => document.querySelector(selector);

// Los segmentos ya cargados. La API pagina siempre (una reunión larga son
// miles de líneas), así que la página acumula lo que va pidiendo.
const cargados = { segmentos: [], total: 0, con_tiempos: false };

const uidDeLaUrl = () => new URLSearchParams(window.location.search).get("uid");

/** El `#s-12` de la URL, si lo hay: el ancla de una cita a un segmento. */
function segmentoDelAncla() {
  const encaje = /^#s-(\d+)$/.exec(window.location.hash);
  return encaje ? Number(encaje[1]) : null;
}

function pintarError(donde, mensaje) {
  $(donde).innerHTML = `
    <div class="aviso error">
      <strong>No se han podido cargar los datos.</strong>
      <code>${escapar(mensaje)}</code>
    </div>`;
}

async function cargarPagina(uid) {
  const pagina = await api.segmentos(uid, {
    desplazamiento: cargados.segmentos.length,
  });
  cargados.total = pagina.total;
  // `con_tiempos` describe lo devuelto en cada página; basta con que una
  // traiga marcas para que la columna tenga sentido.
  cargados.con_tiempos = cargados.con_tiempos || pagina.con_tiempos;
  cargados.segmentos.push(...pagina.segmentos);
  return pagina;
}

function pintarTranscripcion(uid) {
  dibujarTranscripcion($("#transcripcion"), cargados);
  $("#total-segmentos").textContent = cargados.total
    ? plural(cargados.total, "segmento", "segmentos")
    : "";
  const mas = $("#mas");
  if (mas) {
    mas.addEventListener("click", async () => {
      mas.disabled = true;
      mas.textContent = "Cargando…";
      try {
        await cargarPagina(uid);
        pintarTranscripcion(uid);
      } catch (error) {
        pintarError("#transcripcion", error.message);
      }
    });
  }
}

async function cargar() {
  const uid = uidDeLaUrl();
  if (!uid) {
    $("#reunion").innerHTML =
      '<div class="aviso">Falta el parámetro <code>uid</code> en la dirección. ' +
      'Vuelve al <a href="/">histórico</a> y elige una reunión.</div>';
    return;
  }

  // Las dos peticiones van en paralelo: la transcripción es independiente de
  // la ficha y encadenarlas duplicaría la espera a través del túnel SSH.
  const [detalle, segmentos] = await Promise.allSettled([
    api.reunion(uid),
    api.segmentos(uid, { desplazamiento: 0 }),
  ]);

  if (detalle.status !== "fulfilled") {
    // La reunión no existe (o la base no responde): un solo mensaje. Repetir
    // el mismo error debajo de un encabezado "Transcripción" solo añade ruido
    // sobre algo que no se puede leer de todos modos.
    pintarError("#reunion", detalle.reason.message);
    return;
  }

  dibujarReunion($("#reunion"), detalle.value);
  $("#bloque-transcripcion").hidden = false;
  if (segmentos.status === "fulfilled") {
    cargados.total = segmentos.value.total;
    cargados.con_tiempos = segmentos.value.con_tiempos;
    cargados.segmentos = segmentos.value.segmentos.slice();
    pintarTranscripcion(uid);
    await irAlAncla(uid);
  } else {
    pintarError("#transcripcion", segmentos.reason.message);
  }
}

/**
 * Salta al segmento citado en la URL, cargando las páginas que hagan falta.
 *
 * Sin esto, una cita a la línea 800 de una reunión larga abriría la página y
 * no iría a ninguna parte, porque ese segmento aún no está en el documento.
 */
async function irAlAncla(uid) {
  const idx = segmentoDelAncla();
  if (idx === null) return;
  while (!irAlSegmento(idx) && cargados.segmentos.length < cargados.total) {
    await cargarPagina(uid);
    pintarTranscripcion(uid);
  }
}

iniciarTema();
cargar();
pintarSalud($("#salud"));
