// El pie de página con el estado del despliegue. Lo pintan las dos páginas, así
// que vive aquí y no en el orquestador de ninguna: en un servidor sin
// navegador ni comodidad para depurar, esta línea es el primer sitio donde se
// mira cuando algo no va, y tiene que decir lo mismo en todas partes.

import { api } from "../api.js";
import { escapar, plural } from "../formato.js";

export async function pintarSalud(pie) {
  if (!pie) return;
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
      plural(salud.reuniones, "reunión", "reuniones"),
      plural(salud.acciones_abiertas, "acción abierta", "acciones abiertas"),
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
