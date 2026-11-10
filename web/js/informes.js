// Orquestador de la página de informes (`/informes.html`, I8). Decide qué se
// pide y con qué filtros; el Markdown lo compone el motor (`report_teams.py`)
// y `vistas/informe.js` solo lo pinta y lo descarga.
//
// Mismo reparto que en el resto del front: los filtros viven en la URL para que
// «el informe de noviembre de Ana» se pueda guardar o pegar en un chat. Cambiar
// de periodo o de responsable **navega**, porque cambia el informe entero; la
// lectura del modelo se pide a mano porque tarda y no siempre hace falta.

import { api } from "./api.js";
import { iniciarTema } from "./tema.js";
import { escapar, fechaCorta } from "./formato.js";
import {
  aHtml,
  conLectura,
  descargar,
  nombreDeArchivo,
} from "./vistas/informe.js";
import { pintarSalud } from "./vistas/salud.js";
import { urlInforme } from "./enlaces.js";
import { iniciarAtajoDeBusqueda } from "./buscador.js";

const $ = (selector) => document.querySelector(selector);

// El mismo que aplica la API cuando no se le dice nada: el mes en curso es la
// pregunta normal («¿qué ha pasado este mes?») y el histórico completo es el
// enlace, no el punto de partida.
const PERIODO_POR_DEFECTO = "mes";
const CAMPOS = ["desde", "hasta", "persona", "periodo"];

let informe = null;
// La lectura del modelo, que llega después del informe y se guarda aparte para
// no rehacer el Markdown entero en cada trozo.
let narrativa = "";
let estado = "inactivo"; // inactivo | escribiendo | hecho | error
let avisoError = "";

function filtrosDeLaUrl() {
  const parametros = new URLSearchParams(window.location.search);
  const filtros = { sugerencias: parametros.get("sugerencias") === "1" };
  for (const campo of CAMPOS) {
    const valor = parametros.get(campo);
    if (valor) filtros[campo] = valor;
  }
  if (!filtros.periodo && !filtros.desde && !filtros.hasta) {
    filtros.periodo = PERIODO_POR_DEFECTO;
  }
  return filtros;
}

function urlDe(filtros) {
  // `urlInforme` es la que comparten esta página y el enlace de las métricas,
  // así que la dirección que queda aquí se puede abrir desde allí igual.
  return urlInforme({
    ...filtros,
    sugerencias: filtros.sugerencias ? "1" : undefined,
  });
}

const aplicar = (filtros) => {
  window.location.href = urlDe(filtros);
};

function volcarEnElFormulario(filtros) {
  for (const campo of ["desde", "hasta", "persona"]) {
    $(`#${campo}`).value = filtros[campo] || "";
  }
  $("#sugerencias").checked = Boolean(filtros.sugerencias);
}

function marcarPeriodoActivo(filtros) {
  // Un rango escrito a mano no pertenece a ningún atajo: entonces no se marca
  // ninguno, que es lo honesto.
  const conFechas = Boolean(filtros.desde || filtros.hasta);
  for (const boton of document.querySelectorAll("[data-periodo]")) {
    boton.setAttribute(
      "aria-pressed",
      String(!conFechas && filtros.periodo === boton.dataset.periodo)
    );
  }
}

function leerFormulario() {
  const filtros = { ...filtrosDeLaUrl() };
  for (const campo of ["desde", "hasta", "persona"]) {
    const valor = $(`#${campo}`).value.trim();
    if (valor) filtros[campo] = valor;
    else delete filtros[campo];
  }
  filtros.sugerencias = $("#sugerencias").checked;
  // Las fechas escritas a mano mandan sobre el atajo; si hay una, el periodo
  // deja de tener sentido y no debe viajar en la dirección.
  if (filtros.desde || filtros.hasta) delete filtros.periodo;
  else filtros.periodo = filtros.periodo || PERIODO_POR_DEFECTO;
  return filtros;
}

/** Lo que espera la API. `undefined` deja el parámetro fuera de la URL. */
function parametros(filtros) {
  return {
    periodo: filtros.periodo,
    desde: filtros.desde,
    hasta: filtros.hasta,
    persona: filtros.persona,
    sugerencias: filtros.sugerencias ? "true" : undefined,
  };
}

/**
 * El desplegable de responsables, con las personas que la base conoce.
 *
 * Las opciones se construyen con `createElement` y no con una plantilla HTML:
 * un nombre con comillas rompería el atributo `value` y `escapar()` no escapa
 * comillas (solo `&`, `<` y `>`), que para texto es correcto y para un
 * atributo no.
 */
async function cargarPersonas(elegida) {
  const select = $("#persona");
  let personas = [];
  try {
    personas = await api.personas();
  } catch (_) {
    personas = [];
  }
  select.length = 1; // «Todo el equipo»
  const nombres = personas.map((p) => p.nombre);
  if (elegida && !nombres.includes(elegida)) nombres.push(elegida);
  for (const nombre of nombres) {
    const opcion = document.createElement("option");
    opcion.value = nombre;
    opcion.textContent = nombre;
    select.appendChild(opcion);
  }
  select.value = elegida || "";
}

