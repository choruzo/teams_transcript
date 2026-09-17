// Unico punto del front que habla con el servidor. Si algun dia cambia el
// prefijo o hay que meter autenticacion, se toca aqui y en ningun otro sitio.

const BASE = "/api";

async function pedir(ruta, parametros = {}) {
  const url = new URL(BASE + ruta, window.location.origin);
  for (const [clave, valor] of Object.entries(parametros)) {
    if (valor === null || valor === undefined || valor === "") continue;
    // Un array se repite (`?estado=abierta&estado=bloqueada`), que es como
    // FastAPI lee una lista. Un `set` con el array lo mandaría como una sola
    // cadena separada por comas y el servidor no reconocería ningún estado.
    if (Array.isArray(valor)) {
      for (const uno of valor) url.searchParams.append(clave, uno);
    } else {
      url.searchParams.set(clave, valor);
    }
  }

  let respuesta;
  try {
    respuesta = await fetch(url);
  } catch (error) {
    // fetch solo rechaza si no hubo respuesta: servidor caido, DNS, red.
    throw new Error("No se puede contactar con el servidor.");
  }

  if (!respuesta.ok) throw await errorDeRespuesta(respuesta);
  return respuesta.json();
}

/**
 * El error de una respuesta no satisfactoria, con el mensaje del servidor.
 *
 * La API devuelve {detail: "..."}; es lo que hay que enseñar ("falta el
 * volumen", "sería un ciclo"). Un 409 trae un objeto con `mensaje` y la
 * versión actual. El código viaja en `estado`, porque el panel reacciona
 * distinto a un 409 (recargar) que a un 422 (enseñar el motivo).
 */
async function errorDeRespuesta(respuesta) {
  let detalle = `Error ${respuesta.status}`;
  let datos = null;
  try {
    const cuerpo = await respuesta.json();
    if (cuerpo && cuerpo.detail) {
      datos = cuerpo.detail;
      detalle = typeof cuerpo.detail === "string"
        ? cuerpo.detail
        : cuerpo.detail.mensaje || JSON.stringify(cuerpo.detail);
    }
  } catch (_) {
    /* respuesta sin JSON: nos quedamos con el codigo */
  }
  const error = new Error(detalle);
  error.estado = respuesta.status;
  error.datos = datos;
  return error;
}

/** El `If-Match` de una ficha: el mismo formato que el `ETag` de la API. */
export const etiquetaDeFicha = (ficha) => `"${ficha.uid}:${ficha.version}"`;

/**
 * Una escritura de I6a. `ficha` es la que está viendo el panel: su versión
 * va en `If-Match` y, si alguien la ha cambiado entretanto, la API responde
 * 409 en vez de pisarla.
 */
async function escribir(metodo, ruta, ficha, cuerpo) {
  const opciones = {
    method: metodo,
    headers: { "If-Match": etiquetaDeFicha(ficha) },
  };
  if (cuerpo !== undefined) {
    opciones.headers["Content-Type"] = "application/json";
    opciones.body = JSON.stringify(cuerpo);
  }
  let respuesta;
  try {
    respuesta = await fetch(BASE + ruta, opciones);
  } catch (_) {
    throw new Error("No se puede contactar con el servidor.");
  }
  if (!respuesta.ok) throw await errorDeRespuesta(respuesta);
  return respuesta.json();
}

const accion = (uid) => `/acciones/${encodeURIComponent(uid)}`;

