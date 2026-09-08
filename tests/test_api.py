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


class TestFrontEstatico(BaseAPI):
    def test_sirve_la_pagina_y_sus_recursos(self):
        for ruta, tipo in [
            ("/", "text/html"),
            ("/js/app.js", "javascript"),
            ("/js/api.js", "javascript"),
            ("/css/estilo.css", "text/css"),
        ]:
            with self.subTest(ruta=ruta):
                respuesta = self.cliente.get(ruta)
                self.assertEqual(respuesta.status_code, 200)
                self.assertIn(tipo, respuesta.headers["content-type"])

    def test_favicon_no_lo_eclipsa_el_montaje_estatico(self):
        self.assertEqual(self.cliente.get("/favicon.ico").status_code, 200)


if __name__ == "__main__":
    unittest.main(verbosity=2)
