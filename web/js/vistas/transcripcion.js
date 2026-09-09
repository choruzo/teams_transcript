// La transcripción de una reunión: lo que de verdad hace que no haya que
// abrir un fichero para saber qué se dijo (criterio de aceptación de I2).
//
// Dos decisiones que se notan al leerla:
//
// 1. **Se agrupa por hablante consecutivo.** Whisper corta en segmentos de
//    unos pocos segundos; una lista de trescientas líneas con el mismo nombre
//    repetido delante no se lee. El nombre se escribe cuando cambia.
// 2. **Cada segmento conserva su ancla `#s-<idx>`** aunque se agrupe. Es lo
//    que permite enlazar una frase concreta, y será el punto por el que el
//    chat (I5) justifique sus respuestas y el reproductor (I7) salte al audio.

import { escapar, marcaTiempo, plural } from "../formato.js";
import { idSegmento } from "../enlaces.js";

/** Segmentos consecutivos del mismo hablante, en un solo bloque. */
function agrupar(segmentos) {
  const grupos = [];
  for (const seg of segmentos) {
    const quien = seg.persona || seg.etiqueta || null;
    const ultimo = grupos[grupos.length - 1];
    if (ultimo && ultimo.quien === quien) ultimo.lineas.push(seg);
    else grupos.push({ quien, identificado: Boolean(seg.persona), lineas: [seg] });
  }
  return grupos;
}

function linea(seg, conTiempos) {
  const ancla = idSegmento(seg.idx);
  const tiempo = conTiempos
    ? `<a class="tiempo" href="#${ancla}" title="Enlace a este momento">${escapar(
        marcaTiempo(seg.inicio)
      )}</a>`
    : "";
  return `<li id="${ancla}" class="linea">${tiempo}<span class="dicho">${escapar(
    seg.texto
  )}</span></li>`;
}

function bloque(grupo, conTiempos) {
  const nombre = grupo.quien
    ? `<b class="quien${grupo.identificado ? "" : " sin-nombre"}">${escapar(
        grupo.quien
      )}</b>`
    : "";
  return `
    <div class="turno">
      ${nombre}
      <ul class="lineas">${grupo.lineas.map((s) => linea(s, conTiempos)).join("")}</ul>
    </div>`;
}

/**
 * Pinta los segmentos ya cargados.
 *
 * `estado` es `{segmentos, total, con_tiempos}`. Se repinta entero en cada
 * página en vez de ir añadiendo nodos: son como mucho un par de miles de
 * líneas y el código que resulta es la mitad de largo.
 */
export function dibujarTranscripcion(contenedor, estado) {
  if (!estado.total) {
    contenedor.innerHTML =
      '<div class="aviso">Esta reunión no tiene transcripción en la base. ' +
      "Procésala con summarize_teams.py para incorporarla.</div>";
    return;
  }

  const partes = [];
  if (!estado.con_tiempos) {
    // D9: los segmentos entran por summarize_teams.py leyendo el `.srt`
    // hermano; si no existía, se cayó al `.txt` y no hay marcas de tiempo.
    partes.push(
      '<p class="nota">Sin marcas de tiempo: esta reunión se procesó sin el ' +
        "<code>.srt</code>, así que no se puede enlazar un momento concreto.</p>"
    );
  }
  partes.push(
    `<div class="transcripcion">${agrupar(estado.segmentos)
      .map((g) => bloque(g, estado.con_tiempos))
      .join("")}</div>`
  );

  const faltan = estado.total - estado.segmentos.length;
  if (faltan > 0) {
    partes.push(
      `<p class="mas"><button type="button" id="mas">Cargar ${plural(
        faltan,
        "segmento más",
        "segmentos más"
      )}</button></p>`
    );
  }
  contenedor.innerHTML = partes.join("");
}

/** Lleva la vista al segmento del ancla, si está cargado. Devuelve si lo logró. */
export function irAlSegmento(idx) {
  const nodo = document.getElementById(idSegmento(idx));
  if (!nodo) return false;
  nodo.scrollIntoView({ behavior: "smooth", block: "center" });
  nodo.classList.add("señalada");
  return true;
}