export const api = {
  salud: () => pedir("/salud"),
  reuniones: (filtros) => pedir("/reuniones", filtros),
  // Devuelve la reunion **completa** (secciones, acciones, arrastres): el
  // mismo endpoint que en I0 daba solo la ficha, ahora con mas campos.
  reunion: (uid) => pedir(`/reuniones/${encodeURIComponent(uid)}`),
  // La transcripcion va aparte y paginada: una reunion de una hora son
  // ~350 segmentos y no pueden viajar con la ficha.
  segmentos: (uid, parametros) =>
    pedir(`/reuniones/${encodeURIComponent(uid)}/segmentos`, parametros),
  metricas: (filtros) => pedir("/metricas", filtros),
  // El timeline pide reuniones y carriles juntos: se dibujan sobre el mismo
  // eje y en dos peticiones habria un instante con la mitad del dibujo.
  timeline: (filtros) => pedir("/timeline", filtros),
  // El tablero (I3). Devuelve la página **y** los recuentos con los que
  // contrastarla, para que los controles de filtro sepan qué ofrecen.
  acciones: (filtros) => pedir("/acciones", filtros),
  // La búsqueda (I4). `q` es texto libre: la API lo traduce a sintaxis FTS5 y
  // devuelve en `consulta_fts` cómo lo entendió, que es lo que permite
  // explicar un resultado raro sin adivinar.
  buscar: (filtros) => pedir("/buscar", filtros),
  // Qué puede hacer el chat ahora mismo (I5). Va aparte de `/salud` porque
  // hace ping a LiteLLM, y el pie de las otras páginas no debe pagarlo.
  estadoDelChat: () => pedir("/chat/estado"),
  chat: preguntar,

  // I6a: la ficha de una acción y sus correcciones. Cada escritura recibe la
  // ficha que se estaba viendo (para `If-Match`) y devuelve la ficha nueva,
  // que puede ser de otra acción: fusionar devuelve la principal y separar,
  // la que se separa.
  ficha: (uid) => pedir(accion(uid)),
  personas: () => pedir("/personas"),
  corregir: (ficha, uid, cambios) => escribir("PATCH", accion(uid), ficha, cambios),
  descartar: (ficha, uid, motivo) =>
    escribir("POST", `${accion(uid)}/descartar`, ficha, { motivo }),
  restaurar: (ficha, uid) => escribir("POST", `${accion(uid)}/restaurar`, ficha),
  fusionar: (ficha, principal, duplicada) =>
    escribir("POST", `${accion(principal)}/fusionar`, ficha, { duplicada_uid: duplicada }),
  separar: (ficha, duplicada) => escribir("POST", `${accion(duplicada)}/separar`, ficha),
  anadirDependencia: (ficha, uid, otra) =>
    escribir("PUT", `${accion(uid)}/dependencias/${encodeURIComponent(otra)}`, ficha),
  quitarDependencia: (ficha, uid, otra) =>
    escribir("DELETE", `${accion(uid)}/dependencias/${encodeURIComponent(otra)}`, ficha),
  deshacer: (ficha, uid, correccion) =>
    escribir("POST", `${accion(uid)}/correcciones/${correccion}/deshacer`, ficha),
};

/**
 * El chat (I5). No pasa por `pedir()` porque la respuesta no es un JSON sino
 * un stream de eventos: hay que ir leyéndola según llega.
 *
 * Se usa `fetch` + `ReadableStream` y no `EventSource` porque `EventSource`
 * solo sabe hacer GET, y la pregunta con su historial no cabe en una URL.
 *
 * `manejadores` recibe `{plan, fuentes, texto, fin, error}`; devuelve un
 * `AbortController` para poder parar una respuesta a medias.
 */
function preguntar(cuerpo, manejadores = {}) {
  const control = new AbortController();
  (async () => {
    let respuesta;
    try {
      respuesta = await fetch(BASE + "/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(cuerpo),
        signal: control.signal,
      });
    } catch (error) {
      if (error.name !== "AbortError") {
        manejadores.error?.("No se puede contactar con el servidor.");
      }
      return;
    }
    if (!respuesta.ok) {
      // Aquí el error todavía es un JSON normal: el stream no ha empezado.
      let detalle = `Error ${respuesta.status}`;
      try {
        const json = await respuesta.json();
        if (json?.detail) {
          detalle = typeof json.detail === "string"
            ? json.detail
            : JSON.stringify(json.detail);
        }
      } catch (_) { /* sin JSON: nos quedamos con el código */ }
      manejadores.error?.(detalle);
      return;
    }
    try {
      await leerEventos(respuesta.body, manejadores);
    } catch (error) {
      if (error.name !== "AbortError") manejadores.error?.(error.message);
      return;
    }
    manejadores.cierre?.();
  })();
  return control;
}

/**
 * Parser mínimo de SSE: bloques separados por línea en blanco, con `event:` y
 * una o más líneas `data:`.
 *
 * El buffer es imprescindible: un `read()` puede cortar por la mitad de un
 * evento, y procesar medio JSON sería un error intermitente de los que solo
 * aparecen con respuestas largas.
 */
async function leerEventos(cuerpo, manejadores) {
  const lector = cuerpo.getReader();
  const decodificador = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { done, value } = await lector.read();
    if (done) break;
    buffer += decodificador.decode(value, { stream: true });
    let corte;
    while ((corte = buffer.indexOf("\n\n")) !== -1) {
      despachar(buffer.slice(0, corte), manejadores);
      buffer = buffer.slice(corte + 2);
    }
  }
  if (buffer.trim()) despachar(buffer, manejadores);
}

function despachar(bloque, manejadores) {
  let evento = "message";
  const datos = [];
  for (const linea of bloque.split("\n")) {
    if (linea.startsWith("event:")) evento = linea.slice(6).trim();
    else if (linea.startsWith("data:")) datos.push(linea.slice(5).trim());
  }
  if (!datos.length) return;
  let dato;
  try {
    dato = JSON.parse(datos.join("\n"));
  } catch (_) {
    return; // un keepalive o un bloque que no entendemos: se ignora
  }
  manejadores[evento]?.(dato);
}
