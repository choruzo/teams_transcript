// Subir una grabación desde la web (I10b).
//
// El reparto es el de siempre: esta página decide qué se manda y pinta lo que
// llega; la API solo escribe en la cola y el trabajador del host es quien
// transcribe. Aquí no hay ninguna lógica del pipeline.
//
// La subida va por trozos porque un .wav de una hora son cientos de MB. Si se
// corta, se guarda en localStorage qué trabajo quedó a medias y, al volver a
// elegir el mismo fichero, se reanuda por los bytes que el servidor ya
// confirmó. El trozo se identifica por nombre + tamaño + lastModified, que es
// lo único que el navegador conserva de un fichero entre recargas.

import { api } from "./api.js";
import { iniciarTema } from "./tema.js";
import { escapar, fechaCorta, fechaLarga, iso, duracion, plural } from "./formato.js";
import { pintarSalud } from "./vistas/salud.js";
import { iniciarAtajoDeBusqueda } from "./buscador.js";
import { urlReunion } from "./enlaces.js";

const $ = (selector) => document.querySelector(selector);

const CLAVE_RESERVA = "subida-en-curso";

const NOMBRES_ESTADO = {
  subiendo: "Subiendo",
  encolado: "En cola",
  preparando: "Preparando",
  transcribiendo: "Transcribiendo",
  resumiendo: "Resumiendo",
  indexando: "Indexando",
  esperando_llm: "Esperando al LLM",
  hecho: "Hecho",
  error: "Error",
  cancelado: "Cancelado",
  interrumpido: "Interrumpido",
};

const TERMINALES = ["hecho", "error", "cancelado", "interrumpido"];
const SE_PUEDE_REINTENTAR = ["error", "interrumpido", "cancelado"];

let config = null;
let archivo = null;
let reserva = null;
let controlSSE = null;
let enviando = false;

const nombreEstado = (estado) => NOMBRES_ESTADO[estado] || estado;

function tamanoLegible(bytes) {
  if (!bytes) return "0 B";
  const unidades = ["B", "KB", "MB", "GB"];
  let valor = bytes;
  let i = 0;
  while (valor >= 1024 && i < unidades.length - 1) {
    valor /= 1024;
    i += 1;
  }
  return `${valor.toFixed(valor >= 10 || i === 0 ? 0 : 1)} ${unidades[i]}`;
}

function mensaje(html, tipo = "") {
  const caja = $("#mensaje-subida");
  caja.innerHTML = html ? `<div class="aviso ${tipo}">${html}</div>` : "";
}

function leerReserva() {
  try {
    const crudo = localStorage.getItem(CLAVE_RESERVA);
    return crudo ? JSON.parse(crudo) : null;
  } catch (_) {
    return null;
  }
}

function guardarReserva(datos) {
  try {
    localStorage.setItem(CLAVE_RESERVA, JSON.stringify(datos));
  } catch (_) {
    /* sin localStorage la reanudación entre recargas se pierde, nada más */
  }
}

function borrarReserva() {
  try {
    localStorage.removeItem(CLAVE_RESERVA);
  } catch (_) {
    /* da igual */
  }
}

const esperar = (ms) => new Promise((listo) => setTimeout(listo, ms));

async function hashDe(blob) {
  // `crypto.subtle` solo existe en contexto seguro (HTTPS o localhost). Si no
  // está, se manda el trozo sin hash: el servidor comprueba longitud y el
  // sha256 del fichero completo al finalizar, así que no se pierde la garantía
  // de que llegó entero, solo la de detectarlo antes.
  if (!globalThis.crypto?.subtle) return null;
  try {
    const digest = await crypto.subtle.digest("SHA-256", await blob.arrayBuffer());
    return [...new Uint8Array(digest)]
      .map((byte) => byte.toString(16).padStart(2, "0"))
      .join("");
  } catch (_) {
    return null;
  }
}

// --------------------------------------------------------------------------
// Estado del despliegue y cola
// --------------------------------------------------------------------------

