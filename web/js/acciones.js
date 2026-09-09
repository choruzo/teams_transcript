// Orquestador del tablero de acciones (`/acciones.html`). Decide qué se pide y
// con qué filtros; `vistas/acciones.js` solo pinta.
//
// Mismo reparto y mismas reglas que la página principal: los filtros viven en
// la URL para que una vista concreta —«lo de Ana que lleva un mes parado»— se
// pueda guardar o pegar en un chat, y la lista viene paginada del servidor, así
// que ordenar y filtrar son peticiones y no un `sort` en el navegador.
//
// El tablero es de **solo lectura** (fase I3). Los botones para cambiar
// estados, reasignar o fusionar duplicados llegan con la tabla `overrides` en
// I6: sin ella, la siguiente pasada de `summarize_teams.py` borraría cualquier
// corrección hecha aquí.

import { api } from "./api.js";
import { iniciarTema } from "./tema.js";
import { escapar, plural } from "./formato.js";
import { dibujarEstados, dibujarTablero } from "./vistas/acciones.js";
import { pintarSalud } from "./vistas/salud.js";
import { urlAcciones } from "./enlaces.js";

const $ = (selector) => document.querySelector(selector);

// Campos de un solo valor que viajan en la URL. `estado` va aparte porque es
// repetible y se maneja como lista.
const CAMPOS = ["persona", "q", "dias_sin_tocar", "orden"];

// Lo ya traído del servidor. La paginación es acumulativa (el botón dice
// cuántas faltan) en vez de páginas numeradas: un tablero se recorre de arriba
// abajo, no se salta a la página 4.
let cargadas = [];
let ultimo = null;

function filtrosDeLaUrl() {
  const parametros = new URLSearchParams(window.location.search);
  const filtros = {};
  for (const campo of CAMPOS) {
    const valor = parametros.get(campo);
    if (valor) filtros[campo] = valor;
  }
  const estados = parametros.getAll("estado").filter(Boolean);
  if (estados.length) filtros.estado = estados;
  if (parametros.get("estancadas") === "true") filtros.estancadas = "true";
  return filtros;
}

const estadosActivos = (filtros) =>
  Array.isArray(filtros.estado) ? filtros.estado : filtros.estado ? [filtros.estado] : [];

function aplicar(filtros) {
  // Se navega en vez de repintar: el filtro cambia la página entera y no hay
  // estado que conservar. De paso, el botón «atrás» deshace el último filtro,
  // que es lo que se espera de una vista cuyo estado vive en la dirección.
  window.location.href = urlAcciones(filtros);
}

// El orden no tiene opción vacía: sin valor en la URL, el desplegable tiene
// que enseñar el que la API va a aplicar por defecto, o se queda en blanco
// diciendo que no hay ninguno mientras la lista sí está ordenada.
const ORDEN_POR_DEFECTO = "prioridad";

function volcarEnElFormulario(filtros) {
  for (const campo of CAMPOS) $(`#${campo}`).value = filtros[campo] || "";
  $("#orden").value = filtros.orden || ORDEN_POR_DEFECTO;
  $("#estancadas").checked = filtros.estancadas === "true";
}

function leerFormulario() {
  const filtros = { ...filtrosDeLaUrl() };
  for (const campo of CAMPOS) {
    const valor = $(`#${campo}`).value.trim();
    if (valor) filtros[campo] = valor;
    else delete filtros[campo];
  }
  // El orden por defecto no viaja en la URL: es ruido en un enlace que se
  // comparte, y la API aplica ese mismo si no se le dice nada.
  if (filtros.orden === ORDEN_POR_DEFECTO) delete filtros.orden;
  if ($("#estancadas").checked) filtros.estancadas = "true";
  else delete filtros.estancadas;
  return filtros;
}

/**
 * El desplegable de responsables se rellena con lo que la respuesta trae, y no
 * con una lista de personas fija: solo aparecen quienes tienen alguna acción
 * bajo el resto de filtros, así que ninguna opción lleva a una lista vacía.
 */
