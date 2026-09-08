// Unico punto del front que habla con el servidor. Si algun dia cambia el
// prefijo o hay que meter autenticacion, se toca aqui y en ningun otro sitio.

const BASE = "/api";

async function pedir(ruta, parametros = {}) {
  const url = new URL(BASE + ruta, window.location.origin);
  for (const [clave, valor] of Object.entries(parametros)) {
    if (valor !== null && valor !== undefined && valor !== "") {
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

  if (!respuesta.ok) {
    // La API devuelve {detail: "..."} en los errores; es el mensaje que el
    // usuario necesita ver ("falta el volumen", "tipo desconocido").
    let detalle = `Error ${respuesta.status}`;
    try {
      const cuerpo = await respuesta.json();
      if (cuerpo && cuerpo.detail) {
        detalle = typeof cuerpo.detail === "string"
          ? cuerpo.detail
          : JSON.stringify(cuerpo.detail);
      }
    } catch (_) {
      /* respuesta sin JSON: nos quedamos con el codigo */
    }
    throw new Error(detalle);
  }
  return respuesta.json();
}

export const api = {
  salud: () => pedir("/salud"),
  reuniones: (filtros) => pedir("/reuniones", filtros),
  reunion: (uid) => pedir(`/reuniones/${encodeURIComponent(uid)}`),
};