async function cargarEstado() {
  try {
    config = await api.estadoTrabajos();
  } catch (error) {
    $("#estado-subidas").className = "aviso error";
    $("#estado-subidas").textContent = error.message;
    return;
  }
  if (!config.disponible) {
    $("#estado-subidas").className = "aviso error";
    $("#estado-subidas").textContent =
      config.motivo || "Las subidas están apagadas.";
    return;
  }

  $("#bloque-subida").hidden = false;
  $("#maximo").textContent = tamanoLegible(config.maximo_bytes);
  const libre = config.libre_bytes
    ? ` · ${tamanoLegible(config.libre_bytes)} libres`
    : "";
  if (config.motivo) {
    $("#estado-subidas").className = "aviso error";
    $("#estado-subidas").textContent = config.motivo;
  } else if (config.trabajador) {
    $("#estado-subidas").className = "aviso";
    $("#estado-subidas").textContent =
      `Subidas activas · ${plural(config.en_cola, "trabajo en cola", "trabajos en cola")}` +
      ` · ${config.en_curso ? "transcribiendo ahora" : "nada en ejecución"}${libre}`;
  } else {
    $("#estado-subidas").className = "aviso error";
    $("#estado-subidas").textContent =
      "Las subidas están activas, pero el trabajador del servidor no da señal. " +
      "El fichero se quedará en cola hasta que vuelva." +
      (config.latido_en ? ` Último latido: ${config.latido_en}.` : "");
  }
}

async function cargarTrabajos() {
  let pagina;
  try {
    pagina = await api.trabajos({ limite: 25 });
  } catch (error) {
    $("#trabajos").innerHTML =
      `<li class="aviso error">${escapar(error.message)}</li>`;
    return;
  }
  const trabajos = pagina.trabajos;
  $("#total-trabajos").textContent = trabajos.length
    ? plural(trabajos.length, "trabajo", "trabajos")
    : "";
  if (!trabajos.length) {
    $("#trabajos").innerHTML =
      '<li class="aviso">Todavía no se ha subido ninguna grabación.</li>';
    return;
  }
  $("#trabajos").innerHTML = trabajos.map(tarjetaDeTrabajo).join("");
}

function tarjetaDeTrabajo(trabajo) {
  const estado = trabajo.estado;
  const fecha = trabajo.fecha_reunion
    ? `${fechaCorta(trabajo.fecha_reunion)}${trabajo.hora_reunion ? ` · ${trabajo.hora_reunion}` : ""}`
    : "";
  const modelo = trabajo.opciones?.modelo ? ` · ${trabajo.opciones.modelo}` : "";
  const reunion =
    estado === "hecho" && trabajo.reunion_uid
      ? `<a class="abrir-reunion" href="${urlReunion(trabajo.reunion_uid)}">Abrir la reunión</a>`
      : "";
  const cancelar = !TERMINALES.includes(estado)
    ? `<button type="button" class="secundario" data-cancelar="${escapar(trabajo.uid)}">Cancelar</button>`
    : "";
  const reintentar = SE_PUEDE_REINTENTAR.includes(estado)
    ? `<button type="button" class="secundario" data-reintentar="${escapar(trabajo.uid)}">Reintentar</button>`
    : "";
  const error =
    estado === "error" && trabajo.mensaje
      ? `<pre class="error-trabajo">${escapar(trabajo.mensaje)}</pre>`
      : "";
  return `
    <li class="trabajo estado-${escapar(estado)}">
      <div class="trabajo-cabecera">
        <span class="insignia">${escapar(nombreEstado(estado))}</span>
        <span class="trabajo-fecha">${escapar(fecha)}</span>
        <span class="tenue">${escapar(tamanoLegible(trabajo.tamano))}${escapar(modelo)}</span>
      </div>
      <div class="trabajo-nombre">${escapar(trabajo.nombre_original)}</div>
      ${error}
      <div class="acciones-trabajo">
        <button type="button" class="secundario" data-ver="${escapar(trabajo.uid)}">Ver progreso</button>
        ${reunion}
        ${cancelar}
        ${reintentar}
      </div>
    </li>`;
}

// --------------------------------------------------------------------------
// Elegir el fichero
// --------------------------------------------------------------------------

function fechaYHoraDe(marca) {
  const fecha = new Date(marca || Date.now());
  const dos = (n) => String(n).padStart(2, "0");
  return {
    fecha: iso(fecha),
    hora: `${dos(fecha.getHours())}:${dos(fecha.getMinutes())}`,
  };
}

