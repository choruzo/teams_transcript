// El pie de página con el estado del despliegue. Lo pintan las dos páginas, así
// que vive aquí y no en el orquestador de ninguna: en un servidor sin
// navegador ni comodidad para depurar, esta línea es el primer sitio donde se
// mira cuando algo no va, y tiene que decir lo mismo en todas partes.
//
// Desde la autenticación lleva además el botón «Salir»: es el único punto que
// todas las páginas comparten, así que el cierre de sesión va aquí y no
// repetido en cinco cabeceras.

import { api } from "../api.js";
import { escapar, plural } from "../formato.js";

export async function pintarSalud(pie) {
  if (!pie) return;
  pie.innerHTML = `
    <span class="salud-texto"></span>
    <button type="button" class="salir" hidden>Salir</button>`;
  const texto = pie.querySelector(".salud-texto");
  const salir = pie.querySelector(".salir");
  salir.addEventListener("click", () => api.cerrarSesion());

  const mal = (html) => {
    texto.innerHTML = html;
  };

  try {
    const salud = await api.salud();
    // Solo hay login del que salir si la autenticación está activa.
    if (salud.autenticacion) salir.hidden = false;
    if (!salud.base_accesible) {
      mal(
        `<span class="mal">Base de datos inaccesible:</span> ${escapar(
          salud.detalle || salud.base_de_datos
        )}`
      );
      return;
    }
    const trozos = [
      `v${escapar(salud.version)}`,
      `esquema ${salud.esquema}`,
      plural(salud.reuniones, "reunión", "reuniones"),
      plural(salud.acciones_abiertas, "acción abierta", "acciones abiertas"),
    ];
    if (salud.solo_lectura) trozos.push("solo lectura");
    texto.textContent = trozos.join(" · ");
    if (salud.detalle) {
      texto.innerHTML += ` — <span class="mal">${escapar(salud.detalle)}</span>`;
    }
  } catch (error) {
    mal(`<span class="mal">${escapar(error.message)}</span>`);
  }
}
