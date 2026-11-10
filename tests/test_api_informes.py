# -*- coding: utf-8 -*-
"""Pruebas de los informes web (fase I8).

Se saltan enteras si FastAPI no esta instalado, como el resto de `test_api*`:
el servidor de transcripcion usa el venv de Whisper, donde
`requirements-api.txt` no tiene por que estar.

Lo que se comprueba aqui es el contrato de la capa fina: que el Markdown sea el
del motor (ni una cifra recalculada en la API), que el periodo se resuelva con
los mismos atajos que el CLI y que un fallo del modelo llegue como evento SSE
en vez de cortar el stream.

    python -m unittest discover -s tests -v
"""

import os
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import memoria  # noqa: E402
import report_teams  # noqa: E402

try:
    from fastapi.testclient import TestClient

    from api.main import app

    HAY_FASTAPI = True
except ImportError:  # pragma: no cover - depende del entorno
    HAY_FASTAPI = False


def eventos_de(texto: str) -> list[tuple[str, object]]:
    """Trocea una respuesta SSE en `(evento, dato)`.

    Se parsea a mano en vez de con una libreria porque el formato es de dos
    lineas y aqui hace falta ver **el orden** y los eventos de error.
    """
    import json

    salida = []
    for bloque in texto.split("\n\n"):
        nombre, datos = None, []
        for linea in bloque.split("\n"):
            if linea.startswith("event:"):
                nombre = linea[6:].strip()
            elif linea.startswith("data:"):
                datos.append(linea[5:].strip())
        if nombre and datos:
            salida.append((nombre, json.loads("\n".join(datos))))
    return salida