async function elegirArchivo(nuevo) {
  if (!nuevo) return;
  if (!/\.wav$/i.test(nuevo.name)) {
    mensaje("Solo se aceptan ficheros <code>.wav</code>.", "error");
    return;
  }
  if (/_(mic|system)\.wav$/i.test(nuevo.name)) {
    mensaje(
      "Ese fichero es <code>_mic.wav</code> o <code>_system.wav</code>: " +
        "solo tiene una de las dos fuentes. Sube el <code>_mixed.wav</code> " +
        "que mezcla micrófono y audio del sistema.",
      "error"
    );
    return;
  }
  if (config && nuevo.size > config.maximo_bytes) {
    mensaje(
      `El fichero ocupa ${tamanoLegible(nuevo.size)} y el máximo es ` +
        `${tamanoLegible(config.maximo_bytes)}.`,
      "error"
    );
    return;
  }

  archivo = nuevo;
  mensaje("");
  $("#ficha-archivo").hidden = false;
  $("#nombre-archivo").textContent = nuevo.name;
  const cuandos = fechaYHoraDe(nuevo.lastModified);
  if (!$("#fecha").value) $("#fecha").value = cuandos.fecha;
  if (!$("#hora").value) $("#hora").value = cuandos.hora;
  $("#detalle-archivo").textContent = `${tamanoLegible(nuevo.size)} · modificado el ${fechaLarga(cuandos.fecha)}`;

  const url = URL.createObjectURL(nuevo);
  const audio = new Audio();
  audio.preload = "metadata";
  audio.onloadedmetadata = () => {
    $("#detalle-archivo").textContent =
      `${tamanoLegible(nuevo.size)} · ${duracion(audio.duration)}` +
      ` · modificado el ${fechaLarga(cuandos.fecha)}`;
    URL.revokeObjectURL(url);
  };
  audio.onerror = () => URL.revokeObjectURL(url);
  audio.src = url;

  await comprobarReserva(nuevo);
}

async function comprobarReserva(nuevo) {
  const guardada = leerReserva();
  reserva = null;
  $("#descartar").hidden = true;
  $("#empezar").textContent = "Subir y procesar";
  if (
    !guardada ||
    guardada.nombre !== nuevo.name ||
    guardada.tamano !== nuevo.size ||
    guardada.lastModified !== nuevo.lastModified
  ) {
    return;
  }
  try {
    const estado = await api.estadoSubida(guardada.uid);
    if (estado.estado !== "subiendo") {
      // El servidor ya la terminó (o la canceló): la reserva está obsoleta.
      borrarReserva();
      return;
    }
    reserva = guardada;
    $("#empezar").textContent = "Reanudar subida";
    $("#descartar").hidden = false;
    mensaje(
      `Hay una subida a medias de este mismo fichero: se han recibido ` +
        `${tamanoLegible(estado.recibidos)} de ${tamanoLegible(estado.tamano)}. ` +
        `Se reanuda por donde iba, o se empieza de cero descartándola.`
    );
    pintarSubida(estado.recibidos, estado.tamano);
  } catch (_) {
    borrarReserva();
  }
}

// --------------------------------------------------------------------------
// Subida por trozos
// --------------------------------------------------------------------------

function pintarSubida(recibidos, total) {
  const porcentaje = total ? Math.round((recibidos / total) * 100) : 0;
  mensaje(
    `<strong>Subiendo… ${porcentaje} %</strong> ` +
      `(${tamanoLegible(recibidos)} de ${tamanoLegible(total)}). ` +
      `Si se corta, vuelve a elegir el mismo fichero para reanudar.`
  );
}

async function enviar(evento) {
  evento.preventDefault();
  if (enviando) return;
  if (!archivo) {
    mensaje("Elige primero un fichero <code>.wav</code>.", "error");
    return;
  }
  if (!$("#fecha").value) {
    mensaje("Falta la fecha de la reunión.", "error");
    return;
  }

  enviando = true;
  $("#empezar").disabled = true;
  try {
    let uid;
    if (reserva) {
      uid = reserva.uid;
    } else {
      const creado = await api.crearTrabajo({
        nombre: archivo.name,
        tamano: archivo.size,
        fecha: $("#fecha").value,
        hora: $("#hora").value || null,
        tipo: $("#tipo").value,
        diarizar: $("#diarizar").checked,
        modelo: $("#modelo").value,
        titulo: $("#titulo").value.trim() || null,
      });
      uid = creado.uid;
      reserva = {
        uid,
        nombre: archivo.name,
        tamano: archivo.size,
        lastModified: archivo.lastModified,
      };
      guardarReserva(reserva);
    }
    await subirTodo(uid);
  } catch (error) {
    if (error.estado === 401) return; // api.js ya ha llevado al login
    if (error.estado === 409 && error.datos?.uid) {
      ofrecerForzar(error.datos, reserva?.uid);
      return;
    }
    mensaje(
      `<strong>No se pudo terminar la subida.</strong> ${escapar(error.message)} ` +
        `Vuelve a elegir el mismo fichero para reanudar sin perder lo enviado.`,
      "error"
    );
  } finally {
    enviando = false;
    $("#empezar").disabled = false;
  }
}