function textoPeriodo(datos) {
  if (datos.desde && datos.hasta) {
    return `del ${fechaCorta(datos.desde)} al ${fechaCorta(datos.hasta)}`;
  }
  if (datos.desde) return `desde el ${fechaCorta(datos.desde)}`;
  if (datos.hasta) return `hasta el ${fechaCorta(datos.hasta)}`;
  return "histórico completo";
}

function pintarInforme() {
  $("#informe").innerHTML = aHtml(informe.markdown);
  $("#periodo").textContent = `· ${textoPeriodo(informe)}`;
}

/**
 * La sección de lectura dentro del informe, creada la primera vez.
 *
 * Va antes de la primera sección para que el orden de la pantalla sea el mismo
 * que el del `.md` que se descarga (`## Lectura` y después `## Actividad`).
 */
function seccionLectura() {
  let seccion = $("#informe .lectura");
  if (seccion) return seccion;
  seccion = document.createElement("section");
  seccion.className = "lectura";
  seccion.innerHTML = '<h2>Lectura</h2><div class="lectura-texto"></div>';
  $("#informe").insertBefore(seccion, $("#informe h2"));
  return seccion;
}

/** Solo repinta la lectura: el informe no se toca en cada trozo del stream. */
function pintarLectura() {
  const visible = Boolean(narrativa) || estado === "escribiendo" || estado === "error";
  const existente = $("#informe .lectura");
  if (!visible) {
    existente?.remove();
    return;
  }
  const caja = seccionLectura().querySelector(".lectura-texto");
  caja.innerHTML = narrativa ? aHtml(narrativa) : "";
  if (estado === "error") {
    // Se conserva lo que el modelo llegara a escribir: perder media lectura
    // porque se cortó al final es peor que enseñarla con su aviso debajo.
    caja.insertAdjacentHTML(
      "beforeend",
      `<p class="aviso error">${escapar(avisoError)}</p>`
    );
  }
  if (!caja.innerHTML) {
    caja.innerHTML = '<p class="aviso">Escribiendo… puede tardar un minuto.</p>';
  }
}

function actualizarBotones() {
  const escribiendo = estado === "escribiendo";
  const narrar = $("#narrar");
  narrar.disabled = escribiendo || !informe;
  narrar.textContent = escribiendo
    ? "Escribiendo…"
    : narrativa
      ? "Volver a escribir la lectura"
      : "Escribir la lectura";
  $("#descargar").disabled = !informe;
  $("#estado-narrativa").textContent = escribiendo
    ? "Consultando al modelo…"
    : estado === "hecho"
      ? "Lectura lista."
      : "";
}

function narrar() {
  if (estado === "escribiendo" || !informe) return;
  estado = "escribiendo";
  narrativa = "";
  avisoError = "";
  pintarLectura();
  actualizarBotones();
  api.narrativaInforme(parametros(filtrosDeLaUrl()), {
    texto: (trozo) => {
      narrativa += trozo;
      pintarLectura();
    },
    fin: () => {
      estado = "hecho";
      pintarLectura();
      actualizarBotones();
    },
    error: (mensaje) => {
      estado = "error";
      avisoError = mensaje;
      pintarLectura();
      actualizarBotones();
    },
  });
}

async function cargar() {
  const filtros = filtrosDeLaUrl();
  volcarEnElFormulario(filtros);
  marcarPeriodoActivo(filtros);
  $("#informe").innerHTML = '<div class="aviso">Cargando…</div>';
  cargarPersonas(filtros.persona);
  try {
    informe = await api.informe(parametros(filtros));
  } catch (error) {
    informe = null;
    $("#informe").innerHTML = `
      <div class="aviso error">
        <strong>No se ha podido generar el informe.</strong>
        <code>${escapar(error.message)}</code>
      </div>`;
    actualizarBotones();
    return;
  }
  pintarInforme();
  actualizarBotones();
}

$("#filtros").addEventListener("submit", (evento) => {
  evento.preventDefault();
  aplicar(leerFormulario());
});

// Volver al punto de partida: el mes en curso, todo el equipo, sin duplicados.
$("#limpiar").addEventListener("click", () => aplicar({}));

for (const boton of document.querySelectorAll("[data-periodo]")) {
  boton.addEventListener("click", () => {
    const filtros = leerFormulario();
    delete filtros.desde;
    delete filtros.hasta;
    aplicar({ ...filtros, periodo: boton.dataset.periodo });
  });
}

// Los controles de valor cerrado filtran al cambiar; el campo de fecha y el de
// texto esperan al botón.
$("#persona").addEventListener("change", () => aplicar(leerFormulario()));
$("#sugerencias").addEventListener("change", () => aplicar(leerFormulario()));

$("#narrar").addEventListener("click", narrar);

$("#descargar").addEventListener("click", () => {
  if (!informe) return;
  // Se descarga exactamente lo que hay en pantalla, con la lectura si se pidió.
  descargar(conLectura(informe.markdown, narrativa), nombreDeArchivo(informe));
});

iniciarTema();
iniciarAtajoDeBusqueda();
pintarSalud($("#salud"));
cargar();