@unittest.skipUnless(HAY_FASTAPI, "requiere requirements-api.txt")
class BaseInformes(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db = Path(self._tmp.name) / "test.db"
        # El indice se apunta a un fichero que no existe: asi `--sugerencias`
        # no depende de lo que haya en el `datos/` real de la maquina.
        self.indice = Path(self._tmp.name) / "indice.db"
        self._entorno = {
            clave: os.environ.get(clave)
            for clave in (
                memoria.VARIABLE_ENTORNO,
                "TEAMS_INDICE",
                "TEAMS_AUTENTICACION",
            )
        }
        os.environ[memoria.VARIABLE_ENTORNO] = str(self.db)
        os.environ["TEAMS_INDICE"] = str(self.indice)
        os.environ["TEAMS_AUTENTICACION"] = "0"
        self.addCleanup(self._restaurar_entorno)
        self.cliente = TestClient(app)

    def _restaurar_entorno(self):
        for clave, valor in self._entorno.items():
            if valor is None:
                os.environ.pop(clave, None)
            else:
                os.environ[clave] = valor

    def poblar(self):
        """Dos reuniones este mes y una vieja, con acciones, riesgos y gente."""
        hoy = date.today()
        ayer = hoy - timedelta(days=1)
        vieja = hoy - timedelta(days=45)
        conn = memoria.conectar(self.db)
        datos = [
            (hoy, "daily", "Daily de hoy", "Ana"),
            (ayer, "retro", "Retro de ayer", "Luis"),
            (vieja, "daily", "Daily antigua", "Ana"),
        ]
        for fecha, tipo, titulo, persona in datos:
            meeting_id = memoria.crear_reunion(
                conn,
                fecha=fecha.isoformat(),
                tipo=tipo,
                titulo=titulo,
                resumen=f"Resumen de {titulo}",
                transcript_path=(
                    f"/datos/{fecha.isoformat().replace('-', '')}_090000_mixed.txt"
                ),
            )
            memoria.insertar_segmentos(
                conn, meeting_id, [{"texto": "hola", "inicio": 0.0, "fin": 1.0}]
            )
            memoria.insertar_updates(
                conn, meeting_id, [{"persona": persona, "trabajo": "algo"}]
            )
            memoria.insertar_acciones(
                conn,
                meeting_id,
                [{"descripcion": f"Accion de {titulo}", "persona": persona}],
            )
            memoria.insertar_riesgos(
                conn,
                meeting_id,
                [{"descripcion": "Riesgo", "area": "equipo", "severidad": "media"}],
            )
        conn.commit()
        conn.close()
        return hoy, vieja

    # -- ayudas ------------------------------------------------------------

    def informe(self, **parametros):
        return self.cliente.get("/api/informes", params=parametros)

    def narrativa(self, **parametros):
        return self.cliente.get("/api/informes/narrativa", params=parametros)


class TestInforme(BaseInformes):
    def test_el_markdown_es_el_del_motor(self):
        self.poblar()
        respuesta = self.informe()
        self.assertEqual(respuesta.status_code, 200)
        datos = respuesta.json()
        desde, hasta = report_teams.periodo_mensual()
        self.assertEqual((datos["desde"], datos["hasta"]), (desde, hasta))
        # Las mismas secciones que imprime la consola, en el mismo orden.
        for titulo in (
            "# Informe de reuniones",
            "## Actividad",
            "## Acciones abiertas",
            "## Riesgos mencionados",
        ):
            self.assertIn(titulo, datos["markdown"])
        # Sin pedir la narrativa, no hay seccion de lectura: no se inventa.
        self.assertNotIn("## Lectura", datos["markdown"])

    def test_el_mes_recorta_la_actividad_pero_no_las_abiertas(self):
        """Los dos alcances del informe, comprobados a la vez.

        Las reuniones y las personas son del periodo; las acciones abiertas son
        el estado de hoy del historico entero. Es el criterio de `metricas()` y
        el informe lo dice en su propia cabecera de seccion.
        """
        self.poblar()
        datos = self.informe().json()
        self.assertIn("- **2 reuniones**", datos["markdown"])
        self.assertIn("Accion de Daily de hoy", datos["markdown"])
        # La accion vieja sigue abierta, asi que sale aunque su reunion no
        # entre en el periodo: quitarla seria mentir sobre lo que hay pendiente.
        self.assertIn("Accion de Daily antigua", datos["markdown"])
        self.assertIn("estado de **hoy** del historico completo", datos["markdown"])

    def test_periodo_todo_no_recorta(self):
        self.poblar()
        datos = self.informe(periodo="todo").json()
        self.assertIsNone(datos["desde"])
        self.assertIsNone(datos["hasta"])
        self.assertIn("historico completo", datos["markdown"])
        self.assertIn("Accion de Daily antigua", datos["markdown"])

    def test_las_fechas_explicitas_mandan_sobre_el_atajo(self):
        self.poblar()
        hoy = date.today()
        datos = self.informe(
            periodo="semana",
            desde=(hoy - timedelta(days=2)).isoformat(),
            hasta=hoy.isoformat(),
        ).json()
        self.assertEqual(datos["desde"], (hoy - timedelta(days=2)).isoformat())
        self.assertEqual(datos["hasta"], hoy.isoformat())

    def test_filtra_por_persona_y_lo_dice(self):
        self.poblar()
        datos = self.informe(persona="Luis").json()
        self.assertEqual(datos["persona"], "Luis")
        # El filtro no alcanza a reuniones ni riesgos, y la cabecera lo dice.
        self.assertIn("filtrado por Luis", datos["markdown"])

    def test_la_referencia_cambia_la_semana(self):
        self.poblar()
        referencia = "2026-03-11"  # un miercoles
        datos = self.informe(periodo="semana", referencia=referencia).json()
        self.assertEqual((datos["desde"], datos["hasta"]), ("2026-03-09", "2026-03-15"))

    def test_periodo_inventado_es_422(self):
        self.poblar()
        self.assertEqual(self.informe(periodo="trimestre").status_code, 422)

    def test_fecha_mal_escrita_es_422(self):
        # El patron de la ruta solo mira la forma; `2026-13-01` la cumple y la
        # tiene que rechazar `date.fromisoformat`, con su propio mensaje.
        self.poblar()
        respuesta = self.informe(desde="2026-13-01")
        self.assertEqual(respuesta.status_code, 422)
        self.assertIn("AAAA-MM-DD", respuesta.json()["detail"])

    def test_rango_al_reves_es_422(self):
        self.poblar()
        respuesta = self.informe(desde="2026-09-10", hasta="2026-09-01")
        self.assertEqual(respuesta.status_code, 422)
        self.assertIn("al reves", respuesta.json()["detail"])

    def test_sin_motor_responde_503_explicativo(self):
        self.poblar()
        with mock.patch(
            "api.rutas.informes._motor", return_value=(None, "falta el montaje")
        ):
            respuesta = self.informe()
        self.assertEqual(respuesta.status_code, 503)
        self.assertIn("montaje", respuesta.json()["detail"])

    def test_las_sugerencias_sin_indice_avisan(self):
        self.poblar()
        datos = self.informe(sugerencias="true").json()
        self.assertTrue(datos["sugerencias"])
        self.assertIn("indice", (datos["aviso"] or "").lower())
        self.assertIn("## Posibles duplicados", datos["markdown"])
        self.assertIn("no disponibles", datos["markdown"])

    def test_sin_sugerencias_no_aparece_la_seccion(self):
        self.poblar()
        datos = self.informe().json()
        self.assertFalse(datos["sugerencias"])
        self.assertIsNone(datos["aviso"])
        self.assertNotIn("## Posibles duplicados", datos["markdown"])

    def test_la_base_manda_el_markdown_tambien_en_la_api(self):
        """Lo que se ve y lo que descarga el navegador sale de este endpoint."""
        self.poblar()
        datos = self.informe(periodo="todo").json()
        conn = memoria.conectar(self.db, solo_lectura=True)
        try:
            esperado = report_teams.renderizar_markdown(
                report_teams.recopilar(conn)
            )
        finally:
            conn.close()
        # `generado` es el mismo dia en las dos llamadas, asi que el texto es
        # identico: la API no recompone nada por su cuenta.
        self.assertEqual(datos["markdown"], esperado)


class TestNarrativa(BaseInformes):
    def test_emite_el_texto_y_el_fin(self):
        self.poblar()

        def falso_chat_stream(*_args, **_kwargs):
            yield "La semana fue "
            yield "tranquila."

        with mock.patch.object(report_teams.llm, "chat_stream", falso_chat_stream):
            respuesta = self.narrativa()
        self.assertEqual(respuesta.status_code, 200)
        self.assertTrue(
            respuesta.headers["content-type"].startswith("text/event-stream")
        )
        eventos = eventos_de(respuesta.text)
        tipos = [tipo for tipo, _ in eventos]
        self.assertEqual(tipos, ["texto", "texto", "fin"])
        self.assertEqual(
            "".join(d for t, d in eventos if t == "texto"), "La semana fue tranquila."
        )

    def test_el_texto_va_en_orden_y_antes_del_fin(self):
        self.poblar()
        with mock.patch.object(
            report_teams.llm, "chat_stream", lambda *a, **k: iter(["uno", "dos"])
        ):
            eventos = eventos_de(self.narrativa().text)
        self.assertEqual(eventos[-1][0], "fin")
        self.assertEqual([d for t, d in eventos if t == "texto"], ["uno", "dos"])

    def test_un_fallo_del_modelo_llega_como_error(self):
        self.poblar()

        def revienta(*_args, **_kwargs):
            raise RuntimeError("LiteLLM no responde")

        with mock.patch.object(report_teams.llm, "chat_stream", revienta):
            eventos = eventos_de(self.narrativa().text)
        self.assertEqual(eventos[-1][0], "error")
        self.assertIn("LiteLLM no responde", eventos[-1][1])

    def test_una_respuesta_vacia_es_un_error(self):
        """Un stream sin texto no puede dejar al usuario con una seccion vacia."""
        self.poblar()
        with mock.patch.object(
            report_teams.llm, "chat_stream", lambda *a, **k: iter(["", ""])
        ):
            eventos = eventos_de(self.narrativa().text)
        self.assertEqual(eventos[-1][0], "error")
        self.assertIn("vacia", eventos[-1][1])

    def test_sin_motor_responde_503(self):
        with mock.patch(
            "api.rutas.informes._motor", return_value=(None, "falta el montaje")
        ):
            respuesta = self.narrativa()
        self.assertEqual(respuesta.status_code, 503)

    def test_el_rango_se_valida_antes_de_abrir_el_stream(self):
        respuesta = self.narrativa(desde="2026-09-10", hasta="2026-09-01")
        self.assertEqual(respuesta.status_code, 422)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
