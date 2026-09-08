// Timeline de reuniones con carriles de accion (seccion 3.1 del plan).
//
// Dos SVG que comparten la misma escala horizontal: la cabecera (marcas de
// reunion y eje de fechas) y los carriles. Van separados para que la cabecera
// quede fija mientras se desplazan cien carriles; con un solo SVG habria que
// elegir entre perder el eje de vista o limitar el numero de acciones.
//
// **No hay zoom ni desplazamiento propios.** El "zoom" son los presets de
// periodo (semana / mes / trimestre), que ademas quedan en la URL y se pueden
// compartir; reimplementar pan y zoom sobre el SVG daria una vista que no se
// puede enlazar.

import { crear, escala, lienzo, titulo } from "../svg.js";
import { aFecha, duracion, fechaCorta, fechaLarga, nombreEstado } from "../formato.js";

const ETIQUETAS = 200; // columna izquierda con la descripcion de cada accion
const ETIQUETAS_MIN = 560; // por debajo de este ancho se prescinde de ella
const MARGEN_DER = 16;
const ALTO_MARCA = 44; // altura maxima de la marca de una reunion
const ALTO_CABECERA = ALTO_MARCA + 34;
const ALTO_CARRIL = 24;
const DIA = 86400000;

// SVG no recorta el texto que se sale de su caja: hay que hacerlo a mano. El
// numero sale de la anchura de la columna dividida por la anchura media de un
// caracter a 11,5 px; la descripcion entera queda en el `<title>`.
const MAX_CARACTERES = 30;
const recortar = (texto) =>
  texto.length > MAX_CARACTERES ? texto.slice(0, MAX_CARACTERES - 1) + "…" : texto;

const anchoUtil = (ancho, conEtiquetas) =>
  ancho - (conEtiquetas ? ETIQUETAS : 12) - MARGEN_DER;

/** Dominio temporal: el filtro manda; si no hay filtro, lo que haya que pintar. */
function dominio(datos) {
  const fechas = [
    ...datos.reuniones.map((r) => r.fecha),
    ...datos.carriles.flatMap((c) => [c.origen_fecha, c.ultima_fecha]),
  ].map((f) => aFecha(f).getTime());
  let min = datos.desde ? aFecha(datos.desde).getTime() : Math.min(...fechas);
  let max = datos.hasta ? aFecha(datos.hasta).getTime() : Math.max(...fechas);
  if (!(min <= max)) [min, max] = [Math.min(...fechas), Math.max(...fechas)];
  // Un dia de aire a cada lado para que las marcas de los extremos no queden
  // cortadas por la mitad contra el borde del dibujo.
  return [min - DIA / 2, max + DIA / 2];
}

