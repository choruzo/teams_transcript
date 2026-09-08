// Listado de reuniones. I0: solo lectura, sin enrutado todavia (la vista de
// reunion es I2). Los filtros viven en la URL para que una vista concreta se
// pueda guardar en marcadores o pasar por chat.

import { api } from "./api.js";

const $ = (selector) => document.querySelector(selector);

const CAMPOS = ["desde", "hasta", "tipo"];

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

function escapar(texto) {
  const nodo = document.createElement("div");
  nodo.textContent = texto ?? "";
  return nodo.innerHTML;
}

function duracion(segundos) {
  if (!segundos) return null;
  const minutos = Math.round(segundos / 60);
  if (minutos < 60) return `${minutos} min`;
  const horas = Math.floor(minutos / 60);
  return `${horas} h ${String(minutos % 60).padStart(2, "0")} min`;
}

function tarjeta(reunion) {
  const partes = [];
  const dur = duracion(reunion.duracion_seg);
  if (dur) partes.push(`<span>${dur}</span>`);
  partes.push(`<span><b>${reunion.n_segmentos}</b> segmentos</span>`);
  partes.push(`<span><b>${reunion.n_acciones}</b> acciones</span>`);
  if (reunion.n_riesgos) {
    partes.push(`<span><b>${reunion.n_riesgos}</b> riesgos</span>`);
  }
  if (reunion.tiene_audio) partes.push("<span>con audio</span>");

  return `
    <article class="reunion">
      <div class="reunion-cabecera">
        <h2>${escapar(reunion.titulo || reunion.fecha)}</h2>
        <span class="etiqueta">${escapar(reunion.tipo)}</span>
      </div>
      <div class="uid">${escapar(reunion.fecha)} · ${escapar(reunion.uid)}</div>
      ${reunion.resumen ? `<p class="resumen">${escapar(reunion.resumen)}</p>` : ""}
      <div class="cifras">${partes.join("")}</div>
    </article>`;
}

function pintarError(mensaje) {
  $("#listado").innerHTML = `
    <div class="aviso error">
      <strong>No se han podido cargar las reuniones.</strong>
      <code>${escapar(mensaje)}</code>
    </div>`;
}

async function cargar() {
  const filtros = filtrosDeLaUrl();
  volcarFiltrosEnElFormulario(filtros);
  $("#listado").innerHTML = '<div class="aviso">Cargando…</div>';

  try {
    const pagina = await api.reuniones(filtros);
    if (pagina.total === 0) {
      // Distinguir "no hay nada" de "el filtro no encuentra nada": con una
      // base casi vacia, lo primero es lo normal y no un error.
      const hayFiltros = Object.keys(filtros).length > 0;
      $("#listado").innerHTML = `<div class="aviso">${
        hayFiltros
          ? "Ninguna reunión coincide con el filtro."
          : "Todavía no hay reuniones. Procesa una con summarize_teams.py."
      }</div>`;
      $("#total").textContent = "";
      return;
    }
    $("#total").textContent =
      pagina.total === 1 ? "1 reunión" : `${pagina.total} reuniones`;
    $("#listado").innerHTML = pagina.reuniones.map(tarjeta).join("");
  } catch (error) {
    pintarError(error.message);
    $("#total").textContent = "";
  }
}

async function pintarSalud() {
  const pie = $("#salud");
  try {
    const salud = await api.salud();
    if (!salud.base_accesible) {
      pie.innerHTML = `<span class="mal">Base de datos inaccesible:</span> ${escapar(
        salud.detalle || salud.base_de_datos
      )}`;
      return;
    }
    const trozos = [
      `v${escapar(salud.version)}`,
      `esquema ${salud.esquema}`,
      `${salud.reuniones} reuniones`,
      `${salud.acciones_abiertas} acciones abiertas`,
    ];
    if (salud.solo_lectura) trozos.push("solo lectura");
    pie.textContent = trozos.join(" · ");
    if (salud.detalle) {
      pie.innerHTML += ` — <span class="mal">${escapar(salud.detalle)}</span>`;
    }
  } catch (error) {
    pie.innerHTML = `<span class="mal">${escapar(error.message)}</span>`;
  }
}

$("#filtros").addEventListener("submit", (evento) => {
  evento.preventDefault();
  const parametros = new URLSearchParams();
  for (const campo of CAMPOS) {
    const valor = $(`#${campo}`).value.trim();
    if (valor) parametros.set(campo, valor);
  }
  const consulta = parametros.toString();
  // Cambiar la URL sin recargar: el enlace sigue siendo compartible.
  history.replaceState(null, "", consulta ? `?${consulta}` : location.pathname);
  cargar();
});

$("#limpiar").addEventListener("click", () => {
  history.replaceState(null, "", location.pathname);
  cargar();
});

cargar();
pintarSalud();
