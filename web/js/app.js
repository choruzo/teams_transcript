// Orquestador de la página. Decide qué se pide, con qué filtros, y reparte a
// las vistas de js/vistas/. Ninguna vista pide datos por su cuenta ni conoce la
// URL: la navegación a la vista de reunión (I2) se decide aquí y en ningún
// otro sitio, con las direcciones de js/enlaces.js.
//
// Los filtros viven en la URL (`?desde=&hasta=&tipo=`) para que una vista
// concreta se pueda guardar en marcadores o pasar por chat. Los "periodos"
// (7/30/90 días) no son un estado aparte: escriben esas mismas fechas, que es
// lo que hace que el zoom del timeline sea compartible.

import { api } from "./api.js";
import { iniciarTema } from "./tema.js";
import { escapar, iso, plural } from "./formato.js";
import { dibujarMetricas } from "./vistas/metricas.js";
import { dibujarTimeline, marcarSeleccionada } from "./vistas/timeline.js";
import { crearPanel } from "./vistas/panel_accion.js";
import { dibujarListado } from "./vistas/listado.js";
import { pintarSalud } from "./vistas/salud.js";
import { urlReunion } from "./enlaces.js";
import { iniciarAtajoDeBusqueda } from "./buscador.js";

const $ = (selector) => document.querySelector(selector);

const CAMPOS = ["desde", "hasta", "tipo"];

// Lo último que se pintó, para poder redibujar en un cambio de tamaño sin
// volver a pedirlo al servidor (el SVG se calcula en píxeles reales, no en
// unidades relativas, así que un `resize` obliga a rehacerlo).
let ultimo = null;

function filtrosDeLaUrl() {
  const parametros = new URLSearchParams(window.location.search);
  const filtros = {};
  for (const campo of CAMPOS) {
    const valor = parametros.get(campo);
    if (valor) filtros[campo] = valor;
  }
  return filtros;
}

function volcarFiltrosEnElFormulario(filtros) {
  for (const campo of CAMPOS) {
    $(`#${campo}`).value = filtros[campo] || "";
  }
}

function aplicarFiltros(filtros) {
  const parametros = new URLSearchParams();
  for (const campo of CAMPOS) {
    if (filtros[campo]) parametros.set(campo, filtros[campo]);
  }
  if (verDescartadas()) parametros.set("descartadas", "1");
  const consulta = parametros.toString();
  // Cambiar la URL sin recargar: el enlace sigue siendo compartible.
  history.replaceState(null, "", consulta ? `?${consulta}` : location.pathname);
  cargar();
}

function periodo(dias) {
  const hasta = new Date();
  const desde = new Date();
  desde.setDate(hasta.getDate() - dias);
  return { ...filtrosDeLaUrl(), desde: iso(desde), hasta: iso(hasta) };
}

// El destino de pulsar una reunión en el timeline: su vista (I2). Pulsar una
// acción abre su ficha en el panel lateral (I6a); ir a su última reunión es
// ahora un botón dentro del panel. Este es el único sitio donde se decide.
const abrirReunion = (uid) => {
  window.location.href = urlReunion(uid);
};

// «Ver descartadas» vive en la URL como el resto de filtros, pero no en el
// formulario: solo cambia el timeline, así que no pide las otras dos cosas.
const verDescartadas = () =>
  new URLSearchParams(window.location.search).get("descartadas") === "1";

const panel = crearPanel({
  // Tras guardar se repinta sin recargar la página: se conserva el filtro, la
  // posición de la lista de carriles y el panel abierto.
  alCambiar: () => recargarTimeline(),
  // En pantallas anchas el panel empuja la página en vez de taparla, así que
  // el SVG, que va en píxeles, hay que rehacerlo al abrir y al cerrar.
  alAlternar: () => dibujarTodo(),
  alSeleccionar: (uid) => marcarSeleccionada($("#timeline"), uid),
  elementoDe: (uid) =>
    document.querySelector(`.tl-carril[data-accion="${CSS.escape(uid)}"]`),
});

const manejadores = () => ({
  alReunion: abrirReunion,
  alAccion: (uid, elemento) => panel.abrir(uid, elemento),
  seleccionada: panel.abierta(),
});

const filtrosDelTimeline = (filtros) =>
  verDescartadas() ? { ...filtros, descartadas: "true" } : filtros;

/** Vuelve a pedir el timeline y las métricas (una descartada deja de contar). */
async function recargarTimeline() {
  const filtros = filtrosDeLaUrl();
  ultimo = ultimo || {};
  const desplazamiento = $("#timeline .tl-scroll")?.scrollTop || 0;
  const [timeline, metricas] = await Promise.allSettled([
    api.timeline(filtrosDelTimeline(filtros)),
    api.metricas({ desde: filtros.desde, hasta: filtros.hasta }),
  ]);
  if (timeline.status === "fulfilled") {
    ultimo.timeline = timeline.value;
    dibujarTimeline($("#timeline"), timeline.value, manejadores());
    const caja = $("#timeline .tl-scroll");
    if (caja) caja.scrollTop = desplazamiento;
  }
  if (metricas.status === "fulfilled") {
    ultimo.metricas = metricas.value;
    dibujarMetricas($("#metricas"), metricas.value);
  }
}

