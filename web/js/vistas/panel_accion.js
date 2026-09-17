// Panel lateral de una acción (sección 3.6 bis del plan, fase I6a).
//
// Lo importan las tres páginas que enseñan acciones —el timeline, el tablero
// y la vista de reunión—, igual que la tarjeta de `vistas/acciones.js`: dos
// copias del formulario acabarían editando cosas distintas.
//
// Qué decide este módulo y qué no:
//
// - **Toda la lógica está en el motor** (`memoria.py`, Fase 7). Aquí no se
//   calcula ningún estado ni se valida ninguna regla: un ciclo, una fusión en
//   cadena o un estado inventado los rechaza la API con el mensaje del motor,
//   y el panel lo enseña tal cual.
// - **No es un modal.** Es un `<aside>` que no atrapa el foco: el timeline de
//   detrás sigue siendo usable, y la mitad de las correcciones se deciden
//   mirando la acción de al lado («esta y la de abajo son la misma»).
// - **Cada escritura lleva la versión que se vio** (`If-Match`). Si otra
//   pestaña o el pipeline la han cambiado, la API responde 409: el panel
//   recarga la ficha y lo dice, en vez de pisar el cambio en silencio.
// - **Nada se aplica al escribir.** Un único *Guardar* para la edición, y
//   confirmación explícita para descartar y fusionar. Si se cierra con
//   cambios sin guardar se pregunta, dentro del panel y no con `confirm()`,
//   que bloquearía la página entera.

import { api } from "../api.js";
import { escapar, fechaCorta, fechaLarga, nombreEstado, ESTADOS, plural } from "../formato.js";
import { urlReunion } from "../enlaces.js";
import { marcasDeAccion } from "./acciones.js";

const NOMBRES_DE_CAMPO = {
  descripcion: "Descripción",
  persona: "Responsable",
  estado: "Estado",
  descarte: "Descarte",
  fusion: "Fusión",
  dependencia: "Dependencia",
};

// Sugerencias, no una lista cerrada: el motivo es texto libre.
const MOTIVOS = [
  "No es una tarea",
  "Duplicada de algo fuera del sistema",
  "Ya no aplica",
  "Mal transcrita",
];

const CANDIDATAS = 8;

const chip = (texto, clase = "") =>
  `<span class="chip ${clase}">${escapar(texto)}</span>`;

const enlaceReunion = (uid, fecha, titulo) =>
  `<a href="${urlReunion(uid)}">${escapar(
    titulo ? `${titulo} (${fechaCorta(fecha)})` : fechaLarga(fecha)
  )}</a>`;

const vigente = (f) => !f.descartada_en && !f.absorbida_por;

function debounce(funcion, ms) {
  let temporizador;
  return (...argumentos) => {
    clearTimeout(temporizador);
    temporizador = setTimeout(() => funcion(...argumentos), ms);
  };
}

/** Una correccion del historial, en una línea legible. */
function lineaDeCorreccion(c) {
  const nombre = NOMBRES_DE_CAMPO[c.campo] || c.campo;
  let que;
  switch (c.campo) {
    case "estado":
      que = `${nombreEstado(c.valor_anterior)} → ${nombreEstado(c.valor_nuevo)}`;
      break;
    case "persona":
      que = `${c.valor_anterior || "sin responsable"} → ${c.valor_nuevo || "sin responsable"}`;
      break;
    case "descripcion":
      que = `«${c.valor_nuevo}»`;
      break;
    case "descarte":
      que = c.valor_nuevo ? `motivo: ${c.valor_nuevo}` : "sin motivo";
      break;
    case "fusion":
      que = `absorbida por ${c.valor_nuevo}`;
      break;
    case "dependencia":
      que = c.valor_nuevo ? `depende de ${c.valor_nuevo}` : `quitada la de ${c.valor_anterior}`;
      break;
    default:
      que = c.valor_nuevo || "";
  }
  return { nombre, que };
}

/**
 * Lo que quedaría tras fusionar, calculado con las menciones de las dos
 * fichas. Solo es una vista previa: el resultado real lo decide el motor, y
 * lo que se pinta después es su respuesta. Suma **reuniones**, no contadores:
 * una daily que nombró a las dos cuenta una vez.
 */