async function subirTodo(uid) {
  const estado = await api.estadoSubida(uid);
  if (estado.estado !== "subiendo") {
    // Ya estaba subida y finalizada (doble clic, recarga...).
    trasFinalizar(uid);
    return;
  }
  let recibidos = estado.recibidos;
  const total = estado.tamano;
  const trozo = estado.trozo;
  while (recibidos < total) {
    const indice = Math.floor(recibidos / trozo);
    const fin = Math.min(recibidos + trozo, total);
    const parte = archivo.slice(recibidos, fin);
    const huella = await hashDe(parte);
    let ultimo = null;
    for (let intento = 1; intento <= 4; intento += 1) {
      try {
        const confirmado = await api.subirParte(uid, indice, parte, huella);
        recibidos = confirmado.recibidos;
        ultimo = null;
        break;
      } catch (error) {
        if (error.estado === 401) throw error;
        ultimo = error;
        // 409 por orden o 422 por contenido no se arreglan reintentando: el
        // estado del servidor manda. Solo se reintenta lo que puede ser red.
        if (error.estado === 409 || error.estado === 422) break;
        await esperar(1000 * intento);
      }
    }
    if (ultimo) throw ultimo;
    pintarSubida(recibidos, total);
  }
  mensaje("<strong>Subida completa.</strong> Validando el WAV y encolando…");
  const final = await api.finalizarTrabajo(uid, false);
  trasFinalizar(final.uid);
}

async function ofrecerForzar(datos, uid) {
  if (!uid) return;
  const enlace = datos.reunion_uid
    ? ` <a href="${urlReunion(datos.reunion_uid)}">Abrir la reunión que ya existe</a>.`
    : "";
  mensaje(
    `<strong>Esa grabación ya se subió.</strong> ${escapar(datos.mensaje || "")}` +
      enlace +
      ` <button type="button" id="forzar">Procesarla igualmente</button>`,
    "error"
  );
  const boton = $("#forzar");
  if (!boton) return;
  boton.addEventListener("click", async () => {
    boton.disabled = true;
    try {
      const final = await api.finalizarTrabajo(uid, true);
      trasFinalizar(final.uid);
    } catch (error) {
      mensaje(escapar(error.message), "error");
      boton.disabled = false;
    }
  });
}

function trasFinalizar(uid) {
  borrarReserva();
  reserva = null;
  $("#descartar").hidden = true;
  $("#empezar").textContent = "Subir y procesar";
  mensaje("<strong>En cola.</strong> El trabajador la procesará en cuanto la GPU esté libre.");
  abrirProgreso(uid);
  cargarEstado();
  cargarTrabajos();
}

async function descartar() {
  if (reserva) {
    try {
      await api.cancelarTrabajo(reserva.uid);
    } catch (_) {
      /* si ya no se puede cancelar, da igual: se descarta la reserva local */
    }
  }
  borrarReserva();
  reserva = null;
  archivo = null;
  $("#archivo").value = "";
  $("#ficha-archivo").hidden = true;
  $("#descartar").hidden = true;
  $("#empezar").textContent = "Subir y procesar";
  mensaje("Subida descartada. Elige el fichero de nuevo.");
  cargarTrabajos();
}

// --------------------------------------------------------------------------
// Progreso en vivo
// --------------------------------------------------------------------------

function cerrarProgreso() {
  if (controlSSE) {
    controlSSE.abort();
    controlSSE = null;
  }
}

function abrirProgreso(uid) {
  cerrarProgreso();
  $("#bloque-progreso").hidden = false;
  $("#progreso-uid").textContent = uid;
  $("#progreso-log").textContent = "";
  $("#progreso-acciones").innerHTML = "";
  controlSSE = api.eventosTrabajo(uid, {
    estado: pintarTrabajo,
    log: ({ lineas }) => {
      const caja = $("#progreso-log");
      caja.textContent = lineas || "";
      caja.scrollTop = caja.scrollHeight;
    },
    fin: (trabajo) => {
      pintarTrabajo(trabajo);
      cargarTrabajos();
      cargarEstado();
    },
    error: (detalle) => {
      $("#progreso-log").textContent += `\n[error] ${detalle}`;
    },
  });
}

