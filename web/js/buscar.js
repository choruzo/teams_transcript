// Orquestador de la búsqueda (`/buscar.html?q=…`, fase I4).
//
// Mismo reparto que las otras tres páginas: aquí se decide qué se pide y con
// qué filtros, y `vistas/busqueda.js` solo pinta. Los filtros —la consulta
// incluida— viven en la URL, porque «dónde dijimos lo del certificado» es
// justo lo que se pega en un chat.

import { api } from "./api.js";
import { iniciarTema } from "./tema.js";
import { escapar, plural } from "./formato.js";
import {
  dibujarAyuda,
  dibujarResultados,
  dibujarReunionesDeBusqueda,
} from "./vistas/busqueda.js";
import { pintarSalud } from "./vistas/salud.js";
import { iniciarAtajoDeBusqueda } from "./buscador.js";
import { urlBuscar } from "./enlaces.js";

const $ = (selector) => document.querySelector(selector);

const CAMPOS = ["q", "tipo", "desde", "hasta", "orden"];

// El orden que aplica la API si no se le dice nada. No viaja en la URL: es
// ruido en un enlace que se comparte, como en el tablero.
const ORDEN_POR_DEFECTO = "relevancia";

let cargadas = [];
let ultimo = null;

function filtrosDeLaUrl() {
  const parametros = new URLSearchParams(window.location.search);
  const filtros = {};
  for (const campo of [...CAMPOS, "uid"]) {
    const valor = parametros.get(campo);
    if (valor) filtros[campo] = valor;
  }
  return filtros;
}

function volcarEnElFormulario(filtros) {
  for (const campo of CAMPOS) $(`#${campo}`).value = filtros[campo] || "";
  $("#orden").value = filtros.orden || ORDEN_POR_DEFECTO;
}

function leerFormulario() {
  const filtros = { ...filtrosDeLaUrl() };
  for (const campo of CAMPOS) {
    const valor = $(`#${campo}`).value.trim();
    if (valor) filtros[campo] = valor;
    else delete filtros[campo];
  }
  if (filtros.orden === ORDEN_POR_DEFECTO) delete filtros.orden;
  return filtros;
}

// Filtrar es navegar, como en el tablero: el botón «atrás» deshace el último
// filtro y la dirección siempre describe lo que se está viendo.
const aplicar = (filtros) => {
  window.location.href = urlBuscar(filtros);
};

function pintarError(mensaje) {
  $("#resultados").innerHTML = `
    <div class="aviso error">
      <strong>No se ha podido buscar.</strong>
      <code>${escapar(mensaje)}</code>
    </div>`;
  $("#reuniones").innerHTML = "";
  $("#total").textContent = "";
}

const parametros = (filtros, desplazamiento = 0) => ({
  q: filtros.q,
  uid: filtros.uid,
  tipo: filtros.tipo,
  desde: filtros.desde,
  hasta: filtros.hasta,
  orden: filtros.orden,
  desplazamiento,
});

function pintar(filtros) {
  dibujarResultados($("#resultados"), ultimo, cargadas);
  $("#total").textContent = ultimo.total
    ? plural(ultimo.total, "coincidencia", "coincidencias")
    : "";
  const mas = $("#mas");
  if (!mas) return;
  mas.addEventListener("click", async () => {
    mas.disabled = true;
    mas.textContent = "Cargando…";
    try {
      const pagina = await api.buscar(parametros(filtros, cargadas.length));
      cargadas.push(...pagina.coincidencias);
      ultimo = pagina;
      pintar(filtros);
    } catch (error) {
      pintarError(error.message);
    }
  });
}

async function cargar() {
  const filtros = filtrosDeLaUrl();
  volcarEnElFormulario(filtros);
  document.title = filtros.q
    ? `${filtros.q} · Buscar · Memoria del equipo`
    : "Buscar · Memoria del equipo";

  if (!filtros.q) {
    // Sin consulta no se pide nada: una búsqueda vacía no tiene resultados
    // que enseñar, y el sitio de la ayuda es justo ese hueco.
    ultimo = null;
    cargadas = [];
    dibujarAyuda($("#resultados"));
    $("#reuniones").innerHTML = "";
    $("#total").textContent = "";
    return;
  }

  $("#resultados").innerHTML = '<div class="aviso">Buscando…</div>';
  try {
    ultimo = await api.buscar(parametros(filtros));
    cargadas = ultimo.coincidencias.slice();
  } catch (error) {
    pintarError(error.message);
    return;
  }
  dibujarReunionesDeBusqueda($("#reuniones"), ultimo, filtros.uid);
  pintar(filtros);
}

// Un chip de reunión acota la búsqueda a esa reunión, y vuelve a soltarla si
// ya estaba puesta.
$("#reuniones").addEventListener("click", (evento) => {
  const boton = evento.target.closest("[data-uid]");
  if (!boton) return;
  const filtros = leerFormulario();
  if (filtros.uid === boton.dataset.uid) delete filtros.uid;
  else filtros.uid = boton.dataset.uid;
  aplicar(filtros);
});

$("#filtros").addEventListener("submit", (evento) => {
  evento.preventDefault();
  aplicar(leerFormulario());
});

$("#limpiar").addEventListener("click", () => aplicar({}));

for (const id of ["tipo", "orden"]) {
  $(`#${id}`).addEventListener("change", () => aplicar(leerFormulario()));
}

iniciarTema();
// En esta página el atajo enfoca el campo en vez de navegar: ya se está aquí.
iniciarAtajoDeBusqueda($("#q"));
cargar();
pintarSalud($("#salud"));
