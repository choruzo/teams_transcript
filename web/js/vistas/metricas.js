// Panel de salud del equipo (seccion 3.2 del plan).
//
// Todo lo que se pinta aqui viene de SQL, no del LLM: son cifras comprobables.
// Tres reglas que no hay que romper al añadir una métrica:
//
// 1. **Decir el alcance.** Las acciones son el estado de hoy del histórico
//    entero; las reuniones, los riesgos y el reparto son del periodo filtrado.
//    Mezclarlos sin decirlo produce números que nadie sabe interpretar.
// 2. **No prometer lo que el dato no soporta.** Los riesgos son "mencionados en
//    el periodo" y nunca "abiertos" (D3), y los cierres llevan su advertencia
//    porque `cerrada_en` es la fecha de proceso (D4).
// 3. **Con poco dato, el dato crudo.** Por debajo de MIN_SEMANAS no se dibuja
//    una serie temporal: tres barras no son una tendencia.

import { crear, escala, lienzo, pasos, titulo } from "../svg.js";
import { escapar, etiquetaSemana, minutos, nombreEstado, plural } from "../formato.js";

const MIN_SEMANAS = 3;

const ALTO_SERIE = 130;
const MARGEN = { arriba: 18, derecha: 8, abajo: 22, izquierda: 34 };

function cifra(valor, etiqueta, clase = "") {
  return `
    <div class="cifra ${clase}">
      <b>${valor}</b>
      <span>${etiqueta}</span>
    </div>`;
}

/** Las tres cosas que justifican abrir esto un lunes por la mañana. */
function alertas(datos) {
  const avisos = [];
  if (datos.estancadas) {
    avisos.push({
      nivel: "mal",
      texto:
        `<b>${plural(datos.estancadas, "acción estancada", "acciones estancadas")}</b>` +
        ` — ${datos.umbral_estancamiento} menciones o más sin cerrarse.`,
    });
  }
  const altas = datos.riesgos.por_severidad.alta || 0;
  if (altas) {
    avisos.push({
      nivel: "mal",
      texto:
        `<b>${plural(altas, "riesgo de severidad alta", "riesgos de severidad alta")}</b>` +
        " mencionados en el periodo.",
    });
  }
  if (datos.reuniones_sin_duracion) {
    // No es una alerta del equipo sino del dato: sin `.srt` no hay duración
    // (D9), y los minutos de abajo se quedan cortos. Mejor decirlo que dejar
    // que alguien saque conclusiones de una suma incompleta.
    avisos.push({
      nivel: "aviso",
      texto:
        `${plural(datos.reuniones_sin_duracion, "reunión", "reuniones")} sin duración ` +
        "registrada: los minutos del periodo se quedan cortos.",
    });
  }
  if (!avisos.length) return "";
  return `<ul class="alertas">${avisos
    .map((a) => `<li class="${a.nivel}">${a.texto}</li>`)
    .join("")}</ul>`;
}

function resumen(datos) {
  const abiertas = datos.acciones_abiertas;
  const desglose = Object.entries(datos.acciones)
    .filter(([, n]) => n > 0)
    .map(([estado, n]) => `${nombreEstado(estado).toLowerCase()} ${n}`)
    .join(" · ");
  return `
    <div class="cifras-grandes">
      ${cifra(abiertas, "acciones vivas hoy", abiertas ? "" : "apagada")}
      ${cifra(datos.estancadas, "estancadas", datos.estancadas ? "mal" : "apagada")}
      ${cifra(datos.reuniones, "reuniones en el periodo")}
      ${cifra(minutos(datos.minutos), "de reunión")}
    </div>
    <p class="nota">${escapar(desglose)}. Las acciones cuentan el histórico
      completo a día de hoy; el resto, solo el periodo filtrado.</p>`;
}

