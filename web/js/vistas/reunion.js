// Vista de reunión (sección 3.3 del plan, fase I2). Pinta todo lo que la API
// devuelve en `/api/reuniones/{uid}` menos la transcripción, que es larga,
// paginada y vive en `transcripcion.js`.
//
// El contenido sale de las tablas del histórico, no del `.md` ni del JSON del
// modelo: lo que se lee aquí es lo que la base da por bueno **hoy**. Eso tiene
// una consecuencia que hay que decir en pantalla y no esconder: una acción
// nacida en esta reunión puede haber cambiado de estado en una posterior, y
// entonces lo que se ve no coincide con el `.md` que se generó aquel día.

import { duracion, escapar, fechaLarga, plural } from "../formato.js";
import { tarjetaAccion } from "./acciones.js";

const chip = (texto, clase = "") =>
  `<span class="chip ${clase}">${escapar(texto)}</span>`;

function cabecera(reunion) {
  const chips = [];
  const dur = duracion(reunion.duracion_seg);
  if (dur) chips.push(chip(dur));
  // D9: sin `.srt` no hay duración. Se dice, en vez de dejar el hueco y que
  // parezca que la reunión duró cero.
  else chips.push(chip("duración desconocida", "apagado"));
  chips.push(chip(plural(reunion.n_segmentos, "segmento", "segmentos")));
  if (reunion.modelo_whisper) chips.push(chip(`whisper: ${reunion.modelo_whisper}`));
  if (reunion.modelo_llm) chips.push(chip(`llm: ${reunion.modelo_llm}`));
  if (reunion.tiene_audio) chips.push(chip("con audio"));

  // Las descargas se regeneran en el servidor desde la base; no son el
  // fichero de `grabaciones/`, que puede estar en otra máquina.
  const descargas = [];
  if (reunion.tiene_markdown) {
    descargas.push(
      `<a href="/api/reuniones/${encodeURIComponent(reunion.uid)}/markdown">Resumen .md</a>`
    );
  }
  if (reunion.n_segmentos) {
    descargas.push(
      `<a href="/api/reuniones/${encodeURIComponent(reunion.uid)}/srt">Subtítulos .srt</a>`
    );
  }

  return `
    <div class="ficha">
      <div class="ficha-titulo">
        <h2>${escapar(reunion.titulo || fechaLarga(reunion.fecha))}</h2>
        <span class="etiqueta" data-tipo="${escapar(reunion.tipo)}">${escapar(reunion.tipo)}</span>
      </div>
      <div class="uid">${escapar(fechaLarga(reunion.fecha))} · ${escapar(reunion.uid)}</div>
      <div class="chips">${chips.join("")}</div>
      ${descargas.length ? `<div class="descargas">${descargas.join("")}</div>` : ""}
    </div>`;
}

function panel(titulo, cuerpo, clase = "") {
  return `<section class="panel ${clase}"><h3>${escapar(titulo)}</h3>${cuerpo}</section>`;
}

const vacio = (texto) => `<p class="vacio">${escapar(texto)}</p>`;

function hablantes(lista) {
  if (!lista.length) return "";
  const filas = lista
    .map((h) => {
      const quien = h.persona
        ? escapar(h.persona)
        : '<span class="vacio">sin identificar</span>';
      const confianza =
        h.confianza === null || h.confianza === undefined
          ? ""
          : ` <span class="tenue">(${Math.round(h.confianza * 100)} %)</span>`;
      return `<li><code>${escapar(h.etiqueta)}</code> → ${quien}${confianza}</li>`;
    })
    .join("");
  return panel("Hablantes", `<ul class="hablantes">${filas}</ul>`);
}

function intervenciones(lista) {
  if (!lista.length) return panel("Por persona", vacio("Sin novedades registradas."));
  const filas = lista
    .map((i) => {
      const detalles = [];
      if (i.trabajo) detalles.push(`<p>${escapar(i.trabajo)}</p>`);
      if (i.bloqueos) {
        detalles.push(`<p class="bloqueo"><b>Bloqueos:</b> ${escapar(i.bloqueos)}</p>`);
      }
      if (i.proximos_pasos) {
        detalles.push(`<p><b>Próximos pasos:</b> ${escapar(i.proximos_pasos)}</p>`);
      }
      return `
        <li>
          <b class="persona">${escapar(i.persona)}</b>
          ${detalles.join("") || vacio("Sin novedades.")}
        </li>`;
    })
    .join("");
  return panel("Por persona", `<ul class="intervenciones">${filas}</ul>`);
}

function acciones(titulo, lista, uidActual, nota) {
  if (!lista.length) return panel(titulo, vacio("Ninguna."));
  const cuerpo =
    (nota ? `<p class="nota">${escapar(nota)}</p>` : "") +
    `<ul class="acciones">${lista
      .map((a) => tarjetaAccion(a, { uidActual }))
      .join("")}</ul>`;
  return panel(titulo, cuerpo);
}

function riesgos(lista) {
  if (!lista.length) return "";
  const filas = lista
    .map((r) => {
      const marcas = [r.area, r.severidad]
        .filter(Boolean)
        .map((m) => chip(m, r.severidad === m ? `s-${m}` : ""))
        .join("");
      return `<li><p>${escapar(r.descripcion)}</p><div class="meta">${marcas}</div></li>`;
    })
    .join("");
  // "Mencionados", nunca "abiertos": los riesgos no tienen estado ni
  // continuidad entre reuniones (deuda D3, decisión de la sección 0 del plan).
  return panel(
    "Riesgos mencionados",
    `<ul class="riesgos">${filas}</ul>`
  );
}

function secciones(lista) {
  return lista
    .map((s) =>
      panel(
        s.titulo,
        `<ul class="puntos">${s.puntos.map((p) => `<li>${escapar(p)}</li>`).join("")}</ul>`
      )
    )
    .join("");
}

export function dibujarReunion(contenedor, reunion) {
  const posteriores = reunion.acciones.filter((a) => a.ultima_uid !== reunion.uid);
  const nota = posteriores.length
    ? "El estado y las menciones son los de hoy; algunas de estas acciones se " +
      "tocaron en reuniones posteriores."
    : "";

  contenedor.innerHTML = `
    ${cabecera(reunion)}
    <section class="bloque">
      <h2>Resumen</h2>
      ${
        reunion.resumen
          ? `<p class="resumen-largo">${escapar(reunion.resumen)}</p>`
          : vacio("Esta reunión no tiene resumen en la base.")
      }
    </section>
    <div class="paneles">
      ${intervenciones(reunion.intervenciones)}
      ${acciones("Acciones nacidas aquí", reunion.acciones, reunion.uid, nota)}
      ${acciones(
        "Arrastres de reuniones anteriores",
        reunion.arrastres,
        reunion.uid,
        ""
      )}
      ${riesgos(reunion.riesgos)}
      ${secciones(reunion.secciones)}
      ${hablantes(reunion.hablantes)}
    </div>`;
  document.title = `${reunion.titulo || reunion.fecha} · Memoria del equipo`;
}