function previaDeFusion(principal, duplicada) {
  const reuniones = new Map();
  for (const m of [...principal.menciones_detalle, ...duplicada.menciones_detalle]) {
    reuniones.set(m.reunion_uid, m);
  }
  const fechas = [...reuniones.values()].map((m) => m.fecha).sort();
  return {
    reuniones: reuniones.size,
    primera: fechas[0] || principal.origen_fecha,
    ultima: fechas[fechas.length - 1] || principal.ultima_fecha,
  };
}

/** Reuniones distintas: una principal puede tener dos menciones de la misma. */
const reunionesDe = (f) => new Set(f.menciones_detalle.map((m) => m.reunion_uid)).size;

/** La fecha más antigua en que se mencionó: decide la principal por defecto. */
const primeraMencion = (f) =>
  f.menciones_detalle.map((m) => m.fecha).sort()[0] || f.origen_fecha;

/**
 * Crea el panel (uno por página) y devuelve cómo manejarlo.
 *
 * - `alCambiar(ficha)`: tras cada escritura correcta, para que la página
 *   repinte lo suyo (el timeline, el tablero) sin recargar.
 * - `alAlternar(abierto)`: al abrir o cerrar; el timeline se redibuja porque
 *   en pantallas anchas el panel le quita sitio en vez de taparlo.
 * - `alSeleccionar(uid|null)`: la acción abierta cambia, para marcarla.
 * - `elementoDe(uid)`: a dónde devolver el foco al cerrar, si el elemento que
 *   lo abrió ya no existe porque la página se ha repintado.
 */