function cabecera(datos, x, ancho, alFijar) {
  const svg = lienzo(ancho, ALTO_CABECERA, "Reuniones del periodo");
  svg.classList.add("tl-cabecera");
  const base = ALTO_MARCA + 4;

  // Escala de altura por duracion. El maximo se reparte sobre la reunion mas
  // larga del periodo, no sobre un valor fijo: lo que interesa comparar es
  // unas reuniones con otras.
  const masLarga = Math.max(
    ...datos.reuniones.map((r) => r.duracion_seg || 0),
    1
  );

  svg.appendChild(
    crear("line", { class: "tl-eje", x1: 0, y1: base, x2: ancho, y2: base })
  );

  let ultimaEtiqueta = -Infinity;
  for (const reunion of datos.reuniones) {
    const cx = x(aFecha(reunion.fecha).getTime());
    const alto = reunion.duracion_seg
      ? Math.max(8, (reunion.duracion_seg / masLarga) * ALTO_MARCA)
      : 8; // D9: sin `.srt` no hay duracion; se dibuja el minimo y se dice
    const grupo = crear("g", {
      class: `tl-reunion${reunion.duracion_seg ? "" : " sin-duracion"}`,
      tabindex: 0,
      role: "button",
      datos: { uid: reunion.uid, tipo: reunion.tipo },
    });
    grupo.appendChild(
      crear("rect", {
        class: `tl-marca t-${reunion.tipo}`,
        x: cx - 5,
        y: base - alto,
        width: 10,
        height: alto,
        rx: 3,
      })
    );
    grupo.appendChild(
      titulo(
        `${fechaLarga(reunion.fecha)} · ${reunion.tipo}` +
          (reunion.duracion_seg ? ` · ${duracion(reunion.duracion_seg)}` : "")
      )
    );
    grupo.addEventListener("click", () => alFijar(reunion.uid));
    grupo.addEventListener("keydown", (evento) => {
      if (evento.key === "Enter" || evento.key === " ") {
        evento.preventDefault();
        alFijar(reunion.uid);
      }
    });
    svg.appendChild(grupo);

    // La fecha solo se escribe si no pisa a la anterior. Con un trimestre de
    // dailys no caben todas, y superpuestas no se lee ninguna.
    if (cx - ultimaEtiqueta > 52) {
      // Las de los extremos se anclan por su lado para no salirse del lienzo:
      // centradas, la primera y la ultima quedan cortadas por el borde.
      const anclaje =
        cx < 30 ? "start" : cx > ancho - 30 ? "end" : "middle";
      svg.appendChild(
        crear("text", {
          class: "tl-fecha",
          x: cx,
          y: base + 16,
          "text-anchor": anclaje,
          texto: fechaCorta(reunion.fecha),
        })
      );
      ultimaEtiqueta = cx;
    }
  }
  return svg;
}

function carriles(datos, x, ancho, conEtiquetas, [min, max], alFijar) {
  const alto = Math.max(ALTO_CARRIL * datos.carriles.length + 8, 40);
  const svg = lienzo(ancho, alto, "Acciones a lo largo del tiempo");
  svg.classList.add("tl-carriles");

  // Guias verticales bajo cada reunion: son las que permiten *ver* que una
  // barra cruza tres dailys, que es el criterio de aceptacion de esta fase.
  for (const reunion of datos.reuniones) {
    const cx = x(aFecha(reunion.fecha).getTime());
    svg.appendChild(
      crear("line", { class: "tl-guia", x1: cx, y1: 0, x2: cx, y2: alto })
    );
  }

  datos.carriles.forEach((carril, i) => {
    const y = i * ALTO_CARRIL + ALTO_CARRIL / 2;
    const inicioReal = aFecha(carril.origen_fecha).getTime();
    const finReal = aFecha(carril.ultima_fecha).getTime();
    // Un tramo puede empezar antes del periodo mirado (una accion de hace dos
    // meses que sigue viva). Se recorta y el extremo se dibuja plano en vez de
    // redondeado, para que se vea que viene de fuera.
    const cortadoIzq = inicioReal < min;
    const cortadoDer = finReal > max;
    const x1 = x(Math.max(inicioReal, min));
    const x2 = x(Math.min(finReal, max));
    const clases = [
      "tl-carril",
      `e-${carril.estado}`,
      carril.estancada ? "estancada" : "",
    ].join(" ");

    const grupo = crear("g", {
      class: clases,
      tabindex: 0,
      role: "button",
      datos: { uid: carril.ultima_uid },
    });
    grupo.appendChild(
      crear("rect", {
        class: "tl-barra",
        x: Math.min(x1, x2) - 4,
        y: y - 5,
        width: Math.abs(x2 - x1) + 8,
        height: 10,
        rx: 5,
      })
    );
    // Extremos: un punto marca el nacimiento y otro la ultima mencion. Las
    // menciones intermedias no se dibujan porque la base no las guarda (D10);
    // el numero de veces va en el texto.
    if (!cortadoIzq) {
      grupo.appendChild(crear("circle", { class: "tl-nodo", cx: x1, cy: y, r: 4 }));
    }
    if (!cortadoDer && Math.abs(x2 - x1) > 2) {
      grupo.appendChild(crear("circle", { class: "tl-nodo", cx: x2, cy: y, r: 4 }));
    }
    if (carril.menciones > 1) {
      grupo.appendChild(
        crear("text", {
          class: "tl-menciones",
          x: Math.max(x1, x2) + 10,
          y: y + 4,
          texto: `×${carril.menciones}`,
        })
      );
    }
    grupo.appendChild(
      titulo(
        `${carril.descripcion}\n${carril.persona || "sin responsable"} · ` +
          `${nombreEstado(carril.estado)} · ${carril.menciones} mención(es)\n` +
          `de ${fechaLarga(carril.origen_fecha)} a ${fechaLarga(carril.ultima_fecha)}` +
          (carril.estancada ? "\nESTANCADA" : "")
      )
    );
    grupo.addEventListener("click", () => alFijar(carril.ultima_uid));
    svg.appendChild(grupo);

    if (conEtiquetas) {
      const texto = crear("text", {
        class: "tl-etiqueta",
        x: 0,
        y: y + 4,
        texto: recortar(carril.descripcion),
      });
      texto.appendChild(titulo(carril.descripcion));
      svg.appendChild(texto);
    }
  });
  return svg;
}

