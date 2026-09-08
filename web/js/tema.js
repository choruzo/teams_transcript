// Conmutador de tema. El tema efectivo vive en el atributo data-tema de <html>
// (ver css/tokens.css); aqui solo se decide cual poner y se recuerda.
//
// Tres estados, pero un solo boton: mientras el usuario no toque nada se sigue
// la preferencia del sistema y se cambia con ella (util en los escritorios que
// alternan claro de dia y oscuro de noche). En cuanto pulsa, su eleccion manda
// y se guarda. Para volver a "lo que diga el sistema" basta con borrar la clave
// en el navegador; no merece un tercer estado en la interfaz.

const CLAVE = "tema";

const preferenciaDelSistema = () =>
  window.matchMedia("(prefers-color-scheme: light)").matches ? "claro" : "oscuro";

const guardado = () => {
  try {
    return localStorage.getItem(CLAVE);
  } catch (_) {
    // Modo incognito o cookies bloqueadas: se sigue al sistema y no se recuerda.
    return null;
  }
};

export function aplicarTema(tema) {
  document.documentElement.dataset.tema = tema;
}

export function iniciarTema() {
  const boton = document.querySelector("#tema");
  if (!boton) return;

  boton.addEventListener("click", () => {
    const nuevo =
      document.documentElement.dataset.tema === "claro" ? "oscuro" : "claro";
    aplicarTema(nuevo);
    try {
      localStorage.setItem(CLAVE, nuevo);
    } catch (_) {
      /* sin almacenamiento el cambio dura lo que la pagina */
    }
  });

  // Seguir al sistema mientras el usuario no haya elegido.
  window
    .matchMedia("(prefers-color-scheme: light)")
    .addEventListener("change", () => {
      if (!guardado()) aplicarTema(preferenciaDelSistema());
    });
}
