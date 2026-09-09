// El atajo global de teclado que lleva a la búsqueda (I4).
//
// Lo importan las cinco páginas, porque «buscar dónde se dijo algo» es la
// operación que se hace desde cualquier sitio y no un rincón de una vista.
// Dos combinaciones, y las dos por el mismo motivo (que la gente ya las tiene
// en los dedos): `/` como en GitHub o Gmail, y `Ctrl`/`Cmd` + `K` como en
// media aplicación moderna.
//
// Reglas de convivencia con el resto de la página:
//
//   - `/` se ignora si el foco está en un campo de texto: ahí es una barra.
//     `Ctrl+K` no, porque un atajo con modificador se pulsa a propósito.
//   - En la propia página de búsqueda no se navega: se enfoca el campo y se
//     selecciona lo que hubiera, para que empezar a teclear reemplace la
//     consulta anterior.

import { urlBuscar } from "./enlaces.js";

const ESCRIBIENDO = ["input", "textarea", "select"];

function estaEscribiendo(destino) {
  if (!destino) return false;
  if (destino.isContentEditable) return true;
  return ESCRIBIENDO.includes(destino.tagName.toLowerCase());
}

/**
 * Activa el atajo. `campo` es el buscador de esta página, si lo tiene: cuando
 * existe se enfoca en vez de navegar.
 */
export function iniciarAtajoDeBusqueda(campo = null) {
  document.addEventListener("keydown", (evento) => {
    const conModificador = evento.ctrlKey || evento.metaKey;
    const esK = conModificador && evento.key.toLowerCase() === "k";
    const esBarra = !conModificador && !evento.altKey && evento.key === "/";
    if (!esK && !esBarra) return;
    if (esBarra && estaEscribiendo(evento.target)) return;

    evento.preventDefault();
    if (campo) {
      campo.focus();
      campo.select();
    } else {
      window.location.href = urlBuscar();
    }
  });
}
