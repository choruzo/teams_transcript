// Orquestador del tablero de acciones (`/acciones.html`). Decide qué se pide y
// con qué filtros; `vistas/acciones.js` solo pinta.
//
// Mismo reparto y mismas reglas que la página principal: los filtros viven en
// la URL para que una vista concreta —«lo de Ana que lleva un mes parado»— se
// pueda guardar o pegar en un chat, y la lista viene paginada del servidor, así
// que ordenar y filtrar son peticiones y no un `sort` en el navegador.
//
// Pulsar la descripción de una acción abre su ficha en el panel lateral
// (I6a), el mismo que en el timeline. Tras corregir, la lista se vuelve a
// pedir con los mismos filtros sin navegar, para no cerrar el panel.

import { api } from "./api.js";
import { iniciarTema } from "./tema.js";
import { escapar, fechaCorta, nombreEstado, plural } from "./formato.js";
import { dibujarEstados, dibujarTablero } from "./vistas/acciones.js";
import {
  abrirDesdeTarjetas,
  botonDeTarjeta,
  crearPanel,
  marcarTarjeta,
} from "./vistas/panel_accion.js";
import { pintarSalud } from "./vistas/salud.js";
import { urlAcciones } from "./enlaces.js";
import { iniciarAtajoDeBusqueda } from "./buscador.js";

const $ = (selector) => document.querySelector(selector);

// Campos de un solo valor que viajan en la URL. `estado` va aparte porque es
// repetible y se maneja como lista.
const CAMPOS = ["persona", "q", "dias_sin_tocar", "orden"];

// Lo ya traído del servidor. La paginación es acumulativa (el botón dice
// cuántas faltan) en vez de páginas numeradas: un tablero se recorre de arriba
// abajo, no se salta a la página 4.
let cargadas = [];
let ultimo = null;
let paginaSugerencias = null;

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

const panel = crearPanel({
  alCambiar: () => cargar({ conservar: true }),
  alSeleccionar: (uid) => marcarTarjeta($("#tablero"), uid),
  alRechazarSugerencia: () => cargarSugerencias(filtrosDeLaUrl()),
  elementoDe: (uid) => botonDeTarjeta($("#tablero"), uid),
});
abrirDesdeTarjetas($("#tablero"), panel);
// Las descripciones de las sugerencias abren el mismo panel que las tarjetas.
abrirDesdeTarjetas($("#sugerencias"), panel);

// --------------------------------------------------------------------------
// Posibles duplicados (I11)
// --------------------------------------------------------------------------

function dibujarSugerencias() {
  const contenedor = $("#sugerencias");
  const contador = $("#n-sugerencias");
  if (!paginaSugerencias) {
    contenedor.innerHTML = '<div class="aviso">Cargando…</div>';
    return;
  }
  contador.textContent = paginaSugerencias.total ? `(${paginaSugerencias.total})` : "";
  if (!paginaSugerencias.disponible) {
    contenedor.innerHTML = `<div class="aviso">${escapar(
      paginaSugerencias.aviso || "Sin índice semántico."
    )}</div>`;
    return;
  }
  if (!paginaSugerencias.sugerencias.length) {
    contenedor.innerHTML =
      '<div class="aviso">Ninguna pareja por encima del umbral.</div>';
    return;
  }
  const lado = (s) => `
    <button type="button" class="abrir-ficha" data-abrir-ficha="${escapar(s.uid)}">${escapar(
      s.descripcion
    )}</button>
    <span class="tenue">${escapar(s.persona || "sin responsable")}${
      s.fecha ? ` · ${escapar(fechaCorta(s.fecha))}` : ""
    } · ${escapar(nombreEstado(s.estado))}</span>`;
  contenedor.innerHTML = `
    <ul class="duplicados">
      ${paginaSugerencias.sugerencias
        .map(
          (s) => `
          <li class="duplicado">
            <div class="duplicado-par">
              <div>${lado({
                uid: s.a_uid,
                descripcion: s.a_descripcion,
                persona: s.a_persona,
                fecha: s.a_fecha,
                estado: s.a_estado,
              })}</div>
              <span class="similitud" title="similitud coseno">${Math.round(
                s.similitud * 100
              )}%</span>
              <div>${lado({
                uid: s.b_uid,
                descripcion: s.b_descripcion,
                persona: s.b_persona,
                fecha: s.b_fecha,
                estado: s.b_estado,
              })}</div>
            </div>
            <div class="duplicado-acciones">
              <button type="button" class="mini" data-rechazar
                      data-a="${escapar(s.a_uid)}" data-b="${escapar(s.b_uid)}"
                      title="No volverá a proponerse, ni al reconstruir el índice">No es la misma</button>
            </div>
          </li>`
        )
        .join("")}
    </ul>`;
}

/**
 * Pide las sugerencias con el mismo responsable/texto que la lista, no con el
 * estado: solo se comparan acciones abiertas y una accion cerrada no se fusiona.
 */
async function cargarSugerencias(filtros) {
  try {
    paginaSugerencias = await api.sugerencias({ persona: filtros.persona, q: filtros.q });
  } catch (error) {
    paginaSugerencias = {
      disponible: false,
      aviso: error.message,
      total: 0,
      sugerencias: [],
    };
  }
  dibujarSugerencias();
}

$("#sugerencias").addEventListener("click", async (evento) => {
  const boton = evento.target.closest("[data-rechazar]");
  if (!boton || boton.disabled) return;
  boton.disabled = true;
  $("#mensaje-sugerencias").innerHTML = "";
  try {
    await api.rechazarSugerencia(boton.dataset.a, boton.dataset.b);
    $("#mensaje-sugerencias").innerHTML =
      '<p class="nota">Anotado: esas dos acciones no son la misma.</p>';
    await cargarSugerencias(filtrosDeLaUrl());
  } catch (error) {
    $("#mensaje-sugerencias").innerHTML = `
      <p class="aviso error">${escapar(error.message)}</p>`;
    boton.disabled = false;
  }
});

function pintar(filtros) {
  dibujarTablero($("#tablero"), ultimo, cargadas);
  marcarTarjeta($("#tablero"), panel.abierta());
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

/**
 * `conservar`: tras una corrección, se vuelven a pedir tantas acciones como
 * había cargadas y se mantiene la posición, en vez de volver a la primera
 * página con un «Cargando…» que haría saltar la lista.
 */
async function cargar({ conservar = false } = {}) {
  const filtros = filtrosDeLaUrl();
  volcarEnElFormulario(filtros);
  cargarSugerencias(filtros);
  if (!conservar) $("#tablero").innerHTML = '<div class="aviso">Cargando…</div>';
  const posicion = window.scrollY;
  try {
    ultimo = await api.acciones({
      ...parametros(filtros),
      limite: conservar ? Math.max(cargadas.length, 1) : undefined,
    });
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
  if (conservar) window.scrollTo(0, posicion);
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
iniciarAtajoDeBusqueda();
cargar();
pintarSalud($("#salud"));
