# -*- coding: utf-8 -*-
"""Pruebas de `report_teams.py` y de las consultas de informe de `memoria.py`.

`unittest` de la stdlib, sin dependencias ni servidor: el LLM se inyecta como
un doble (`cliente=`), igual que en `tests/test_ask.py`.

    python -m unittest discover -s tests -v
"""

import json
import sys
import tempfile
import unittest
import unittest.mock
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import memoria  # noqa: E402
import report_teams  # noqa: E402


class BaseConHistorico(unittest.TestCase):
    """Un historico pequeño pero completo, compartido por casi todo el fichero.

    Tres dailys consecutivas con la misma gente, una accion que se arrastra y
    se cierra, otra que se queda abierta y sin mencionar, y un bloqueo
    repetido en dos reuniones. Es el minimo con el que el informe tiene algo
    que decir en todas sus secciones.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db = Path(self._tmp.name) / "test.db"
        self.conn = memoria.conectar(self.db)
        self.addCleanup(self.conn.close)
        self.reuniones = {}
        self._poblar()

    def reunion(self, fecha, *, nombre=None, tipo="daily", duracion=1800.0):
        nombre = nombre or fecha.replace("-", "")
        meeting_id = memoria.crear_reunion(
            self.conn,
            fecha=fecha,
            titulo=f"Daily {fecha}",
            tipo=tipo,
            transcript_path=f"/datos/{nombre}.txt",
            duracion_seg=duracion,
        )
        self.reuniones[fecha] = meeting_id
        return meeting_id

    def _poblar(self):
        conn = self.conn

        m1 = self.reunion("2026-09-01")
        memoria.insertar_updates(conn, m1, [
            {"persona": "Ana", "trabajo": "OLS", "bloqueos": "Falta el certificado."},
            {"persona": "Beto", "trabajo": "Coverity", "bloqueos": None},
        ])
        memoria.insertar_acciones(conn, m1, [
            {"descripcion": "Pedir el certificado", "persona": "Ana",
             "estado": "bloqueada"},
            {"descripcion": "Revisar el informe de Coverity", "persona": "Beto",
             "estado": "abierta"},
        ])
        memoria.insertar_riesgos(conn, m1, [
            {"descripcion": "El entorno de pruebas caduca", "area": "infra",
             "severidad": "alta"},
        ])

        m2 = self.reunion("2026-09-03")
        memoria.insertar_updates(conn, m2, [
            {"persona": "Ana", "trabajo": "OLS", "bloqueos": "falta el certificado"},
            {"persona": "Beto", "trabajo": "Coverity", "bloqueos": "Sin GPU."},
        ])
        # Ana sigue bloqueada; la de Beto se cierra en la tercera.
        memoria.aplicar_arrastres(conn, m2, [
            {"action_id": 1, "estado": "bloqueada"},
        ])

        m3 = self.reunion("2026-09-08")
        memoria.insertar_updates(conn, m3, [
            {"persona": "Ana", "trabajo": "OLS", "bloqueos": None},
        ])
        memoria.aplicar_arrastres(conn, m3, [
            {"action_id": 1, "estado": "bloqueada"},
        ])
        memoria.insertar_acciones(conn, m3, [
            {"descripcion": "Cerrar el pipeline", "persona": "Ana",
             "estado": "completada"},
        ])
        conn.commit()

    # -- utilidades -------------------------------------------------------

    def datos(self, **kwargs):
        return report_teams.recopilar(self.conn, **kwargs)


class TestConsultasDeInforme(BaseConHistorico):
    """Las cuatro consultas nuevas de `memoria.py`."""

    def test_acciones_cerradas_por_fecha_de_reunion(self):
        """D4 resuelta en el esquema 3: la fecha es la de la reunion que la cerro."""
        cerradas = memoria.acciones_cerradas(
            self.conn, desde="2026-09-08", hasta="2026-09-08"
        )
        descripciones = [a["descripcion"] for a in cerradas]
        self.assertIn("Cerrar el pipeline", descripciones)
        self.assertNotIn("Pedir el certificado", descripciones)

    def test_acciones_cerradas_fuera_del_periodo_no_salen(self):
        cerradas = memoria.acciones_cerradas(
            self.conn, desde="2020-01-01", hasta="2020-12-31"
        )
        self.assertEqual(cerradas, [])

    def test_bloqueo_repetido_ignora_acentos_y_puntuacion(self):
        bloqueos = memoria.bloqueos_recurrentes(self.conn, minimo=2)
        self.assertEqual(len(bloqueos), 1, "solo el certificado se repite")
        self.assertEqual(bloqueos[0]["n_reuniones"], 2)
        self.assertEqual(bloqueos[0]["personas"], ["Ana"])
        # Se conserva la redaccion mas reciente, no la primera.
        self.assertEqual(bloqueos[0]["texto"], "falta el certificado")

    def test_bloqueo_de_una_sola_reunion_no_es_recurrente(self):
        textos = [
            b["texto"] for b in memoria.bloqueos_recurrentes(self.conn, minimo=2)
        ]
        self.assertNotIn("Sin GPU.", textos)

    def test_bloqueos_respetan_el_periodo(self):
        bloqueos = memoria.bloqueos_recurrentes(
            self.conn, desde="2026-09-02", minimo=2
        )
        self.assertEqual(bloqueos, [], "solo queda una mencion en el rango")

    def test_personas_sin_actualizar_cuenta_desde_hasta(self):
        # Con `hasta` en la ultima daily, Beto lleva 5 dias sin hablar y Ana 0.
        sin = memoria.personas_sin_actualizar(self.conn, hasta="2026-09-08", dias=1)
        nombres = [p["persona"] for p in sin]
        self.assertEqual(nombres, ["Beto"])
        self.assertEqual(sin[0]["dias_sin_update"], 5)

    def test_personas_sin_actualizar_incluye_lo_que_arrastran(self):
        sin = memoria.personas_sin_actualizar(self.conn, hasta="2026-09-08", dias=1)
        self.assertEqual(sin[0]["abiertas"], 1, "Beto tiene la de Coverity")

    def test_acciones_sin_mencion_cuenta_reuniones_posteriores(self):
        sin = memoria.acciones_sin_mencion(self.conn, minimo=1)
        por_descripcion = {a["descripcion"]: a for a in sin}
        self.assertIn("Revisar el informe de Coverity", por_descripcion)
        # Nacio el 1 y no se toco: quedan la del 3 y la del 8.
        self.assertEqual(
            por_descripcion["Revisar el informe de Coverity"]["reuniones_sin_mencion"],
            2,
        )
        self.assertNotIn(
            "Pedir el certificado", por_descripcion,
            "se menciono en la ultima reunion",
        )

    def test_acciones_sin_mencion_ignora_las_cerradas(self):
        descripciones = [
            a["descripcion"] for a in memoria.acciones_sin_mencion(self.conn, minimo=0)
        ]
        self.assertNotIn("Cerrar el pipeline", descripciones)

    def test_acciones_sin_mencion_respeta_el_minimo(self):
        self.assertEqual(memoria.acciones_sin_mencion(self.conn, minimo=5), [])


class TestPeriodo(unittest.TestCase):
    """Los atajos `--semanal` y `--mensual`."""

    def test_semanal_es_la_semana_iso_del_dia(self):
        # 2026-09-10 es jueves.
        self.assertEqual(
            report_teams.periodo_semanal(date(2026, 9, 10)),
            ("2026-09-07", "2026-09-13"),
        )

    def test_semanal_desde_un_lunes(self):
        self.assertEqual(
            report_teams.periodo_semanal(date(2026, 9, 7)),
            ("2026-09-07", "2026-09-13"),
        )

    def test_mensual(self):
        self.assertEqual(
            report_teams.periodo_mensual(date(2026, 9, 10)),
            ("2026-09-01", "2026-09-30"),
        )

    def test_mensual_en_diciembre_no_se_pasa_de_anio(self):
        self.assertEqual(
            report_teams.periodo_mensual(date(2026, 12, 3)),
            ("2026-12-01", "2026-12-31"),
        )


class TestRecopilar(BaseConHistorico):
    """Las cifras del informe. Son SQL: tienen que cuadrar exactamente."""

    def test_actividad_del_periodo(self):
        datos = self.datos(desde="2026-09-01", hasta="2026-09-03")
        self.assertEqual(datos["actividad"]["reuniones"], 2)
        self.assertEqual(datos["actividad"]["minutos"], 60)

    def test_las_acciones_son_del_historico_entero_no_del_periodo(self):
        """El alcance mezclado de `metricas()`, que el informe declara."""
        estrecho = self.datos(desde="2026-09-08", hasta="2026-09-08")
        ancho = self.datos()
        self.assertEqual(
            estrecho["acciones"]["abiertas_total"],
            ancho["acciones"]["abiertas_total"],
        )
        self.assertEqual(estrecho["acciones"]["abiertas_total"], 2)

    def test_abiertas_ordenadas_de_la_mas_antigua(self):
        abiertas = self.datos()["acciones"]["abiertas"]
        self.assertEqual(
            [a["descripcion"] for a in abiertas],
            ["Pedir el certificado", "Revisar el informe de Coverity"],
        )

    def test_estancada_a_partir_del_umbral(self):
        abiertas = {a["descripcion"]: a for a in self.datos()["acciones"]["abiertas"]}
        # Tres menciones (nacimiento + dos arrastres) = UMBRAL_ESTANCAMIENTO.
        self.assertEqual(abiertas["Pedir el certificado"]["menciones"], 3)
        self.assertTrue(abiertas["Pedir el certificado"]["estancada"])
        self.assertFalse(abiertas["Revisar el informe de Coverity"]["estancada"])

    def test_filtro_por_persona_alcanza_a_todas_las_listas(self):
        datos = self.datos(persona="Beto")
        self.assertEqual(
            [a["descripcion"] for a in datos["acciones"]["abiertas"]],
            ["Revisar el informe de Coverity"],
        )
        self.assertEqual(
            [a["descripcion"] for a in datos["acciones"]["sin_mencion"]],
            ["Revisar el informe de Coverity"],
        )
        self.assertEqual([p["persona"] for p in datos["personas"]], ["Beto"])

    def test_los_recuentos_respetan_el_filtro_de_persona(self):
        """La estancada es de Ana: en el informe de Beto no puede contarse."""
        datos = self.datos(persona="Beto")
        self.assertEqual(datos["acciones"]["abiertas_total"], 1)
        self.assertEqual(datos["acciones"]["estancadas"], 0)
        self.assertEqual(datos["acciones"]["por_estado"]["bloqueada"], 0)

    def test_la_actividad_no_se_filtra_por_persona(self):
        """Una reunion no es de nadie; el Markdown lo dice en la cabecera."""
        datos = self.datos(persona="Beto")
        self.assertEqual(datos["actividad"]["reuniones"], 3)
        markdown = report_teams.renderizar_markdown(datos)
        self.assertIn("los del equipo", markdown)

    def test_es_serializable(self):
        """`--json` y el prompt del modelo pasan por `json.dumps`."""
        json.dumps(self.datos(), ensure_ascii=False)


class TestNarrativa(BaseConHistorico):
    """El LLM escribe la prosa y nada mas; si falla, el informe sigue en pie."""

    def test_los_datos_del_modelo_no_llevan_identificadores(self):
        limpios = report_teams._para_el_modelo(self.datos())
        for accion in limpios["acciones"]["abiertas"]:
            self.assertNotIn("id", accion)
            self.assertNotIn("origen_uid", accion)
        for bloqueo in limpios["bloqueos_recurrentes"]:
            self.assertNotIn("reuniones", bloqueo)

    def test_el_prompt_lleva_las_cifras(self):
        mensajes = report_teams.construir_mensajes(self.datos(desde="2026-09-01"))
        self.assertEqual(mensajes[0]["role"], "system")
        self.assertIn("No inventes ni una cifra", mensajes[0]["content"])
        self.assertIn("2026-09-01", mensajes[1]["content"])
        self.assertIn("Pedir el certificado", mensajes[1]["content"])

    def test_el_glosario_entra_en_el_system_prompt(self):
        mensajes = report_teams.construir_mensajes(
            self.datos(), glosario="OLS: Online Software"
        )
        self.assertIn("OLS: Online Software", mensajes[0]["content"])

    def test_narrativa_incluida_en_el_markdown(self):
        class Doble:
            def chat(self, *args, **kwargs):
                return "  Ana lleva tres dailys bloqueada.  "

        markdown, _datos, aviso = report_teams.generar(
            self.conn, cliente=Doble()
        )
        self.assertIsNone(aviso)
        self.assertIn("## Lectura", markdown)
        self.assertIn("Ana lleva tres dailys bloqueada.", markdown)

    def test_un_llm_caido_no_tumba_el_informe(self):
        class Roto:
            def chat(self, *args, **kwargs):
                raise RuntimeError("connection refused")

        markdown, _datos, aviso = report_teams.generar(self.conn, cliente=Roto())
        self.assertIn("connection refused", aviso)
        self.assertIn("no disponible", markdown)
        # Lo importante: las cifras siguen ahi.
        self.assertIn("Pedir el certificado", markdown)

    def test_narrativa_vacia_se_trata_como_ausente(self):
        class Vacio:
            def chat(self, *args, **kwargs):
                return "   "

        _markdown, _datos, aviso = report_teams.generar(self.conn, cliente=Vacio())
        self.assertIsNotNone(aviso)

    def test_sin_llm_no_llama_a_nadie(self):
        class Prohibido:
            def chat(self, *args, **kwargs):
                raise AssertionError("no deberia llamarse")

        markdown, _datos, aviso = report_teams.generar(
            self.conn, con_llm=False, cliente=Prohibido()
        )
        self.assertIsNone(aviso)
        self.assertNotIn("## Lectura", markdown)


class TestMarkdown(BaseConHistorico):
    """Lo que se lee en pantalla, incluidas las advertencias obligatorias."""

    def setUp(self):
        super().setUp()
        self.markdown = report_teams.renderizar_markdown(
            self.datos(desde="2026-09-01", hasta="2026-09-08")
        )
        # Las advertencias van pegadas a su lista: una seccion vacia no las
        # imprime, porque no hay nada sobre lo que advertir. Las que se
        # comprueban abajo necesitan un informe donde esa lista tenga filas.
        self.completo = report_teams.renderizar_markdown(self.datos())

    def test_secciones_del_plan(self):
        for titulo in (
            "## Actividad",
            "## Acciones abiertas",
            "## Abiertas sin mencionar",
            "## Cerradas en el periodo",
            "## Bloqueos recurrentes",
            "## Reparto por persona",
            "## Sin novedades",
            "## Riesgos mencionados",
        ):
            with self.subTest(titulo=titulo):
                self.assertIn(titulo, self.markdown)

    def test_dice_que_las_acciones_son_de_hoy_y_no_del_periodo(self):
        self.assertIn("estado de **hoy** del historico completo", self.markdown)

    def test_advierte_de_la_fecha_de_cierre(self):
        """D4: `cerrada_en` es la fecha de la reunion, no la del procesado."""
        self.assertIn("Cerrar el pipeline", self.completo)
        self.assertIn("no la del procesado", self.completo)

    def test_una_seccion_vacia_no_advierte_de_nada(self):
        """En un periodo sin cierres no hay ni lista ni nota al pie."""
        sin_cierres = report_teams.renderizar_markdown(
            self.datos(desde="2026-09-01", hasta="2026-09-07")
        )
        self.assertIn("## Cerradas en el periodo\n\n(ninguna)", sin_cierres)
        self.assertNotIn("no la del procesado", sin_cierres)

    def test_advierte_de_como_se_agrupan_los_bloqueos(self):
        """D8: coincidencia de texto, no deteccion de temas."""
        self.assertIn("con otras palabras no se", self.markdown)

    def test_advierte_de_que_no_hay_roster(self):
        """D7: solo se ve a quien la base ya conoce."""
        with unittest.mock.patch.object(report_teams, "DIAS_SIN_UPDATE", 1):
            markdown = report_teams.renderizar_markdown(
                self.datos(desde="2026-09-01", hasta="2026-09-08")
            )
        self.assertIn("**Beto**: ultima novedad el 2026-09-03", markdown)
        self.assertIn("sin lista de equipo", markdown.replace("\n", " "))

    def test_advierte_de_que_los_riesgos_no_tienen_estado(self):
        """D3."""
        self.assertIn("No tienen estado ni", self.markdown)

    def test_marca_las_estancadas(self):
        self.assertIn("ESTANCADA", self.markdown)

    def test_cuenta_las_reuniones_sin_mencion(self):
        self.assertIn("2 reuniones sin nombrarla", self.markdown)

    def test_una_base_vacia_no_revienta_ni_miente(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = memoria.conectar(Path(tmp) / "vacia.db")
            try:
                markdown = report_teams.renderizar_markdown(
                    report_teams.recopilar(conn)
                )
            finally:
                conn.close()
        self.assertIn("**0 reuniones**", markdown)
        self.assertIn("(ninguna)", markdown)
        self.assertNotIn("ESTANCADA", markdown)


class TestCLI(BaseConHistorico):
    """La resolucion del periodo desde los flags."""

    def _args(self, **kwargs):
        base = {
            "semanal": False, "mensual": False, "referencia": None,
            "desde": None, "hasta": None,
        }
        base.update(kwargs)
        return type("Args", (), base)()

    def test_semanal_con_referencia(self):
        self.assertEqual(
            report_teams._resolver_periodo(
                self._args(semanal=True, referencia="2026-09-10")
            ),
            ("2026-09-07", "2026-09-13"),
        )

    def test_desde_explicito_manda_sobre_el_atajo(self):
        desde, hasta = report_teams._resolver_periodo(
            self._args(semanal=True, referencia="2026-09-10", desde="2026-01-01")
        )
        self.assertEqual(desde, "2026-01-01")
        self.assertEqual(hasta, "2026-09-13")

    def test_sin_flags_es_el_historico_completo(self):
        self.assertEqual(report_teams._resolver_periodo(self._args()), (None, None))


if __name__ == "__main__":
    unittest.main()