/**
 * Pinta el timeline dentro de `contenedor`.
 *
 * `alFijar(uid)` se llama al pulsar una reunion o una accion. En I1 resalta la
 * tarjeta del listado; cuando exista la vista de reunion (I2) sera el sitio
 * donde se cambie el destino, y solo aqui.
 */
export function dibujarTimeline(contenedor, datos, alFijar) {
  contenedor.innerHTML = "";
  if (!datos.reuniones.length) {
    contenedor.innerHTML =
      '<div class="aviso">No hay reuniones en este periodo.</div>';
    return;
  }

  // `clientWidth` incluye el relleno del contenedor: sin descontarlo el SVG
  // sale mas ancho que su caja y aparece una barra de desplazamiento
  // horizontal en toda la pagina.
  const estilo = getComputedStyle(contenedor);
  const relleno =
    parseFloat(estilo.paddingLeft || 0) + parseFloat(estilo.paddingRight || 0);
  const ancho = Math.max((contenedor.clientWidth || 720) - relleno, 300);
  const conEtiquetas = ancho >= ETIQUETAS_MIN;
  const izquierda = conEtiquetas ? ETIQUETAS : 12;
  const [min, max] = dominio(datos);
  const x = escala([min, max], [izquierda, izquierda + anchoUtil(ancho, conEtiquetas)]);

  const cajaCabecera = document.createElement("div");
  cajaCabecera.className = "tl-fija";
  cajaCabecera.appendChild(cabecera(datos, x, ancho, alFijar));
  contenedor.appendChild(cajaCabecera);

  const cajaCarriles = document.createElement("div");
  cajaCarriles.className = "tl-scroll";
  if (datos.carriles.length) {
    cajaCarriles.appendChild(
      carriles(datos, x, ancho, conEtiquetas, [min, max], alFijar)
    );
  } else {
    cajaCarriles.innerHTML =
      '<p class="tl-vacio">Ninguna acción abierta cruza este periodo.</p>';
  }
  contenedor.appendChild(cajaCarriles);

  // Con una sola reunion (o con todas las acciones recien nacidas) los carriles
  // son puntos sueltos y el dibujo parece roto. No lo esta: es que todavia no
  // hay ningun arrastre que mostrar, y es mejor decirlo.
  const hayTramos = datos.carriles.some((c) => c.menciones > 1);
  if (datos.carriles.length && !hayTramos) {
    const aviso = document.createElement("p");
    aviso.className = "tl-vacio";
    aviso.textContent =
      "Todas las acciones se mencionaron una sola vez: aún no hay arrastres " +
      "que dibujen un tramo.";
    contenedor.appendChild(aviso);
  }

  if (datos.truncado) {
    const aviso = document.createElement("p");
    aviso.className = "tl-vacio";
    aviso.textContent =
      "El periodo tiene más elementos de los que se dibujan. Estrecha las fechas para verlo completo.";
    contenedor.appendChild(aviso);
  }
}