function volcarResponsables(pagina, elegida) {
  const opciones = [`<option value="">Cualquiera</option>`];
  for (const r of pagina.responsables) {
    const etiqueta =
      r.persona === pagina.sin_responsable ? "Sin responsable" : r.persona;
    opciones.push(
      `<option value="${escapar(r.persona)}">${escapar(etiqueta)} (${r.n})</option>`
    );
  }
  const select = $("#persona");
  select.innerHTML = opciones.join("");
  // Si la persona filtrada no está entre las opciones (porque el resto de
  // filtros la ha dejado sin ninguna), se añade para no perder el filtro que
  // sí está aplicado y que el desplegable diría que no.
  if (elegida && !pagina.responsables.some((r) => r.persona === elegida)) {
    select.insertAdjacentHTML(
      "beforeend",
      `<option value="${escapar(elegida)}">${escapar(elegida)} (0)</option>`
    );
  }
  select.value = elegida || "";
}

function pintarError(donde, mensaje) {
  $(donde).innerHTML = `
    <div class="aviso error">
      <strong>No se han podido cargar los datos.</strong>
      <code>${escapar(mensaje)}</code>
    </div>`;
}

/** Los filtros de la URL, tal como los espera la API. */
function parametros(filtros, desplazamiento = 0) {
  return {
    estado: filtros.estado,
    persona: filtros.persona,
    q: filtros.q,
    dias_sin_tocar: filtros.dias_sin_tocar,
    estancadas: filtros.estancadas,
    orden: filtros.orden,
    desplazamiento,
  };
}

function pintar(filtros) {
  dibujarTablero($("#tablero"), ultimo, cargadas);
  $("#total").textContent = ultimo.total
    ? plural(ultimo.total, "acción", "acciones")
    : "";
  const mas = $("#mas");
  if (!mas) return;
  mas.addEventListener("click", async () => {
    mas.disabled = true;
    mas.textContent = "Cargando…";
    try {
      const pagina = await api.acciones(parametros(filtros, cargadas.length));
      cargadas.push(...pagina.acciones);
      // El total se refresca con la última respuesta: si el pipeline ha
      // escrito entretanto, es más honesto que conservar el de hace un rato.
      ultimo = pagina;
      pintar(filtros);
    } catch (error) {
      pintarError("#tablero", error.message);
    }
  });
}

async function cargar() {
  const filtros = filtrosDeLaUrl();
  volcarEnElFormulario(filtros);
  $("#tablero").innerHTML = '<div class="aviso">Cargando…</div>';
  try {
    ultimo = await api.acciones(parametros(filtros));
    cargadas = ultimo.acciones.slice();
  } catch (error) {
    pintarError("#tablero", error.message);
    $("#estados").innerHTML = "";
    $("#total").textContent = "";
    return;
  }
  dibujarEstados($("#estados"), ultimo, estadosActivos(filtros));
  volcarResponsables(ultimo, filtros.persona);
  $("#umbral").textContent = ultimo.umbral_estancamiento;
  pintar(filtros);
}

// Un chip de estado alterna ese estado en el filtro. Es la versión honesta de
// las «columnas por estado» del plan: con una sola lista paginada, el recuento
// de cada estado se ve igual y no hay cinco columnas que crezcan por su cuenta.
$("#estados").addEventListener("click", (evento) => {
  const boton = evento.target.closest("[data-estado]");
  if (!boton) return;
  const filtros = leerFormulario();
  const activos = estadosActivos(filtros);
  const estado = boton.dataset.estado;
  const nuevos = activos.includes(estado)
    ? activos.filter((e) => e !== estado)
    : [...activos, estado];
  aplicar({ ...filtros, estado: nuevos });
});

$("#filtros").addEventListener("submit", (evento) => {
  evento.preventDefault();
  aplicar(leerFormulario());
});

$("#limpiar").addEventListener("click", () => aplicar({}));

// Los desplegables filtran al cambiar: son un valor de una lista cerrada y
// obligar a pulsar «Filtrar» después de elegir solo añade un paso. El campo de
// texto no, porque se teclea y se envía con Intro.
for (const id of ["persona", "dias_sin_tocar", "orden"]) {
  $(`#${id}`).addEventListener("change", () => aplicar(leerFormulario()));
}
$("#estancadas").addEventListener("change", () => aplicar(leerFormulario()));

iniciarTema();
cargar();
pintarSalud($("#salud"));