export function crearPanel({ alCambiar, alAlternar, alSeleccionar, elementoDe } = {}) {
  const aside = document.createElement("aside");
  aside.className = "panel-accion";
  aside.setAttribute("aria-labelledby", "pa-titulo");
  aside.hidden = true;
  aside.innerHTML = `
    <div class="pa-cabecera">
      <h2 id="pa-titulo" tabindex="-1">Acción</h2>
      <button type="button" class="pa-cerrar" aria-label="Cerrar la ficha" title="Cerrar (Esc)">×</button>
    </div>
    <div class="pa-mensaje" aria-live="polite"></div>
    <div class="pa-cuerpo"></div>`;
  document.body.appendChild(aside);

  const $ = (selector) => aside.querySelector(selector);
  const estado = {
    uid: null,
    ficha: null,
    origen: null, // el elemento que abrió el panel, para devolverle el foco
    escritura: null, // null: aún no se sabe
    personas: null,
    fusion: null, // { candidata } mientras se elige la principal
  };

  // ------------------------------------------------------------------ datos

  async function saberSiSePuedeEscribir() {
    if (estado.escritura !== null) return;
    try {
      estado.escritura = Boolean((await api.salud()).escritura);
    } catch (_) {
      estado.escritura = false;
    }
  }

  async function cargarPersonas() {
    if (estado.personas) return;
    try {
      estado.personas = await api.personas();
    } catch (_) {
      estado.personas = [];
    }
  }

  // -------------------------------------------------------------- mensajes

  function mensaje(html, clase = "") {
    $(".pa-mensaje").innerHTML = html ? `<div class="pa-nota ${clase}">${html}</div>` : "";
  }

  function mensajeDeError(error) {
    if (error.estado === 409) {
      return mensaje(
        "<b>La acción ha cambiado mientras la mirabas</b> (otra pestaña, o el " +
          "pipeline al reprocesar). No se ha guardado nada: se ha recargado la " +
          "ficha, revísala y vuelve a aplicar tu cambio.",
        "aviso-ambar"
      );
    }
    if (error.estado === 403) estado.escritura = false;
    mensaje(`<b>No se ha guardado.</b> ${escapar(error.message)}`, "error");
  }

  // ----------------------------------------------------------------- pintar

  function cabecera(f) {
    const marcas = marcasDeAccion(f);
    if (f.corregida.length) {
      const campos = f.corregida.map((c) => NOMBRES_DE_CAMPO[c]).join(", ");
      marcas.push(
        `<span class="chip manual" title="${escapar(campos)}">corregida a mano</span>`
      );
    }
    if (f.revisar) marcas.push(chip("revisar", "s-media"));
    if (f.descartada_en) marcas.push(chip("descartada", "e-abandonada"));
    if (f.absorbida_por) marcas.push(chip("fusionada", "apagado"));

    const avisos = [];
    if (f.revisar) {
      avisos.push(
        `<p class="pa-nota aviso-ambar">Al reprocesar su reunión, el modelo ya no
          encontró esta acción. Se ha conservado porque tenía correcciones,
          fusiones o dependencias: decide si sigue valiendo.</p>`
      );
    }
    if (f.descartada_en) {
      avisos.push(
        `<p class="pa-nota">Descartada el ${escapar(fechaLarga(f.descartada_en.slice(0, 10)))}` +
          (f.motivo_descarte ? `: <i>${escapar(f.motivo_descarte)}</i>` : "") +
          ". No se ve, no cuenta y no se le ofrece al modelo.</p>"
      );
    }
    if (f.absorbida_por) {
      avisos.push(
        `<p class="pa-nota">Fusionada en
          <button type="button" class="enlace" data-abrir="${escapar(f.absorbida_por)}">
            ${escapar(f.absorbida_por_descripcion || f.absorbida_por)}</button>.
          Sus menciones cuentan en esa.</p>`
      );
    }

    return `
      <section class="pa-seccion pa-ficha">
        <p class="pa-descripcion">${escapar(f.descripcion)}</p>
        <div class="meta">
          <span class="persona">${escapar(f.persona || "sin responsable")}</span>
          ${marcas.join("")}
        </div>
        <p class="tenue">nace en ${enlaceReunion(f.origen_uid, f.origen_fecha, f.origen_titulo)}
          · última mención en ${enlaceReunion(f.ultima_uid, f.ultima_fecha, f.ultima_titulo)}</p>
        ${f.descripcion_llm && f.descripcion_llm !== f.descripcion
          ? `<p class="tenue">El modelo la llamó: «${escapar(f.descripcion_llm)}»</p>`
          : ""}
        ${avisos.join("")}
        <p><a class="boton" href="${urlReunion(f.ultima_uid)}">Ir a la última reunión</a></p>
      </section>`;
  }

  function seccionEdicion(f, bloqueado) {
    if (!vigente(f)) return "";
    const opciones = Object.entries(ESTADOS)
      .map(
        ([valor, nombre]) =>
          `<option value="${valor}" ${valor === f.estado ? "selected" : ""}>${escapar(nombre)}</option>`
      )
      .join("");
    return `
      <section class="pa-seccion">
        <h3>Corregir</h3>
        <form class="pa-form" id="pa-edicion">
          <label class="pa-campo">
            <span>Descripción</span>
            <textarea name="descripcion" rows="3" ${bloqueado}>${escapar(f.descripcion)}</textarea>
          </label>
          <label class="pa-campo">
            <span>Responsable</span>
            <input name="persona" list="pa-personas" autocomplete="off"
                   placeholder="sin responsable" value="${escapar(f.persona || "")}" ${bloqueado}>
            <small class="tenue">Elige de la lista o escribe un nombre nuevo; vacío la deja sin responsable.</small>
          </label>
          <label class="pa-campo">
            <span>Estado</span>
            <select name="estado" ${bloqueado}>${opciones}</select>
          </label>
          <div class="pa-botones">
            <button type="submit" class="primario" disabled>Guardar</button>
            <button type="reset" class="secundario" disabled>Deshacer cambios</button>
          </div>
        </form>
        <datalist id="pa-personas"></datalist>
      </section>`;
  }

  function seccionDescarte(f, bloqueado) {
    if (f.absorbida_por) return "";
    if (f.descartada_en) {
      return `
        <section class="pa-seccion">
          <h3>Descartada</h3>
          <button type="button" class="secundario" data-hacer="restaurar" ${bloqueado}>Restaurar</button>
        </section>`;
    }
    return `
      <section class="pa-seccion">
        <h3>Descartar</h3>
        <div id="pa-descarte">
          <button type="button" class="peligro" data-hacer="pedir-descarte" ${bloqueado}>Descartar…</button>
        </div>
      </section>`;
  }

  function seccionFusion(f, bloqueado) {
    const absorbidas = f.absorbidas.length
      ? `<ul class="pa-lista">${f.absorbidas
          .map(
            (a) => `
            <li>
              <button type="button" class="enlace" data-abrir="${escapar(a.uid)}">${escapar(a.descripcion)}</button>
              <span class="tenue">${escapar(a.uid)} · ${escapar(nombreEstado(a.estado))}</span>
              <button type="button" class="mini" data-hacer="separar" data-uid="${escapar(a.uid)}" ${bloqueado}>Separar</button>
            </li>`
          )
          .join("")}</ul>`
      : "";
    if (f.absorbida_por) {
      return `
        <section class="pa-seccion">
          <h3>Es la misma que…</h3>
          <p class="tenue">Fusionada en ${escapar(f.absorbida_por)}.</p>
          <button type="button" class="secundario" data-hacer="separar" data-uid="${escapar(f.uid)}" ${bloqueado}>Separar</button>
        </section>`;
    }
    if (!vigente(f)) return "";
    return `
      <section class="pa-seccion">
        <h3>Es la misma que…</h3>
        ${absorbidas ? `<p class="tenue">Ha absorbido:</p>${absorbidas}` : ""}
        <input type="search" class="pa-buscar" data-buscar="fusion"
               placeholder="Buscar la duplicada por texto" aria-label="Buscar acción duplicada" ${bloqueado}>
        <ul class="pa-candidatas" id="pa-candidatas-fusion"></ul>
        <div id="pa-previa"></div>
        <p class="tenue">Una acción fusionada suma las <b>reuniones</b> de las dos, no sus contadores.</p>
      </section>`;
  }

  function relacionadas(lista, sentido, f, bloqueado) {
    if (!lista.length) return `<p class="vacio">Ninguna.</p>`;
    return `<ul class="pa-lista">${lista
      .map(
        (d) => `
        <li>
          <button type="button" class="enlace" data-abrir="${escapar(d.uid)}">${escapar(d.descripcion)}</button>
          ${chip(nombreEstado(d.estado), `e-${d.estado}`)}
          <button type="button" class="mini" data-hacer="quitar-dependencia"
                  data-desde="${escapar(sentido === "depende" ? f.uid : d.uid)}"
                  data-hacia="${escapar(sentido === "depende" ? d.uid : f.uid)}" ${bloqueado}>Quitar</button>
        </li>`
      )
      .join("")}</ul>`;
  }

  function seccionDependencias(f, bloqueado) {
    const alta = vigente(f)
      ? `<input type="search" class="pa-buscar" data-buscar="dependencia"
               placeholder="Añadir: buscar de qué depende" aria-label="Buscar dependencia" ${bloqueado}>
         <ul class="pa-candidatas" id="pa-candidatas-dependencia"></ul>`
      : "";
    return `
      <section class="pa-seccion">
        <h3>Depende de…</h3>
        ${relacionadas(f.depende_de, "depende", f, bloqueado)}
        ${alta}
        <h4>Bloquea a</h4>
        ${relacionadas(f.bloquea_a, "bloquea", f, bloqueado)}
      </section>`;
  }

  function seccionMenciones(f) {
    const filas = f.menciones_detalle
      .map(
        (m) => `
        <li>
          ${enlaceReunion(m.reunion_uid, m.fecha, m.titulo)}
          ${chip(nombreEstado(m.estado), `e-${m.estado}`)}
          ${m.accion_uid !== f.uid ? `<span class="tenue">vía ${escapar(m.accion_uid)}</span>` : ""}
          ${m.comentario ? `<p class="comentario">${escapar(m.comentario)}</p>` : ""}
        </li>`
      )
      .join("");
    return `
      <section class="pa-seccion">
        <h3>Menciones <span class="tenue">${plural(reunionesDe(f), "reunión", "reuniones")}</span></h3>
        <ol class="pa-lista pa-menciones">${filas}</ol>
      </section>`;
  }

  function seccionHistorial(f, bloqueado) {
    if (!f.correcciones.length) {
      return `
        <section class="pa-seccion">
          <h3>Historial</h3>
          <p class="vacio">Nadie la ha corregido: todo lo de arriba lo dijo el modelo.</p>
        </section>`;
    }
    const filas = [...f.correcciones]
      .reverse()
      .map((c) => {
        const { nombre, que } = lineaDeCorreccion(c);
        return `
        <li class="${c.deshecha_en ? "deshecha" : ""}">
          <div><b>${escapar(nombre)}</b> ${escapar(que)}</div>
          <div class="tenue">${escapar(c.creado_en)} · ${escapar(c.origen)}${
            c.deshecha_en ? ` · deshecha el ${escapar(c.deshecha_en)}` : ""
          }</div>
          ${c.deshacible
            ? `<button type="button" class="mini" data-hacer="deshacer" data-id="${c.id}" ${bloqueado}>Deshacer</button>`
            : ""}
        </li>`;
      })
      .join("");
    return `
      <section class="pa-seccion">
        <h3>Historial</h3>
        <ol class="pa-lista pa-historial">${filas}</ol>
      </section>`;
  }

  function pintar() {
    const f = estado.ficha;
    const bloqueado = estado.escritura ? "" : "disabled";
    $("#pa-titulo").innerHTML = `Acción <code>${escapar(f.uid)}</code>`;
    const soloLectura = estado.escritura
      ? ""
      : `<p class="pa-nota">La API está en <b>solo lectura</b>: la ficha se puede
           consultar, pero para corregir hay que arrancar el servicio con
           <code>TEAMS_API_SOLO_LECTURA=0</code>.</p>`;
    $(".pa-cuerpo").innerHTML = `
      ${soloLectura}
      ${cabecera(f)}
      ${seccionEdicion(f, bloqueado)}
      ${seccionDescarte(f, bloqueado)}
      ${seccionFusion(f, bloqueado)}
      ${seccionDependencias(f, bloqueado)}
      ${seccionMenciones(f)}
      ${seccionHistorial(f, bloqueado)}
      <p class="pa-nota tenue">El <code>.md</code> que se generó al procesar la reunión
        no cambia con estas correcciones; la descarga desde la web sí, porque se
        regenera desde la base.</p>`;
    volcarPersonas();
  }

  function volcarPersonas() {
    const lista = $("#pa-personas");
    if (!lista || !estado.personas) return;
    lista.innerHTML = estado.personas
      .map((p) => `<option value="${escapar(p.nombre)}">${escapar(
        p.alias.length ? `${p.nombre} (${p.alias.join(", ")})` : p.nombre
      )}</option>`)
      .join("");
  }

  // ------------------------------------------------------- edición y sucio

  const formulario = () => $("#pa-edicion");

  function cambiosDelFormulario() {
    const form = formulario();
    const f = estado.ficha;
    if (!form || !f) return {};
    const cambios = {};
    const descripcion = form.descripcion.value.trim().replace(/\s+/g, " ");
    if (descripcion !== f.descripcion) cambios.descripcion = descripcion;
    const persona = form.persona.value.trim();
    if (persona !== (f.persona || "")) cambios.persona = persona || null;
    if (form.estado.value !== f.estado) cambios.estado = form.estado.value;
    return cambios;
  }

  const haySinGuardar = () => Object.keys(cambiosDelFormulario()).length > 0;

  function refrescarBotones() {
    const form = formulario();
    if (!form) return;
    const sucio = haySinGuardar();
    form.querySelector("[type=submit]").disabled = !sucio || !estado.escritura;
    form.querySelector("[type=reset]").disabled = !sucio;
  }

  /**
   * Pregunta dentro del panel antes de perder cambios. Devuelve una promesa
   * que se resuelve a `true` si se pueden descartar.
   */
  function confirmarPerdida() {
    if (!haySinGuardar()) return Promise.resolve(true);
    return new Promise((resolver) => {
      mensaje(
        `<b>Hay cambios sin guardar.</b>
         <span class="pa-botones">
           <button type="button" class="peligro" data-confirmar="si">Descartar cambios</button>
           <button type="button" class="secundario" data-confirmar="no">Seguir editando</button>
         </span>`,
        "aviso-ambar"
      );
      const zona = $(".pa-mensaje");
      zona.querySelector("[data-confirmar=no]").focus();
      zona.addEventListener(
        "click",
        function responder(evento) {
          const boton = evento.target.closest("[data-confirmar]");
          if (!boton) {
            zona.addEventListener("click", responder, { once: true });
            return;
          }
          mensaje("");
          resolver(boton.dataset.confirmar === "si");
        },
        { once: true }
      );
    });
  }

  // ------------------------------------------------------------ escrituras

  /**
   * Ejecuta una escritura y deja el panel en la acción `destino` (la propia,
   * salvo al fusionar desde la duplicada, que pasa a la principal).
   */
  async function escribir(operacion, { exito = "Guardado.", destino = null } = {}) {
    const f = estado.ficha;
    aside.setAttribute("aria-busy", "true");
    try {
      const respuesta = await operacion(f);
      const uid = destino || f.uid;
      estado.ficha = respuesta.uid === uid ? respuesta : await api.ficha(uid);
      estado.uid = uid;
      estado.fusion = null;
      pintar();
      const avisos = (respuesta.avisos || [])
        .map((a) => `<br>${escapar(a)}`)
        .join("");
      mensaje(`${escapar(exito)}${avisos}`, avisos ? "aviso-ambar" : "bien");
      // Un responsable nuevo se crea al guardar: la lista hay que pedirla otra vez.
      estado.personas = null;
      cargarPersonas().then(volcarPersonas);
      alCambiar?.(estado.ficha);
    } catch (error) {
      if (error.estado === 409) {
        try {
          estado.ficha = await api.ficha(f.uid);
          pintar();
        } catch (_) {
          /* si ni siquiera se puede recargar, se queda el mensaje */
        }
        alCambiar?.(estado.ficha);
      }
      mensajeDeError(error);
    } finally {
      aside.removeAttribute("aria-busy");
    }
  }

  // ------------------------------------------------------------- búsquedas

  async function buscar(tipo, texto) {
    const lista = $(`#pa-candidatas-${tipo}`);
    if (!lista) return;
    if (texto.trim().length < 2) {
      lista.innerHTML = "";
      return;
    }
    let pagina;
    try {
      pagina = await api.acciones({ q: texto, limite: CANDIDATAS + 5, orden: "reciente" });
    } catch (error) {
      lista.innerHTML = `<li class="error">${escapar(error.message)}</li>`;
      return;
    }
    const f = estado.ficha;
    const excluidas = new Set([f.uid, ...f.depende_de.map((d) => d.uid)]);
    const candidatas = pagina.acciones
      .filter((a) => !excluidas.has(a.uid))
      .slice(0, CANDIDATAS);
    lista.innerHTML = candidatas.length
      ? candidatas
          .map(
            (a) => `
            <li>
              <button type="button" class="candidata" data-elegir="${tipo}" data-uid="${escapar(a.uid)}">
                <span>${escapar(a.descripcion)}</span>
                <span class="tenue">${escapar(a.persona || "sin responsable")} ·
                  ${escapar(fechaCorta(a.origen_fecha))} · ${escapar(nombreEstado(a.estado))}</span>
              </button>
            </li>`
          )
          .join("")
      : `<li class="vacio">Ninguna acción vigente coincide.</li>`;
  }

  async function prepararFusion(uid) {
    const previa = $("#pa-previa");
    previa.innerHTML = `<p class="tenue">Cargando…</p>`;
    try {
      const candidata = await api.ficha(uid);
      const actual = estado.ficha;
      // La más antigua queda preseleccionada como principal.
      const principal =
        primeraMencion(candidata) < primeraMencion(actual) ? candidata.uid : actual.uid;
      estado.fusion = { candidata, principal };
      pintarPreviaDeFusion();
    } catch (error) {
      previa.innerHTML = `<p class="error">${escapar(error.message)}</p>`;
    }
  }

  function pintarPreviaDeFusion() {
    const { candidata, principal } = estado.fusion;
    const actual = estado.ficha;
    const fichaPrincipal = principal === actual.uid ? actual : candidata;
    const duplicada = principal === actual.uid ? candidata : actual;
    const r = previaDeFusion(fichaPrincipal, duplicada);
    const opcion = (fx) => `
      <label class="pa-opcion">
        <input type="radio" name="pa-principal" value="${escapar(fx.uid)}" ${fx.uid === principal ? "checked" : ""}>
        <span>${escapar(fx.descripcion)}
          <span class="tenue">${escapar(fx.uid)} · desde ${escapar(fechaCorta(primeraMencion(fx)))} ·
          ${plural(reunionesDe(fx), "reunión", "reuniones")}</span></span>
      </label>`;
    $("#pa-previa").innerHTML = `
      <div class="pa-previa">
        <p><b>¿Cuál queda como principal?</b></p>
        ${opcion(actual)}
        ${opcion(candidata)}
        <p class="tenue">Resultado: «${escapar(fichaPrincipal.descripcion)}», de
          ${escapar(fechaLarga(r.primera))} a ${escapar(fechaLarga(r.ultima))},
          ${plural(r.reuniones, "reunión", "reuniones")}
          (${reunionesDe(fichaPrincipal)} + ${reunionesDe(duplicada)}
          sin repetir). Conserva el responsable y el estado de la principal.</p>
        <div class="pa-botones">
          <button type="button" class="primario" data-hacer="fusionar">Fusionar</button>
          <button type="button" class="secundario" data-hacer="cancelar-fusion">Cancelar</button>
        </div>
      </div>`;
  }

  // ----------------------------------------------------------- abrir/cerrar

  async function abrir(uid, origen = null) {
    if (!aside.hidden && estado.uid === uid && estado.ficha) {
      $("#pa-titulo").focus(); // ya está abierta: no se pierde lo escrito
      return;
    }
    if (estado.uid && !(await confirmarPerdida())) return;
    const primeraVez = aside.hidden;
    alSeleccionar?.(uid);
    estado.uid = uid;
    estado.origen = origen;
    estado.fusion = null;
    mensaje("");
    aside.hidden = false;
    document.body.classList.add("con-panel");
    $("#pa-titulo").innerHTML = `Acción <code>${escapar(uid)}</code>`;
    $(".pa-cuerpo").innerHTML = `<div class="aviso">Cargando…</div>`;
    if (primeraVez) alAlternar?.(true);
    try {
      const [ficha] = await Promise.all([
        api.ficha(uid),
        saberSiSePuedeEscribir(),
        cargarPersonas(),
      ]);
      if (estado.uid !== uid) return; // se abrió otra mientras tanto
      estado.ficha = ficha;
      pintar();
    } catch (error) {
      $(".pa-cuerpo").innerHTML = `
        <div class="aviso error"><strong>No se ha podido cargar la acción.</strong>
          <code>${escapar(error.message)}</code></div>`;
    }
    $("#pa-titulo").focus();
  }

  async function cerrar() {
    if (aside.hidden) return;
    if (!(await confirmarPerdida())) return;
    const uid = estado.uid;
    aside.hidden = true;
    document.body.classList.remove("con-panel");
    estado.uid = null;
    estado.ficha = null;
    alSeleccionar?.(null);
    alAlternar?.(false);
    // Devolver el foco a quien lo abrió; si la página se repintó, a su sucesor.
    const destino =
      estado.origen && estado.origen.isConnected ? estado.origen : elementoDe?.(uid);
    destino?.focus?.();
  }

  // ---------------------------------------------------------------- eventos

  $(".pa-cerrar").addEventListener("click", cerrar);

  document.addEventListener("keydown", (evento) => {
    if (evento.key !== "Escape" || aside.hidden) return;
    // Esc dentro de un buscador con texto lo vacía primero, como en cualquier
    // campo de búsqueda; el segundo Esc cierra.
    if (evento.target.matches?.(".pa-buscar") && evento.target.value) return;
    evento.preventDefault();
    cerrar();
  });

  aside.addEventListener("input", (evento) => {
    if (evento.target.closest("#pa-edicion")) refrescarBotones();
  });
  aside.addEventListener(
    "input",
    debounce((evento) => {
      const tipo = evento.target.dataset?.buscar;
      if (tipo) buscar(tipo, evento.target.value);
    }, 250)
  );
  aside.addEventListener("change", (evento) => {
    if (evento.target.name === "pa-principal" && estado.fusion) {
      estado.fusion.principal = evento.target.value;
      pintarPreviaDeFusion();
    }
  });
  aside.addEventListener("reset", () => {
    // El `reset` nativo vuelve a los valores del HTML, que son los de la ficha.
    setTimeout(refrescarBotones);
  });
  aside.addEventListener("submit", (evento) => {
    if (evento.target.id !== "pa-edicion") return;
    evento.preventDefault();
    const cambios = cambiosDelFormulario();
    if (!Object.keys(cambios).length) return;
    escribir((f) => api.corregir(f, f.uid, cambios));
  });

  aside.addEventListener("click", async (evento) => {
    const abrirOtra = evento.target.closest("[data-abrir]");
    if (abrirOtra) {
      abrir(abrirOtra.dataset.abrir, estado.origen);
      return;
    }
    const elegida = evento.target.closest("[data-elegir]");
    if (elegida) {
      const uid = elegida.dataset.uid;
      if (elegida.dataset.elegir === "fusion") {
        $("#pa-candidatas-fusion").innerHTML = "";
        prepararFusion(uid);
      } else {
        escribir((f) => api.anadirDependencia(f, f.uid, uid), {
          exito: "Dependencia añadida.",
        });
      }
      return;
    }
    const boton = evento.target.closest("[data-hacer]");
    if (!boton || boton.disabled) return;
    const f = estado.ficha;
    switch (boton.dataset.hacer) {
      case "pedir-descarte":
        $("#pa-descarte").innerHTML = `
          <label class="pa-campo">
            <span>Motivo (opcional, pero ayuda a quien lo vea después)</span>
            <input id="pa-motivo" list="pa-motivos" maxlength="500" autocomplete="off">
          </label>
          <datalist id="pa-motivos">${MOTIVOS.map((m) => `<option value="${escapar(m)}">`).join("")}</datalist>
          <p class="tenue">No se borra: deja de verse, de contar y de ofrecerse al
            modelo, y se puede restaurar. Sigue descartada aunque se reprocese su reunión.</p>
          <div class="pa-botones">
            <button type="button" class="peligro" data-hacer="descartar">Descartar</button>
            <button type="button" class="secundario" data-hacer="cancelar-descarte">Cancelar</button>
          </div>`;
        $("#pa-motivo").focus();
        break;
      case "cancelar-descarte":
        pintar();
        break;
      case "descartar":
        escribir((fx) => api.descartar(fx, fx.uid, $("#pa-motivo").value.trim() || null), {
          exito: "Descartada.",
        });
        break;
      case "restaurar":
        escribir((fx) => api.restaurar(fx, fx.uid), { exito: "Restaurada." });
        break;
      case "cancelar-fusion":
        estado.fusion = null;
        $("#pa-previa").innerHTML = "";
        break;
      case "fusionar": {
        const { candidata, principal } = estado.fusion;
        const duplicada = principal === f.uid ? candidata.uid : f.uid;
        escribir((fx) => api.fusionar(fx, principal, duplicada), {
          exito: `Fusionada: ${duplicada} queda dentro de ${principal}.`,
          destino: principal,
        });
        break;
      }
      case "separar":
        escribir((fx) => api.separar(fx, boton.dataset.uid), { exito: "Separadas." });
        break;
      case "quitar-dependencia":
        escribir(
          (fx) => api.quitarDependencia(fx, boton.dataset.desde, boton.dataset.hacia),
          { exito: "Dependencia quitada." }
        );
        break;
      case "deshacer":
        escribir((fx) => api.deshacer(fx, fx.uid, boton.dataset.id), {
          exito: "Corrección deshecha.",
        });
        break;
    }
  });

  return {
    abrir,
    cerrar,
    /** La acción abierta, para marcarla en la página. */
    abierta: () => (aside.hidden ? null : estado.uid),
  };
}

/**
 * Las tarjetas de `vistas/acciones.js` abren el panel al pulsar su
 * descripción. Delegado en el contenedor, así sobrevive a los repintados.
 */
export function abrirDesdeTarjetas(contenedor, panel) {
  contenedor.addEventListener("click", (evento) => {
    const boton = evento.target.closest("[data-abrir-ficha]");
    if (boton) panel.abrir(boton.dataset.abrirFicha, boton);
  });
}

/** Marca la tarjeta de la acción abierta (o ninguna, con `null`). */
export function marcarTarjeta(contenedor, uid) {
  for (const nodo of contenedor.querySelectorAll(".accion.seleccionada")) {
    nodo.classList.remove("seleccionada");
  }
  if (uid) {
    for (const nodo of contenedor.querySelectorAll(`.accion[data-accion="${CSS.escape(uid)}"]`)) {
      nodo.classList.add("seleccionada");
    }
  }
}

/** El botón de la tarjeta de `uid`, para devolverle el foco al cerrar. */
export const botonDeTarjeta = (contenedor, uid) =>
  contenedor.querySelector(`[data-abrir-ficha="${CSS.escape(uid)}"]`);