function pintarError(donde, mensaje) {
  $(donde).innerHTML = `
    <div class="aviso error">
      <strong>No se han podido cargar los datos.</strong>
      <code>${escapar(mensaje)}</code>
    </div>`;
}

function marcarPeriodoActivo(filtros) {
  // "Todo" solo esta activo si no hay ninguna fecha; un rango escrito a mano
  // que no coincide con ningun preset no marca ninguno, que es lo honesto.
  const dias =
    filtros.desde && filtros.hasta
      ? Math.round((new Date(filtros.hasta) - new Date(filtros.desde)) / 86400000)
      : filtros.desde || filtros.hasta
        ? -1
        : null;
  for (const boton of document.querySelectorAll("[data-dias]")) {
    const suyo = boton.dataset.dias === "" ? null : Number(boton.dataset.dias);
    boton.setAttribute("aria-pressed", String(suyo === dias));
  }
}

function dibujarTodo() {
  if (!ultimo) return;
  // Lo que falló al cargar se queda con su mensaje de error en pantalla: un
  // cambio de tamaño no es motivo para reintentar la petición.
  if (ultimo.timeline) {
    const caja = $("#timeline .tl-scroll");
    const desplazamiento = caja ? caja.scrollTop : 0;
    dibujarTimeline($("#timeline"), ultimo.timeline, manejadores());
    const nueva = $("#timeline .tl-scroll");
    if (nueva) nueva.scrollTop = desplazamiento;
  }
  if (ultimo.metricas) dibujarMetricas($("#metricas"), ultimo.metricas);
}

async function cargar() {
  const filtros = filtrosDeLaUrl();
  volcarFiltrosEnElFormulario(filtros);
  marcarPeriodoActivo(filtros);
  $("#timeline").innerHTML = '<div class="aviso">Cargando…</div>';
  $("#listado").innerHTML = '<div class="aviso">Cargando…</div>';

  // Las tres peticiones van en paralelo: son independientes y contra SQLite
  // local cuestan milisegundos, pero encadenarlas triplicaría la espera en el
  // túnel SSH, que es como se va a usar esto de verdad.
  const [timeline, metricas, pagina] = await Promise.allSettled([
    api.timeline(filtrosDelTimeline(filtros)),
    // El panel de salud no se filtra por tipo: ver api/rutas/metricas.py.
    api.metricas({ desde: filtros.desde, hasta: filtros.hasta }),
    api.reuniones(filtros),
  ]);

  ultimo = {
    timeline: timeline.status === "fulfilled" ? timeline.value : null,
    metricas: metricas.status === "fulfilled" ? metricas.value : null,
  };

  if (timeline.status === "fulfilled") {
    dibujarTimeline($("#timeline"), timeline.value, manejadores());
  } else {
    pintarError("#timeline", timeline.reason.message);
  }

  if (metricas.status === "fulfilled") {
    dibujarMetricas($("#metricas"), metricas.value);
  } else {
    pintarError("#metricas", metricas.reason.message);
  }

  if (pagina.status === "fulfilled") {
    dibujarListado($("#listado"), pagina.value, Object.keys(filtros).length > 0);
    $("#total").textContent = pagina.value.total
      ? plural(pagina.value.total, "reunión", "reuniones")
      : "";
  } else {
    pintarError("#listado", pagina.reason.message);
    $("#total").textContent = "";
  }
}

$("#filtros").addEventListener("submit", (evento) => {
  evento.preventDefault();
  const filtros = {};
  for (const campo of CAMPOS) {
    const valor = $(`#${campo}`).value.trim();
    if (valor) filtros[campo] = valor;
  }
  aplicarFiltros(filtros);
});

$("#limpiar").addEventListener("click", () => aplicarFiltros({}));

$("#descartadas").checked = verDescartadas();
$("#descartadas").addEventListener("change", (evento) => {
  const parametros = new URLSearchParams(window.location.search);
  if (evento.target.checked) parametros.set("descartadas", "1");
  else parametros.delete("descartadas");
  const consulta = parametros.toString();
  history.replaceState(null, "", consulta ? `?${consulta}` : location.pathname);
  recargarTimeline();
});

for (const boton of document.querySelectorAll("[data-dias]")) {
  boton.addEventListener("click", () => {
    const dias = boton.dataset.dias;
    if (dias === "") {
      const { desde, hasta, ...resto } = filtrosDeLaUrl();
      aplicarFiltros(resto);
    } else {
      aplicarFiltros(periodo(Number(dias)));
    }
  });
}

// El SVG se dibuja en píxeles y no se reajusta solo. Se espera a que el
// usuario suelte el borde de la ventana para no rehacerlo sesenta veces por
// segundo.
let temporizador;
window.addEventListener("resize", () => {
  clearTimeout(temporizador);
  temporizador = setTimeout(dibujarTodo, 150);
});

iniciarTema();
iniciarAtajoDeBusqueda();
cargar();
pintarSalud($("#salud"));