function pintarTrabajo(trabajo) {
  const estado = trabajo.estado;
  const insignia = $("#progreso-estado");
  insignia.textContent = nombreEstado(estado);
  insignia.className = `insignia estado-${estado}`;
  const paso = trabajo.paso && trabajo.paso !== estado
    ? ` · ${nombreEstado(trabajo.paso)}`
    : "";
  $("#progreso-paso").textContent = paso;
  const partes = [];
  if (trabajo.duracion_seg) partes.push(`audio de ${duracion(trabajo.duracion_seg)}`);
  if (trabajo.inicio_en) partes.push(`empezó ${trabajo.inicio_en.slice(11)}`);
  if (trabajo.fin_en) partes.push(`terminó ${trabajo.fin_en.slice(11)}`);
  $("#progreso-tiempo").textContent = partes.join(" · ");

  const barra = $("#progreso-barra");
  if (estado === "hecho") barra.value = 100;
  else if (typeof trabajo.progreso === "number") barra.value = trabajo.progreso * 100;
  else barra.value = 0;

  const acciones = [];
  if (!TERMINALES.includes(estado)) {
    acciones.push(
      `<button type="button" class="secundario" data-cancelar="${escapar(trabajo.uid)}">Cancelar</button>`
    );
  }
  if (SE_PUEDE_REINTENTAR.includes(estado)) {
    acciones.push(
      `<button type="button" class="secundario" data-reintentar="${escapar(trabajo.uid)}">Reintentar</button>`
    );
  }
  if (estado === "hecho" && trabajo.reunion_uid) {
    acciones.push(
      `<a class="abrir-reunion" href="${urlReunion(trabajo.reunion_uid)}">Abrir la reunión</a>`
    );
  }
  $("#progreso-acciones").innerHTML = acciones.join(" ");
}

async function cancelar(uid) {
  try {
    await api.cancelarTrabajo(uid);
    mensaje("Cancelación pedida. El trabajador la para en unos segundos.");
    cargarTrabajos();
  } catch (error) {
    mensaje(escapar(error.message), "error");
  }
}

async function reintentar(uid) {
  try {
    await api.reintentarTrabajo(uid);
    abrirProgreso(uid);
    cargarTrabajos();
  } catch (error) {
    mensaje(escapar(error.message), "error");
  }
}

// --------------------------------------------------------------------------
// Arranque
// --------------------------------------------------------------------------

$("#archivo").addEventListener("change", (evento) => {
  elegirArchivo(evento.target.files[0]);
});

const zona = $("#zona");
for (const tipo of ["dragenter", "dragover"]) {
  zona.addEventListener(tipo, (evento) => {
    evento.preventDefault();
    zona.classList.add("encima");
  });
}
for (const tipo of ["dragleave", "drop"]) {
  zona.addEventListener(tipo, (evento) => {
    evento.preventDefault();
    zona.classList.remove("encima");
  });
}
zona.addEventListener("drop", (evento) => {
  const soltado = evento.dataTransfer?.files?.[0];
  if (soltado) elegirArchivo(soltado);
});

$("#formulario").addEventListener("submit", enviar);
$("#descartar").addEventListener("click", descartar);

$("#trabajos").addEventListener("click", (evento) => {
  const ver = evento.target.closest("[data-ver]");
  const cancelarBoton = evento.target.closest("[data-cancelar]");
  const reintentarBoton = evento.target.closest("[data-reintentar]");
  if (ver) abrirProgreso(ver.dataset.ver);
  if (cancelarBoton) cancelar(cancelarBoton.dataset.cancelar);
  if (reintentarBoton) reintentar(reintentarBoton.dataset.reintentar);
});

$("#progreso-acciones").addEventListener("click", (evento) => {
  const cancelarBoton = evento.target.closest("[data-cancelar]");
  const reintentarBoton = evento.target.closest("[data-reintentar]");
  if (cancelarBoton) cancelar(cancelarBoton.dataset.cancelar);
  if (reintentarBoton) reintentar(reintentarBoton.dataset.reintentar);
});

iniciarTema();
iniciarAtajoDeBusqueda();
pintarSalud($("#salud"));
cargarEstado().then(cargarTrabajos);