/** Serie por semanas: reuniones/minutos y cierres. */
function series(datos, ancho) {
  const semanas = datos.semanas;
  if (!semanas.length) return null;
  if (semanas.length < MIN_SEMANAS) {
    // Dato crudo: con una o dos semanas, un gráfico sugiere una tendencia que
    // no existe.
    return tabla(semanas);
  }

  const max = Math.max(...semanas.map((s) => s.minutos), 1);
  const svg = lienzo(ancho, ALTO_SERIE, "Minutos de reunión y cierres por semana");
  svg.classList.add("grafico");
  const y = escala([0, max], [ALTO_SERIE - MARGEN.abajo, MARGEN.arriba]);
  const util = ancho - MARGEN.izquierda - MARGEN.derecha;
  const paso = util / semanas.length;
  const anchoBarra = Math.min(paso * 0.62, 40);

  for (const valor of pasos(max)) {
    svg.appendChild(
      crear("line", {
        class: "g-rejilla",
        x1: MARGEN.izquierda,
        y1: y(valor),
        x2: ancho - MARGEN.derecha,
        y2: y(valor),
      })
    );
    svg.appendChild(
      crear("text", {
        class: "g-tick",
        x: MARGEN.izquierda - 6,
        y: y(valor) + 4,
        "text-anchor": "end",
        texto: valor,
      })
    );
  }

  semanas.forEach((semana, i) => {
    const cx = MARGEN.izquierda + paso * (i + 0.5);
    const alto = Math.max(ALTO_SERIE - MARGEN.abajo - y(semana.minutos), 1);
    const grupo = crear("g", { class: "g-barra" });
    grupo.appendChild(
      crear("rect", {
        x: cx - anchoBarra / 2,
        y: y(semana.minutos),
        width: anchoBarra,
        height: alto,
        rx: 3,
      })
    );
    // Los cierres van como marca sobre la barra y no como segunda serie: son
    // otra unidad (acciones, no minutos) y compartir el eje mentiría. El punto
    // situa y el número dice cuántos; el círculo se queda pequeño para que no
    // compita con la barra, que es el dato principal.
    if (semana.cierres) {
      grupo.appendChild(
        crear("circle", { class: "g-cierres", cx, cy: y(semana.minutos) - 7, r: 3 })
      );
      grupo.appendChild(
        crear("text", {
          class: "g-cierres-n",
          x: cx,
          y: y(semana.minutos) - 14,
          "text-anchor": "middle",
          texto: semana.cierres,
        })
      );
    }
    grupo.appendChild(
      titulo(
        `Semana del ${etiquetaSemana(semana.semana)}: ` +
          `${minutos(semana.minutos)} en ${plural(semana.reuniones, "reunión", "reuniones")}` +
          (semana.sin_duracion ? ` (${semana.sin_duracion} sin duración)` : "") +
          `\n${plural(semana.cierres, "acción cerrada", "acciones cerradas")}`
      )
    );
    svg.appendChild(grupo);
    if (i % Math.ceil(semanas.length / 8) === 0) {
      svg.appendChild(
        crear("text", {
          class: "g-tick",
          x: cx,
          y: ALTO_SERIE - 6,
          "text-anchor": "middle",
          texto: etiquetaSemana(semana.semana),
        })
      );
    }
  });
  return svg;
}

function tabla(semanas) {
  const caja = document.createElement("div");
  caja.className = "tabla-cruda";
  caja.innerHTML = `
    <p class="nota">Pocas semanas para una serie; se muestran los datos tal cual.</p>
    <table>
      <thead><tr><th>Semana</th><th>Reuniones</th><th>Minutos</th><th>Cierres</th></tr></thead>
      <tbody>${semanas
        .map(
          (s) => `<tr>
            <td>${escapar(etiquetaSemana(s.semana))}</td>
            <td>${s.reuniones}</td>
            <td>${s.minutos}${s.sin_duracion ? ` <span class="nota">(${s.sin_duracion} sin dato)</span>` : ""}</td>
            <td>${s.cierres}</td>
          </tr>`
        )
        .join("")}</tbody>
    </table>`;
  return caja;
}

function personas(datos) {
  if (!datos.personas.length) {
    return '<p class="nota">Nadie con actividad en el periodo.</p>';
  }
  const max = Math.max(...datos.personas.map((p) => p.abiertas || 0), 1);
  return `
    <ul class="barras">
      ${datos.personas
        .map(
          (p) => `
        <li>
          <span class="nombre">${escapar(p.persona)}</span>
          <span class="barra"><i style="width:${(p.abiertas / max) * 100}%"></i></span>
          <span class="valor" title="${p.acciones} acciones nacidas en el periodo, ${p.updates} intervenciones">
            ${p.abiertas} abiertas
          </span>
        </li>`
        )
        .join("")}
    </ul>
    <p class="nota">Barra: acciones vivas hoy. El detalle del periodo, al pasar el ratón.</p>`;
}

const SEVERIDADES = ["alta", "media", "baja", "sin severidad"];

function riesgos(datos) {
  const { total, por_severidad, por_area } = datos.riesgos;
  if (!total) {
    return '<p class="nota">Ningún riesgo mencionado en el periodo.</p>';
  }
  const chips = SEVERIDADES.filter((s) => por_severidad[s]).map(
    (s) =>
      `<span class="chip s-${s.replace(" ", "-")}">${escapar(s)}: <b>${por_severidad[s]}</b></span>`
  );
  return `
    <div class="chips">${chips.join("")}</div>
    <ul class="areas">
      ${por_area
        .map((a) => `<li><span>${escapar(a.area)}</span><b>${a.n}</b></li>`)
        .join("")}
    </ul>
    <p class="nota">Riesgos <b>mencionados</b> en el periodo. La base no guarda
      el estado de un riesgo, así que uno que nadie volvió a nombrar no aparece:
      esto describe de qué se habla, no qué sigue vigente.</p>`;
}

export function dibujarMetricas(contenedor, datos) {
  contenedor.innerHTML = `
    ${alertas(datos)}
    ${resumen(datos)}
    <div class="paneles">
      <section class="panel">
        <h3>Por semana</h3>
        <div id="serie"></div>
        <p class="nota">Los cierres usan <code>cerrada_en</code>, que es la fecha
          en que se procesó la reunión y no necesariamente la del cierre real.</p>
      </section>
      <section class="panel">
        <h3>Reparto por persona</h3>
        ${personas(datos)}
      </section>
      <section class="panel">
        <h3>Riesgos mencionados</h3>
        ${riesgos(datos)}
      </section>
    </div>`;

  const hueco = contenedor.querySelector("#serie");
  const dibujo = series(datos, Math.max(hueco.clientWidth || 320, 260));
  if (dibujo) {
    hueco.appendChild(dibujo);
  } else {
    hueco.innerHTML = '<p class="nota">Sin reuniones en el periodo.</p>';
  }
}
