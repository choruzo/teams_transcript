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
import { aFecha, duracion, fechaCorta, fechaLarga, nombreEstado, plural } from "../formato.js";

const ETIQUETAS = 200; // columna izquierda con la descripcion de cada accion
const ETIQUETAS_MIN = 560; // por debajo de este ancho se prescinde de ella
const MARGEN_DER = 16;
// Sitio a la derecha para la cola del tramo ("×3 · 2 fusionadas"), que se
// escribe detras del ultimo punto y SVG no recorta: sin el, se sale del lienzo.
const ANCHO_CARACTER_COLA = 6.2;
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

const anchoUtil = (ancho, conEtiquetas, margen = MARGEN_DER) =>
  ancho - (conEtiquetas ? ETIQUETAS : 12) - margen;

function margenDerecho(datos) {
  const largo = Math.max(
    0,
    ...datos.carriles.map((c) => cola(c).length + (c.revisar ? 2 : 0))
  );
  return Math.max(MARGEN_DER, largo ? largo * ANCHO_CARACTER_COLA + 14 : 0);
}

/** Dominio temporal: el filtro manda; si no hay filtro, lo que haya que pintar. */
function dominio(datos) {
  const fechas = [
    ...datos.reuniones.map((r) => r.fecha),
    ...datos.carriles.flatMap((c) => [c.primera_fecha, c.ultima_fecha]),
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

/** El texto a la derecha del tramo: aviso de revisar, menciones y fusionadas. */
function cola(carril) {
  const partes = [];
  if (carril.menciones > 1) partes.push(`×${carril.menciones}`);
  if (carril.absorbidas) {
    partes.push(plural(carril.absorbidas, "fusionada", "fusionadas"));
  }
  return partes.join(" · ");
}

function tooltip(carril) {
  const lineas = [
    carril.descripcion,
    `${carril.persona || "sin responsable"} · ${nombreEstado(carril.estado)} · ` +
      `${carril.menciones} mención(es)`,
    `de ${fechaLarga(carril.primera_fecha)} a ${fechaLarga(carril.ultima_fecha)}`,
  ];
  if (carril.estancada) lineas.push("ESTANCADA");
  if (carril.absorbidas) {
    lineas.push(`Ha absorbido ${plural(carril.absorbidas, "acción", "acciones")}`);
  }
  if (carril.depende_de.length) {
    lineas.push(`Depende de ${plural(carril.depende_de.length, "acción", "acciones")}`);
  }
  if (carril.bloquea_a.length) {
    lineas.push(`Bloquea a ${plural(carril.bloquea_a.length, "acción", "acciones")}`);
  }
  if (carril.revisar) {
    lineas.push("REVISAR: el modelo ya no la encontró al reprocesar su reunión");
  }
  if (carril.descartada) lineas.push("DESCARTADA");
  lineas.push("Pulsa para abrir la ficha");
  return lineas.join("\n");
}

/**
 * Resalta los carriles relacionados con `grupo` por dependencias, o lo
 * deshace con `null`. Sin flechas permanentes: con cien carriles serían una
 * maraña, y el dato completo está en el panel.
 */
function resaltarRelacionadas(svg, carril) {
  svg.classList.remove("tl-relacionando");
  for (const nodo of svg.querySelectorAll(".tl-depende, .tl-bloquea, .tl-foco")) {
    nodo.classList.remove("tl-depende", "tl-bloquea", "tl-foco");
  }
  if (!carril || !(carril.depende_de.length || carril.bloquea_a.length)) return;
  const buscar = (uid) => svg.querySelector(`[data-accion="${CSS.escape(uid)}"]`);
  svg.classList.add("tl-relacionando");
  buscar(carril.uid)?.classList.add("tl-foco");
  carril.depende_de.forEach((uid) => buscar(uid)?.classList.add("tl-depende"));
  carril.bloquea_a.forEach((uid) => buscar(uid)?.classList.add("tl-bloquea"));
}

function carriles(datos, x, ancho, conEtiquetas, [min, max], { alAccion, seleccionada }) {
  const alto = Math.max(ALTO_CARRIL * datos.carriles.length + 8, 40);
  const svg = lienzo(ancho, alto, "Acciones a lo largo del tiempo");
  svg.classList.add("tl-carriles");
  // Desde I6a cada carril es un botón: con `role="img"` los lectores de
  // pantalla verían una imagen opaca y no podrían abrir ninguna ficha.
  svg.setAttribute("role", "group");

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
    // El tramo empieza en la primera mencion, no en la reunion de origen:
    // tras una fusion, la absorbida puede ser mas antigua que la principal.
    const inicioReal = aFecha(carril.primera_fecha).getTime();
    const finReal = aFecha(carril.ultima_fecha).getTime();
    // Un tramo puede empezar antes del periodo mirado (una accion de hace dos
    // meses que sigue viva). Se recorta y el extremo se dibuja sin punto, para
    // que se vea que viene de fuera.
    const cortadoIzq = inicioReal < min;
    const cortadoDer = finReal > max;
    const x1 = x(Math.max(inicioReal, min));
    const x2 = x(Math.min(finReal, max));
    const clases = [
      "tl-carril",
      `e-${carril.estado}`,
      carril.estancada ? "estancada" : "",
      carril.descartada ? "descartada" : "",
      carril.revisar ? "revisar" : "",
      carril.uid === seleccionada ? "tl-seleccionada" : "",
    ]
      .filter(Boolean)
      .join(" ");

    const grupo = crear("g", {
      class: clases,
      tabindex: 0,
      role: "button",
      "aria-label": `Abrir la ficha: ${carril.descripcion}`,
      datos: { accion: carril.uid },
    });
    // Franja invisible del ancho entero: el blanco de clic es la fila, no
    // solo una barra de diez pixeles o un punto suelto.
    grupo.appendChild(
      crear("rect", {
        class: "tl-fila",
        x: 0,
        y: y - ALTO_CARRIL / 2,
        width: ancho,
        height: ALTO_CARRIL,
      })
    );
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
    // Una marca por reunion que la menciono (D10, resuelta en el esquema 3).
    // Las de los extremos son los nodos grandes; las de fuera del periodo no
    // se dibujan.
    const extremos = new Set();
    if (!cortadoIzq) extremos.add(Math.round(x1));
    if (!cortadoDer) extremos.add(Math.round(x2));
    for (const marca of carril.marcas) {
      const t = aFecha(marca.fecha).getTime();
      if (t < min || t > max) continue;
      const cx = x(t);
      if (extremos.has(Math.round(cx))) continue;
      grupo.appendChild(crear("circle", { class: "tl-mencion", cx, cy: y, r: 2.5 }));
    }
    if (!cortadoIzq) {
      grupo.appendChild(crear("circle", { class: "tl-nodo", cx: x1, cy: y, r: 4 }));
    }
    if (!cortadoDer && Math.abs(x2 - x1) > 2) {
      grupo.appendChild(crear("circle", { class: "tl-nodo", cx: x2, cy: y, r: 4 }));
    }

    const detras = Math.max(x1, x2) + 10;
    const texto = cola(carril);
    if (carril.revisar || texto) {
      const nodo = crear("text", { class: "tl-menciones", x: detras, y: y + 4 });
      if (carril.revisar) {
        nodo.appendChild(crear("tspan", { class: "tl-revisar", texto: "⚠ " }));
      }
      if (texto) nodo.appendChild(crear("tspan", { texto }));
      grupo.appendChild(nodo);
    }

    if (conEtiquetas) {
      grupo.appendChild(
        crear("text", {
          class: "tl-etiqueta",
          x: 0,
          y: y + 4,
          texto: recortar(carril.descripcion),
        })
      );
    }
    grupo.appendChild(titulo(tooltip(carril)));

    const abrir = () => alAccion(carril.uid, grupo);
    grupo.addEventListener("click", abrir);
    grupo.addEventListener("keydown", (evento) => {
      if (evento.key === "Enter" || evento.key === " ") {
        evento.preventDefault();
        abrir();
      }
    });
    grupo.addEventListener("pointerenter", () => resaltarRelacionadas(svg, carril));
    grupo.addEventListener("pointerleave", () => resaltarRelacionadas(svg, null));
    grupo.addEventListener("focus", () => resaltarRelacionadas(svg, carril));
    grupo.addEventListener("blur", () => resaltarRelacionadas(svg, null));
    svg.appendChild(grupo);
  });
  return svg;
}

