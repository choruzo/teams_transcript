# -*- coding: utf-8 -*-
"""Pruebas de la API web (fase I0).

Se saltan enteras si FastAPI no esta instalado: el servidor de transcripcion
usa el venv de Whisper, donde `requirements-api.txt` no tiene por que estar, y
`python -m unittest discover` debe seguir funcionando alli.

    python -m unittest discover -s tests -v
"""

import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import memoria  # noqa: E402

try:
    from fastapi.testclient import TestClient

    from api.main import app

    HAY_FASTAPI = True
except ImportError:  # pragma: no cover - depende del entorno
    HAY_FASTAPI = False


@unittest.skipUnless(HAY_FASTAPI, "requiere requirements-api.txt")
class BaseAPI(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db = Path(self._tmp.name) / "test.db"
        # `ruta_bd` lee la variable en cada peticion, asi que basta con
        # apuntarla aqui; no hay que recargar la aplicacion.
        self._db_previa = os.environ.get(memoria.VARIABLE_ENTORNO)
        os.environ[memoria.VARIABLE_ENTORNO] = str(self.db)
        self.addCleanup(self._restaurar_entorno)
        self.cliente = TestClient(app)

    def _restaurar_entorno(self):
        if self._db_previa is None:
            os.environ.pop(memoria.VARIABLE_ENTORNO, None)
        else:
            os.environ[memoria.VARIABLE_ENTORNO] = self._db_previa

    def poblar(self, reuniones=(("2026-09-01", "daily"), ("2026-09-02", "retro"))):
        conn = memoria.conectar(self.db)
        for i, (fecha, tipo) in enumerate(reuniones, start=1):
            meeting_id = memoria.crear_reunion(
                conn,
                fecha=fecha,
                tipo=tipo,
                titulo=f"Reunion {i}",
                resumen=f"Resumen {i}",
                transcript_path=f"/datos/{fecha.replace('-', '')}_090000_mixed.txt",
            )
            memoria.insertar_segmentos(
                conn, meeting_id, [{"texto": "hola", "inicio": 0.0, "fin": 1.0}]
            )
            memoria.insertar_acciones(
                conn, meeting_id, [{"descripcion": f"Accion {i}", "persona": "Javi"}]
            )
        conn.commit()
        conn.close()


class TestSalud(BaseAPI):
    def test_responde_aunque_no_haya_base(self):
        """El endpoint que diagnostica no puede caerse con lo que diagnostica."""
        respuesta = self.cliente.get("/api/salud")
        self.assertEqual(respuesta.status_code, 200)
        datos = respuesta.json()
        self.assertFalse(datos["base_accesible"])
        self.assertIn("volumen", datos["detalle"])
        self.assertFalse(self.db.exists(), "diagnosticar no debe crear la base")

    def test_cifras_con_base(self):
        self.poblar()
        datos = self.cliente.get("/api/salud").json()
        self.assertTrue(datos["base_accesible"])
        self.assertEqual(datos["reuniones"], 2)
        self.assertEqual(datos["acciones"], 2)
        self.assertEqual(datos["esquema"], memoria.ESQUEMA_VERSION)
        self.assertTrue(datos["solo_lectura"])
        self.assertIsNone(datos["detalle"])

    def test_avisa_de_esquema_atrasado(self):
        self.poblar()
        cru = sqlite3.connect(self.db)
        cru.execute("PRAGMA user_version = 1")
        cru.commit()
        cru.close()
        datos = self.cliente.get("/api/salud").json()
        self.assertEqual(datos["esquema"], 1)
        self.assertIn("esquema", datos["detalle"])


class TestReuniones(BaseAPI):
    def test_lista_de_mas_reciente_a_mas_antigua(self):
        self.poblar()
        datos = self.cliente.get("/api/reuniones").json()
        self.assertEqual(datos["total"], 2)
        self.assertEqual(
            [r["fecha"] for r in datos["reuniones"]], ["2026-09-02", "2026-09-01"]
        )

    def test_recuentos_por_reunion(self):
        self.poblar()
        reunion = self.cliente.get("/api/reuniones").json()["reuniones"][0]
        self.assertEqual(reunion["n_segmentos"], 1)
        self.assertEqual(reunion["n_acciones"], 1)
        self.assertFalse(reunion["tiene_audio"])

    def test_filtros(self):
        self.poblar()
        for consulta, esperado in [
            ("?tipo=retro", 1),
            ("?tipo=daily", 1),
            ("?desde=2026-09-02", 1),
            ("?hasta=2026-08-31", 0),
            ("?desde=2026-09-01&hasta=2026-09-02", 2),
        ]:
            with self.subTest(consulta=consulta):
                datos = self.cliente.get("/api/reuniones" + consulta).json()
                self.assertEqual(datos["total"], esperado)

    def test_total_es_el_del_filtro_no_el_de_la_pagina(self):
        self.poblar()
        datos = self.cliente.get("/api/reuniones?limite=1").json()
        self.assertEqual(datos["total"], 2)
        self.assertEqual(len(datos["reuniones"]), 1)

    def test_paginacion(self):
        self.poblar()
        primera = self.cliente.get("/api/reuniones?limite=1").json()["reuniones"][0]
        segunda = self.cliente.get(
            "/api/reuniones?limite=1&desplazamiento=1"
        ).json()["reuniones"][0]
        self.assertNotEqual(primera["uid"], segunda["uid"])

    def test_entrada_invalida_da_422_con_explicacion(self):
        self.poblar()
        respuesta = self.cliente.get("/api/reuniones?tipo=inventado")
        self.assertEqual(respuesta.status_code, 422)
        self.assertIn("daily", respuesta.json()["detail"])

        self.assertEqual(self.cliente.get("/api/reuniones?desde=ayer").status_code, 422)
        self.assertEqual(self.cliente.get("/api/reuniones?limite=0").status_code, 422)

    def test_una_reunion_por_uid(self):
        self.poblar()
        respuesta = self.cliente.get("/api/reuniones/20260901_090000_mixed")
        self.assertEqual(respuesta.status_code, 200)
        self.assertEqual(respuesta.json()["fecha"], "2026-09-01")

    def test_uid_desconocido_da_404(self):
        self.poblar()
        self.assertEqual(self.cliente.get("/api/reuniones/nada").status_code, 404)

    def test_el_uid_sobrevive_al_reproceso(self):
        """La razon de ser de D0, comprobada desde la API."""
        self.poblar()
        antes = self.cliente.get("/api/reuniones/20260901_090000_mixed").json()

        conn = memoria.conectar(self.db)  # reprocesar la misma transcripcion
        memoria.crear_reunion(
            conn,
            fecha="2026-09-01",
            titulo="Reunion 1 corregida",
            transcript_path="/datos/20260901_090000_mixed.txt",
        )
        conn.commit()
        conn.close()

        despues = self.cliente.get("/api/reuniones/20260901_090000_mixed")
        self.assertEqual(despues.status_code, 200, "la URL no puede romperse")
        self.assertEqual(despues.json()["titulo"], "Reunion 1 corregida")
        self.assertEqual(antes["uid"], despues.json()["uid"])

    def test_esquema_atrasado_da_503_accionable(self):
        """Y no un `no such column` de SQL, que fue lo que paso de verdad."""
        self.poblar()
        cru = sqlite3.connect(self.db)
        cru.execute("PRAGMA user_version = 1")
        cru.commit()
        cru.close()
        respuesta = self.cliente.get("/api/reuniones")
        self.assertEqual(respuesta.status_code, 503)
        self.assertIn("--migrar", respuesta.json()["detail"])


class TestSoloLecturaDeVerdad(BaseAPI):
    def test_la_api_no_puede_escribir(self):
        self.poblar()
        self.cliente.get("/api/reuniones")
        conn = memoria.conectar(self.db, solo_lectura=True)
        try:
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute("DELETE FROM meetings")
        finally:
            conn.close()


class TestMetricasYTimeline(BaseAPI):
    """Fase I1. Lo que se comprueba aqui es el contrato, no la aritmetica:
    los agregados ya tienen sus pruebas en test_memoria.py."""

    def poblar_con_arrastre(self):
        conn = memoria.conectar(self.db)
        primera = memoria.crear_reunion(
            conn,
            fecha="2026-09-01",
            titulo="Daily 1",
            duracion_seg=1200,
            transcript_path="/datos/20260901_090000_mixed.txt",
        )
        memoria.insertar_acciones(
            conn, primera, [{"descripcion": "Arrastrada", "persona": "Lara"}]
        )
        memoria.insertar_riesgos(
            conn, primera, [{"descripcion": "R", "area": "ULS", "severidad": "alta"}]
        )
        accion = conn.execute("SELECT id FROM actions").fetchone()["id"]
        for fecha in ("2026-09-02", "2026-09-03"):
            otra = memoria.crear_reunion(
                conn,
                fecha=fecha,
                tipo="retro",
                transcript_path=f"/datos/{fecha}.txt",
            )
            memoria.aplicar_arrastres(
                conn, otra, [{"action_id": accion, "estado": "en_progreso"}]
            )
        conn.commit()
        conn.close()

    def test_metricas_devuelve_el_objeto_completo(self):
        self.poblar_con_arrastre()
        datos = self.cliente.get("/api/metricas").json()
        self.assertEqual(datos["reuniones"], 3)
        self.assertEqual(datos["acciones_abiertas"], 1)
        self.assertEqual(datos["estancadas"], 1)
        self.assertEqual(datos["riesgos"]["por_severidad"], {"alta": 1})
        self.assertEqual(datos["umbral_estancamiento"], memoria.UMBRAL_ESTANCAMIENTO)
        self.assertEqual([p["persona"] for p in datos["personas"]], ["Lara"])

    def test_metricas_respeta_el_periodo(self):
        self.poblar_con_arrastre()
        datos = self.cliente.get("/api/metricas?desde=2026-09-02").json()
        self.assertEqual(datos["reuniones"], 2)
        self.assertEqual(datos["desde"], "2026-09-02")
        self.assertEqual(datos["riesgos"]["total"], 0, "el riesgo es del dia 1")

    def test_timeline_ordena_de_antiguo_a_reciente(self):
        """Al reves que el listado: el eje se dibuja de izquierda a derecha."""
        self.poblar_con_arrastre()
        datos = self.cliente.get("/api/timeline").json()
        self.assertEqual(
            [r["fecha"] for r in datos["reuniones"]],
            ["2026-09-01", "2026-09-02", "2026-09-03"],
        )
        self.assertEqual(datos["total_reuniones"], 3)
        self.assertFalse(datos["truncado"])

    def test_carril_con_su_tramo(self):
        self.poblar_con_arrastre()
        carril = self.cliente.get("/api/timeline").json()["carriles"][0]
        self.assertEqual(carril["origen_fecha"], "2026-09-01")
        self.assertEqual(carril["ultima_fecha"], "2026-09-03")
        self.assertEqual(carril["menciones"], 3)
        self.assertTrue(carril["estancada"])
        # Los extremos se direccionan por uid, nunca por id: el enlace tiene
        # que seguir funcionando despues de reprocesar.
        self.assertEqual(carril["origen_uid"], "20260901_090000_mixed")

    def test_solo_abiertas_por_defecto(self):
        self.poblar_con_arrastre()
        conn = memoria.conectar(self.db)
        accion = conn.execute("SELECT id FROM actions").fetchone()["id"]
        cierre = memoria.crear_reunion(
            conn, fecha="2026-09-04", transcript_path="/datos/cierre.txt"
        )
        memoria.aplicar_arrastres(
            conn, cierre, [{"action_id": accion, "estado": "completada"}]
        )
        conn.commit()
        conn.close()
        self.assertEqual(self.cliente.get("/api/timeline").json()["carriles"], [])
        con_cerradas = self.cliente.get("/api/timeline?solo_abiertas=false").json()
        self.assertEqual(len(con_cerradas["carriles"]), 1)

    def test_filtros_invalidos_dan_422(self):
        self.poblar_con_arrastre()
        self.assertEqual(
            self.cliente.get("/api/timeline?tipo=inventado").status_code, 422
        )
        self.assertEqual(self.cliente.get("/api/metricas?hasta=ayer").status_code, 422)

    def test_base_vacia_responde_200(self):
        """Un despliegue nuevo no puede recibir un error por no tener datos."""
        memoria.conectar(self.db).close()
        self.assertEqual(self.cliente.get("/api/metricas").status_code, 200)
        datos = self.cliente.get("/api/timeline").json()
        self.assertEqual(datos["reuniones"], [])
        self.assertEqual(datos["carriles"], [])

    def test_esquema_atrasado_da_503_tambien_aqui(self):
        self.poblar_con_arrastre()
        cru = sqlite3.connect(self.db)
        cru.execute("PRAGMA user_version = 1")
        cru.commit()
        cru.close()
        for ruta in ("/api/metricas", "/api/timeline"):
            with self.subTest(ruta=ruta):
                respuesta = self.cliente.get(ruta)
                self.assertEqual(respuesta.status_code, 503)
                self.assertIn("--migrar", respuesta.json()["detail"])


class TestConexionPorPeticion(BaseAPI):
    def test_se_puede_cerrar_en_otro_hilo(self):
        """FastAPI abre la conexion en un hilo del pool y la cierra en otro.

        Con varias peticiones a la vez -- la pagina lanza tres -- el `finally`
        de `deps.conexion` cae en un hilo distinto del que la abrio, y sin
        `entre_hilos=True` sqlite3 aborta con "SQLite objects created in a
        thread can only be used in that same thread". Paso de verdad al montar
        esta fase, y no se reproduce con TestClient porque serializa las
        peticiones; por eso la prueba ataca la dependencia directamente.
        """
        import threading

        from api.deps import conexion

        self.poblar()
        generador = conexion()
        conn = next(generador)
        conn.execute("SELECT count(*) FROM meetings").fetchone()

        fallo = []

        def cerrar():
            try:
                next(generador, None)  # ejecuta el finally: conn.close()
            except Exception as exc:  # noqa: BLE001
                fallo.append(exc)

        hilo = threading.Thread(target=cerrar)
        hilo.start()
        hilo.join()
        self.assertEqual(fallo, [], "la conexion debe poder cerrarse en otro hilo")

    def test_la_pagina_pide_las_tres_cosas_sin_error(self):
        self.poblar()
        for ruta in ("/api/timeline", "/api/metricas", "/api/reuniones"):
            with self.subTest(ruta=ruta):
                self.assertEqual(self.cliente.get(ruta).status_code, 200)


class TestVistaDeReunion(BaseAPI):
    """Fase I2: la reunion completa y su transcripcion.

    La base se puebla a mano (y no con `poblar`) porque esta vista necesita
    justo lo que la del listado no miraba: hablantes, intervenciones, riesgos y
    un arrastre de verdad entre dos reuniones.
    """

    DATOS_MARTES = {
        "resumen": "Seguimos",
        "hablantes": [],
        "por_persona": [],
        "acciones": [],
        "arrastres": [
            {"action_id": 1, "estado": "en_progreso", "comentario": "falta revisarlo"}
        ],
        "riesgos": [],
        "retro": {"bien": ["el despliegue"], "mal": [], "mejoras": ["mas tests"]},
    }

    def poblar_detalle(self):
        conn = memoria.conectar(self.db)
        lunes = memoria.crear_reunion(
            conn,
            fecha="2026-09-07",
            tipo="daily",
            titulo="Daily del lunes",
            resumen="Arrancamos",
            transcript_path="/datos/lunes.txt",
            modelo_whisper="medium",
            modelo_llm="qwen",
            datos_json={
                "resumen": "Arrancamos",
                "hablantes": [],
                "por_persona": [],
                "acciones": [],
                "arrastres": [],
                "riesgos": [],
            },
        )
        mapa = memoria.registrar_hablantes(
            conn,
            lunes,
            [
                {"etiqueta": "SPEAKER_00", "nombre": "Javi", "confianza": "alta"},
                {"etiqueta": "SPEAKER_01", "nombre": "no identificado"},
            ],
        )
        memoria.insertar_segmentos(
            conn,
            lunes,
            [
                {"texto": "hola", "inicio": 0.0, "fin": 1.5, "etiqueta": "SPEAKER_00"},
                {"texto": "que tal", "inicio": 1.5, "fin": 3.0, "etiqueta": "SPEAKER_01"},
                {"texto": "bien", "inicio": 3.0, "fin": 4.25, "etiqueta": "SPEAKER_00"},
            ],
            mapa,
        )
        memoria.insertar_updates(
            conn, lunes, [{"persona": "Javi", "trabajo": "la API", "bloqueos": "ninguno"}]
        )
        memoria.insertar_riesgos(
            conn, lunes, [{"descripcion": "el servidor se cae", "severidad": "alta"}]
        )
        memoria.insertar_acciones(
            conn, lunes, [{"descripcion": "Cerrar el informe", "persona": "Javi"}]
        )
        accion = conn.execute("SELECT id FROM actions").fetchone()["id"]
        martes = memoria.crear_reunion(
            conn,
            fecha="2026-09-08",
            tipo="retro",
            titulo="Retro",
            transcript_path="/datos/martes.txt",
            datos_json=dict(self.DATOS_MARTES, arrastres=[
                {"action_id": accion, "estado": "en_progreso",
                 "comentario": "falta revisarlo"}
            ]),
        )
        memoria.aplicar_arrastres(
            conn, martes, [{"action_id": accion, "estado": "en_progreso"}]
        )
        conn.commit()
        conn.close()

    def detalle(self, uid="lunes"):
        respuesta = self.cliente.get(f"/api/reuniones/{uid}")
        self.assertEqual(respuesta.status_code, 200)
        return respuesta.json()

    def test_el_detalle_amplia_la_ficha_del_listado(self):
        """Es un superconjunto: lo que el listado ya consumia sigue estando."""
        self.poblar_detalle()
        datos = self.detalle()
        for campo in ("uid", "fecha", "tipo", "resumen", "n_segmentos", "n_acciones"):
            self.assertIn(campo, datos)
        self.assertEqual(datos["n_segmentos"], 3)
        self.assertEqual(datos["modelo_whisper"], "medium")
        self.assertEqual(datos["modelo_llm"], "qwen")

    def test_hablantes_intervenciones_y_riesgos(self):
        self.poblar_detalle()
        datos = self.detalle()
        hablantes = {h["etiqueta"]: h for h in datos["hablantes"]}
        self.assertEqual(hablantes["SPEAKER_00"]["persona"], "Javi")
        self.assertIsNone(hablantes["SPEAKER_01"]["persona"])
        self.assertEqual(datos["intervenciones"][0]["trabajo"], "la API")
        self.assertEqual(datos["riesgos"][0]["severidad"], "alta")

    def test_la_accion_se_ve_desde_su_reunion_y_desde_la_que_la_arrastro(self):
        self.poblar_detalle()
        lunes = self.detalle("lunes")
        self.assertEqual(len(lunes["acciones"]), 1)
        self.assertEqual(lunes["arrastres"], [])
        # El estado es el de hoy, no el del dia de la reunion: lo dice
        # `ultima_uid`, y de ahi sale el aviso de la interfaz.
        self.assertEqual(lunes["acciones"][0]["estado"], "en_progreso")
        self.assertEqual(lunes["acciones"][0]["ultima_uid"], "martes")

        martes = self.detalle("martes")
        self.assertEqual(martes["acciones"], [])
        self.assertEqual(len(martes["arrastres"]), 1)
        self.assertEqual(martes["arrastres"][0]["origen_uid"], "lunes")
        # El comentario no tiene tabla (D10): se recupera de datos_json.
        self.assertEqual(martes["arrastres"][0]["comentario"], "falta revisarlo")

    def test_secciones_propias_del_tipo(self):
        """Lo unico que sale de datos_json y no de una tabla."""
        self.poblar_detalle()
        secciones = {s["titulo"]: s["puntos"] for s in self.detalle("martes")["secciones"]}
        self.assertEqual(secciones["Que fue bien"], ["el despliegue"])
        self.assertEqual(secciones["Acciones de mejora acordadas"], ["mas tests"])
        # Las vacias no se envian: la interfaz no pinta secciones en blanco.
        self.assertNotIn("Que no fue bien", secciones)
        # Un daily no tiene seccion propia.
        self.assertEqual(self.detalle("lunes")["secciones"], [])

    def test_segmentos_con_hablante_tiempos_y_paginacion(self):
        self.poblar_detalle()
        datos = self.cliente.get("/api/reuniones/lunes/segmentos").json()
        self.assertEqual(datos["total"], 3)
        self.assertTrue(datos["con_tiempos"])
        self.assertEqual([s["texto"] for s in datos["segmentos"]], ["hola", "que tal", "bien"])
        self.assertEqual(datos["segmentos"][0]["persona"], "Javi")
        self.assertEqual(datos["segmentos"][1]["etiqueta"], "SPEAKER_01")

        pagina = self.cliente.get(
            "/api/reuniones/lunes/segmentos?limite=1&desplazamiento=2"
        ).json()
        self.assertEqual(pagina["total"], 3, "el total es el de la reunion, no el de la pagina")
        self.assertEqual([s["idx"] for s in pagina["segmentos"]], [2])

    def test_segmentos_sin_marcas_de_tiempo(self):
        """D9: sin `.srt` los segmentos entran sin tiempos, y hay que decirlo."""
        conn = memoria.conectar(self.db)
        meeting_id = memoria.crear_reunion(
            conn, fecha="2026-09-07", transcript_path="/datos/sin_srt.txt"
        )
        memoria.insertar_segmentos(conn, meeting_id, [{"texto": "sin tiempos"}])
        conn.commit()
        conn.close()
        datos = self.cliente.get("/api/reuniones/sin_srt/segmentos").json()
        self.assertFalse(datos["con_tiempos"])
        self.assertIsNone(datos["segmentos"][0]["inicio"])
        self.assertEqual(self.cliente.get("/api/reuniones/sin_srt/srt").status_code, 409)

    def test_srt_reconstruido_desde_la_base(self):
        self.poblar_detalle()
        respuesta = self.cliente.get("/api/reuniones/lunes/srt")
        self.assertEqual(respuesta.status_code, 200)
        self.assertIn("lunes.srt", respuesta.headers["content-disposition"])
        texto = respuesta.text
        self.assertIn("00:00:00,000 --> 00:00:01,500", texto)
        self.assertIn("[Javi] hola", texto)
        # Sin nombre se conserva la etiqueta cruda en vez de dejarlo anonimo.
        self.assertIn("[SPEAKER_01] que tal", texto)

    def test_markdown_reconstruido_desde_la_base(self):
        self.poblar_detalle()
        respuesta = self.cliente.get("/api/reuniones/martes/markdown")
        self.assertEqual(respuesta.status_code, 200)
        self.assertIn("markdown", respuesta.headers["content-type"])
        self.assertIn("martes.md", respuesta.headers["content-disposition"])
        texto = respuesta.text
        self.assertIn("# Retro", texto)
        self.assertIn("## Que fue bien", texto)
        self.assertIn("falta revisarlo", texto)

    def test_sin_datos_json_no_hay_markdown_que_reconstruir(self):
        self.poblar()  # las reuniones de `poblar` no llevan datos_json
        uid = self.cliente.get("/api/reuniones").json()["reuniones"][0]["uid"]
        self.assertFalse(self.cliente.get(f"/api/reuniones/{uid}").json()["tiene_markdown"])
        respuesta = self.cliente.get(f"/api/reuniones/{uid}/markdown")
        self.assertEqual(respuesta.status_code, 404)
        self.assertIn("datos_json", respuesta.json()["detail"])

    def test_uid_desconocido_da_404_en_todas_las_subrutas(self):
        self.poblar_detalle()
        for sufijo in ("", "/segmentos", "/markdown", "/srt"):
            with self.subTest(sufijo=sufijo):
                respuesta = self.cliente.get(f"/api/reuniones/inventado{sufijo}")
                self.assertEqual(respuesta.status_code, 404)
                self.assertIn("inventado", respuesta.json()["detail"])

    def test_el_detalle_sobrevive_al_reproceso(self):
        """La URL de la vista de reunion cuelga del uid, que no cambia (D1)."""
        self.poblar_detalle()
        conn = memoria.conectar(self.db)
        memoria.crear_reunion(
            conn,
            fecha="2026-09-07",
            titulo="Daily del lunes (corregida)",
            transcript_path="/datos/lunes.txt",
        )
        conn.commit()
        conn.close()
        self.assertEqual(self.detalle("lunes")["titulo"], "Daily del lunes (corregida)")


class TestFrontEstatico(BaseAPI):
    def test_sirve_la_pagina_y_sus_recursos(self):
        for ruta, tipo in [
            ("/", "text/html"),
            ("/reunion.html", "text/html"),
            ("/js/app.js", "javascript"),
            ("/js/api.js", "javascript"),
            ("/js/svg.js", "javascript"),
            ("/js/formato.js", "javascript"),
            ("/js/vistas/timeline.js", "javascript"),
            ("/js/vistas/metricas.js", "javascript"),
            ("/js/vistas/listado.js", "javascript"),
            ("/js/reunion.js", "javascript"),
            ("/js/enlaces.js", "javascript"),
            ("/js/vistas/reunion.js", "javascript"),
            ("/js/vistas/transcripcion.js", "javascript"),
            ("/js/vistas/salud.js", "javascript"),
            ("/css/estilo.css", "text/css"),
            ("/css/tokens.css", "text/css"),
        ]:
            with self.subTest(ruta=ruta):
                respuesta = self.cliente.get(ruta)
                self.assertEqual(respuesta.status_code, 200)
                self.assertIn(tipo, respuesta.headers["content-type"])

    def test_favicon_no_lo_eclipsa_el_montaje_estatico(self):
        self.assertEqual(self.cliente.get("/favicon.ico").status_code, 200)


if __name__ == "__main__":
    unittest.main(verbosity=2)
