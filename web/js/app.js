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
import { dibujarTimeline } from "./vistas/timeline.js";
import { dibujarListado } from "./vistas/listado.js";
import { pintarSalud } from "./vistas/salud.js";
import { urlReunion } from "./enlaces.js";

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

// El destino de pulsar una reunión o una acción en el timeline. En I1 era
// resaltar su tarjeta del listado; desde I2 hay una vista de reunión a la que
// ir, y este es el único sitio donde se decide.
const abrirReunion = (uid) => {
  window.location.href = urlReunion(uid);
};

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
  if (ultimo.timeline) dibujarTimeline($("#timeline"), ultimo.timeline, abrirReunion);
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
    api.timeline(filtros),
    // El panel de salud no se filtra por tipo: ver api/rutas/metricas.py.
    api.metricas({ desde: filtros.desde, hasta: filtros.hasta }),
    api.reuniones(filtros),
  ]);

  ultimo = {
    timeline: timeline.status === "fulfilled" ? timeline.value : null,
    metricas: metricas.status === "fulfilled" ? metricas.value : null,
  };

  if (timeline.status === "fulfilled") {
    dibujarTimeline($("#timeline"), timeline.value, abrirReunion);
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
cargar();
pintarSalud($("#salud"));
