// Orquestador del chat (`/chat.html`, fase I5).
//
// Mismo reparto que las otras cuatro páginas: aquí se decide qué se pregunta
// y cuándo, y `vistas/chat.js` solo pinta.
//
// Dos diferencias con el resto del front, y las dos son deliberadas:
//
//   - **El historial vive en la pestaña**, en `sessionStorage`. El chat es de
//     solo lectura y guardarlo en la base rompería `TEAMS_API_SOLO_LECTURA`;
//     guardarlo en `localStorage` dejaría la conversación de una tarde
//     esperando en el navegador de un portátil compartido.
//   - **Filtrar no es navegar.** En el tablero y en la búsqueda los filtros
//     van en la URL porque la vista se comparte; aquí lo que se comparte es
//     la respuesta (botón de exportar), y recargar la página a mitad de una
//     conversación la perdería.

import { api } from "./api.js";
import { iniciarTema } from "./tema.js";
import { escapar } from "./formato.js";
import {
  aMarkdown,
  dibujarAyuda,
  dibujarPregunta,
  dibujarRespuesta,
  pintarEstado,
} from "./vistas/chat.js";
import { pintarSalud } from "./vistas/salud.js";
import { iniciarAtajoDeBusqueda } from "./buscador.js";

const $ = (selector) => document.querySelector(selector);

const CLAVE_HISTORIAL = "chat.turnos";

// Cuántos turnos previos se le mandan al modelo. El servidor acepta 20; aquí
// se cortan antes porque cada turno se paga en contexto y una conversación
// larga acaba desplazando a las fuentes, que es lo que de verdad importa.
const TURNOS_DE_CONTEXTO = 6;

let turnos = leerHistorial();
let estado = null;
let enCurso = null;

function leerHistorial() {
  try {
    return JSON.parse(sessionStorage.getItem(CLAVE_HISTORIAL)) || [];
  } catch (_) {
    return [];
  }
}

function guardarHistorial() {
  try {
    sessionStorage.setItem(CLAVE_HISTORIAL, JSON.stringify(turnos));
  } catch (_) {
    /* modo privado o cuota llena: la conversación sigue, solo no se recuerda */
  }
}

function pintar({ alFinal = true } = {}) {
  const conversacion = $("#conversacion");
  if (!turnos.length) {
    dibujarAyuda(conversacion);
    return;
  }
  conversacion.innerHTML = turnos
    .map((turno) =>
      turno.papel === "user" ? dibujarPregunta(turno.texto) : dibujarRespuesta(turno)
    )
    .join("");
  if (alFinal) conversacion.lastElementChild?.scrollIntoView({ block: "end" });
}

function marcarOcupado(ocupado) {
  $("#preguntar").disabled = ocupado;
  $("#parar").hidden = !ocupado;
  $("#q").disabled = false; // se puede ir escribiendo la siguiente
}

function preguntar(texto) {
  const pregunta = texto.trim();
  if (!pregunta || enCurso) return;

  turnos.push({ papel: "user", texto: pregunta });
  const respuesta = {
    papel: "assistant",
    texto: "",
    fuentes: null,
    modo: null,
    aviso: null,
    estado: "escribiendo",
  };
  turnos.push(respuesta);
  pintar();
  marcarOcupado(true);

  const terminar = () => {
    if (respuesta.estado === "escribiendo") respuesta.estado = "listo";
    enCurso = null;
    marcarOcupado(false);
    guardarHistorial();
    pintar();
  };

  enCurso = api.chat(
    {
      pregunta,
      historial: contexto(),
      desde: $("#desde").value || null,
      hasta: $("#hasta").value || null,
      persona: $("#persona").value.trim() || null,
      tipo: $("#tipo").value || null,
    },
    {
      fuentes: (dato) => {
        respuesta.fuentes = dato.fuentes;
        respuesta.modo = dato.modo;
        respuesta.aviso = dato.aviso;
        pintar();
      },
      texto: (trozo) => {
        respuesta.texto += trozo;
        pintar();
      },
      error: (mensaje) => {
        respuesta.estado = "error";
        respuesta.error = mensaje;
        terminar();
      },
      fin: terminar,
      // El stream puede acabar sin `fin` si el servidor se corta: sin esto la
      // página se quedaría con el botón bloqueado para siempre.
      cierre: terminar,
    }
  );
}

/** Los turnos previos, ya completos, que se le mandan al modelo. */
function contexto() {
  return turnos
    .filter((t) => t.papel === "user" || (t.estado === "listo" && t.texto))
    .slice(-TURNOS_DE_CONTEXTO)
    .map((t) => ({ role: t.papel, content: t.texto }));
}

function exportar() {
  const blob = new Blob([aMarkdown(turnos)], { type: "text/markdown" });
  const enlace = document.createElement("a");
  enlace.href = URL.createObjectURL(blob);
  enlace.download = "consulta.md";
  enlace.click();
  URL.revokeObjectURL(enlace.href);
}

$("#filtros").addEventListener("submit", (evento) => {
  evento.preventDefault();
  const campo = $("#q");
  preguntar(campo.value);
  campo.value = "";
});

$("#parar").addEventListener("click", () => {
  enCurso?.abort();
  const ultimo = turnos[turnos.length - 1];
  if (ultimo && ultimo.estado === "escribiendo") {
    ultimo.estado = ultimo.texto ? "listo" : "error";
    if (!ultimo.texto) ultimo.error = "Respuesta cancelada.";
  }
  enCurso = null;
  marcarOcupado(false);
  guardarHistorial();
  pintar();
});

$("#limpiar").addEventListener("click", () => {
  enCurso?.abort();
  enCurso = null;
  turnos = [];
  guardarHistorial();
  marcarOcupado(false);
  pintar();
});

$("#exportar").addEventListener("click", exportar);

// Las preguntas sugeridas de la ayuda.
$("#conversacion").addEventListener("click", (evento) => {
  const boton = evento.target.closest(".sugerencia");
  if (boton) preguntar(boton.textContent);
});

async function cargarEstado() {
  try {
    estado = await api.estadoDelChat();
  } catch (error) {
    estado = { disponible: false, motivo: error.message, busqueda: "literal" };
  }
  pintarEstado($("#estado"), estado);
  if (!turnos.length) pintar();
}

iniciarTema();
// El atajo global navega a la búsqueda: aquí no hay campo de búsqueda literal,
// y el de preguntar es otra cosa.
iniciarAtajoDeBusqueda();
pintar({ alFinal: false });
cargarEstado();
pintarSalud($("#salud"));

// Una pregunta puede venir en la dirección (`/chat.html?q=…`) para poder
// enlazar «pregúntale esto», que es lo único de esta vista que tiene sentido
// compartir por URL.
const inicial = new URLSearchParams(window.location.search).get("q");
if (inicial && !turnos.length) preguntar(inicial);

// Sin esto, un fallo de red al pintar dejaría la consola muda y la página
// aparentemente colgada.
window.addEventListener("unhandledrejection", (evento) => {
  const conversacion = $("#conversacion");
  conversacion.insertAdjacentHTML(
    "beforeend",
    `<div class="aviso error"><code>${escapar(String(evento.reason))}</code></div>`
  );
});