/** Marca la accion abierta en el panel sin redibujar todo el timeline. */
export function marcarSeleccionada(contenedor, uid) {
  for (const nodo of contenedor.querySelectorAll(".tl-seleccionada")) {
    nodo.classList.remove("tl-seleccionada");
  }
  if (uid) {
    contenedor
      .querySelector(`.tl-carril[data-accion="${CSS.escape(uid)}"]`)
      ?.classList.add("tl-seleccionada");
  }
}

/**
 * Pinta el timeline dentro de `contenedor`.
 *
 * - `alReunion(uid)`: al pulsar una reunion.
 * - `alAccion(uid, elemento)`: al pulsar un carril; desde I6a abre el panel
 *   de la accion (antes llevaba a su ultima reunion, que ahora es un boton
 *   dentro del panel).
 * - `seleccionada`: el uid de la accion abierta en el panel, para marcarla.
 */
export function dibujarTimeline(contenedor, datos, { alReunion, alAccion, seleccionada = null }) {
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
  const x = escala([min, max], [izquierda, izquierda + anchoUtil(ancho, conEtiquetas, margenDerecho(datos))]);

  const cajaCabecera = document.createElement("div");
  cajaCabecera.className = "tl-fija";
  cajaCabecera.appendChild(cabecera(datos, x, ancho, alReunion));
  contenedor.appendChild(cajaCabecera);

  const cajaCarriles = document.createElement("div");
  cajaCarriles.className = "tl-scroll";
  if (datos.carriles.length) {
    cajaCarriles.appendChild(
      carriles(datos, x, ancho, conEtiquetas, [min, max], { alAccion, seleccionada })
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
