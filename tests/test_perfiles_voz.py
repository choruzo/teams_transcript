"""Pruebas de los perfiles de voz (Fase 3).

Toda la logica que decide *quien es quien* vive en `perfiles_voz.py` y no en
`transcribe_teams.py` justamente para poder probarla aqui: sin GPU, sin
pyannote y sin numpy. Lo que se vigila es lo que puede fallar en silencio —
un umbral que asigna a la persona equivocada, dos hablantes de la misma
reunion resueltos como la misma persona, un centroide que se contamina, o
mezclar vectores de dos modelos distintos, que no da error, solo resultados
sin sentido.
"""

import json
import math
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import perfiles_voz as pv  # noqa: E402


def _vector(*valores, dim=8):
    """Un vector de `dim` componentes con los primeros valores dados."""
    base = [0.0] * dim
    for i, valor in enumerate(valores):
        base[i] = float(valor)
    return base


ANA = _vector(1.0, 0.0)
PABLO = _vector(0.0, 1.0)


class TestSimilitud(unittest.TestCase):
    def test_vectores_iguales_dan_uno(self):
        self.assertAlmostEqual(pv.similitud(ANA, ANA), 1.0)

    def test_ortogonales_dan_cero(self):
        self.assertAlmostEqual(pv.similitud(ANA, PABLO), 0.0)

    def test_la_escala_no_importa(self):
        doble = [x * 7 for x in ANA]
        self.assertAlmostEqual(pv.similitud(ANA, doble), 1.0)

    def test_dimensiones_distintas_no_revientan(self):
        self.assertEqual(pv.similitud(ANA, [1.0, 0.0]), 0.0)

    def test_vector_nulo_no_se_parece_a_nada(self):
        self.assertEqual(pv.similitud(ANA, [0.0] * 8), 0.0)

    def test_nan_no_es_vector_valido(self):
        self.assertFalse(pv.es_vector_valido(_vector(float("nan"))))
        self.assertFalse(pv.es_vector_valido([0.0] * 8))
        self.assertFalse(pv.es_vector_valido([]))
        self.assertTrue(pv.es_vector_valido(ANA))


class TestCalibracion(unittest.TestCase):
    """El umbral no es una decision de estilo sino una medicion (2026-09-10,
    dos reuniones reales): impostores hasta 0,638 y aciertos desde 0,729."""

    def test_el_umbral_por_defecto_separa_lo_medido(self):
        self.assertGreater(pv.UMBRAL_POR_DEFECTO, 0.638)
        self.assertLess(pv.UMBRAL_POR_DEFECTO, 0.729)

    def test_hay_minimo_de_habla_para_aprender(self):
        self.assertGreaterEqual(pv.MINIMO_SEGUNDOS_MUESTRA, 10.0)


class TestClasificar(unittest.TestCase):
    def setUp(self):
        self.perfiles = pv.perfiles_vacios()
        pv.actualizar(self.perfiles, "Ana Ruiz", ANA)
        pv.actualizar(self.perfiles, "Pablo Gil", PABLO)

    def test_asigna_el_perfil_mas_parecido(self):
        salida = pv.clasificar({"SPEAKER_00": ANA}, self.perfiles, 0.6)
        self.assertEqual(salida["SPEAKER_00"]["nombre"], "Ana Ruiz")
        self.assertAlmostEqual(salida["SPEAKER_00"]["similitud"], 1.0)

    def test_por_debajo_del_umbral_queda_sin_asignar(self):
        # 45 grados de Ana: 0,707. Con umbral 0,8 no basta.
        mezcla = _vector(1.0, 1.0)
        salida = pv.clasificar({"SPEAKER_00": mezcla}, self.perfiles, 0.8)
        self.assertIsNone(salida["SPEAKER_00"]["nombre"])
        self.assertAlmostEqual(salida["SPEAKER_00"]["similitud"], math.sqrt(0.5))

    def test_la_asignacion_es_exclusiva(self):
        # Dos hablantes distintos de la misma reunion no pueden ser la misma
        # persona: se queda con el mas parecido y el otro sin asignar.
        casi_ana = _vector(1.0, 0.2)
        menos_ana = _vector(1.0, 0.6)
        salida = pv.clasificar(
            {"SPEAKER_00": menos_ana, "SPEAKER_01": casi_ana}, self.perfiles, 0.6
        )
        self.assertEqual(salida["SPEAKER_01"]["nombre"], "Ana Ruiz")
        self.assertIsNone(salida["SPEAKER_00"]["nombre"])

    def test_embedding_invalido_no_asigna_nada(self):
        salida = pv.clasificar({"SPEAKER_00": [0.0] * 8}, self.perfiles, 0.1)
        self.assertIsNone(salida["SPEAKER_00"]["nombre"])
        self.assertEqual(salida["SPEAKER_00"]["candidatos"], [])

    def test_sin_perfiles_no_asigna_pero_no_falla(self):
        salida = pv.clasificar({"SPEAKER_00": ANA}, pv.perfiles_vacios(), 0.6)
        self.assertIsNone(salida["SPEAKER_00"]["nombre"])
        self.assertEqual(salida["SPEAKER_00"]["similitud"], 0.0)


class TestActualizar(unittest.TestCase):
    def test_media_incremental(self):
        perfiles = pv.perfiles_vacios()
        pv.actualizar(perfiles, "Ana Ruiz", _vector(0.0, 1.0))
        pv.actualizar(perfiles, "Ana Ruiz", _vector(2.0, 1.0))
        perfil = perfiles["personas"]["Ana Ruiz"]
        self.assertEqual(perfil["muestras"], 2)
        self.assertAlmostEqual(perfil["centroide"][0], 1.0)
        # La tercera muestra pesa un tercio, no la mitad.
        pv.actualizar(perfiles, "Ana Ruiz", _vector(4.0, 1.0))
        self.assertAlmostEqual(perfiles["personas"]["Ana Ruiz"]["centroide"][0], 2.0)
        self.assertEqual(perfiles["personas"]["Ana Ruiz"]["muestras"], 3)

    def test_mayusculas_no_crean_un_segundo_perfil(self):
        perfiles = pv.perfiles_vacios()
        pv.actualizar(perfiles, "Ana Ruiz", ANA)
        pv.actualizar(perfiles, "ana  ruiz", ANA)
        self.assertEqual(list(perfiles["personas"]), ["Ana Ruiz"])
        self.assertEqual(perfiles["personas"]["Ana Ruiz"]["muestras"], 2)

    def test_nombres_genericos_no_crean_perfil(self):
        perfiles = pv.perfiles_vacios()
        for nombre in ("SPEAKER_03", "no identificado", "", "  "):
            self.assertFalse(pv.nombre_valido(nombre))
            with self.assertRaises(ValueError):
                pv.actualizar(perfiles, nombre, ANA)
        self.assertEqual(perfiles["personas"], {})

    def test_embedding_invalido_no_entra(self):
        perfiles = pv.perfiles_vacios()
        with self.assertRaises(ValueError):
            pv.actualizar(perfiles, "Ana Ruiz", _vector(float("nan")))

    def test_dimension_distinta_para_en_seco(self):
        perfiles = pv.perfiles_vacios()
        pv.actualizar(perfiles, "Ana Ruiz", ANA)
        with self.assertRaises(pv.PerfilesIncompatibles):
            pv.actualizar(perfiles, "Pablo Gil", _vector(1.0, dim=4))
        with self.assertRaises(pv.PerfilesIncompatibles):
            pv.comprobar_dimension(perfiles, 4)
        pv.comprobar_dimension(perfiles, 8)  # no lanza


class TestOlvidarYRenombrar(unittest.TestCase):
    def setUp(self):
        self.perfiles = pv.perfiles_vacios()
        pv.actualizar(self.perfiles, "Ana Ruiz", ANA)

    def test_olvidar_borra_el_dato_biometrico(self):
        self.assertTrue(pv.olvidar(self.perfiles, "ana ruiz"))
        self.assertEqual(self.perfiles["personas"], {})
        self.assertFalse(pv.olvidar(self.perfiles, "Ana Ruiz"))

    def test_renombrar_conserva_centroide_y_muestras(self):
        pv.actualizar(self.perfiles, "Ana Ruiz", ANA)
        self.assertTrue(pv.renombrar(self.perfiles, "Ana Ruiz", "Ana Ruiz Perez"))
        perfil = self.perfiles["personas"]["Ana Ruiz Perez"]
        self.assertEqual(perfil["muestras"], 2)
        self.assertEqual(perfil["centroide"], ANA)

    def test_renombrar_no_pisa_otro_perfil(self):
        pv.actualizar(self.perfiles, "Pablo Gil", PABLO)
        with self.assertRaises(ValueError):
            pv.renombrar(self.perfiles, "Ana Ruiz", "pablo gil")


class TestPersistencia(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ruta = Path(self.tmp.name) / "perfiles_voz.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_ida_y_vuelta(self):
        perfiles = pv.perfiles_vacios()
        pv.actualizar(perfiles, "Ana Ruiz", ANA, modelo="pyannote/x")
        pv.guardar(perfiles, self.ruta)
        recuperado = pv.cargar(self.ruta)
        self.assertEqual(recuperado["personas"]["Ana Ruiz"]["centroide"], ANA)
        self.assertEqual(recuperado["modelo"], "pyannote/x")
        self.assertEqual(recuperado["dimension"], 8)

    def test_fichero_inexistente_no_es_un_error(self):
        vacio = pv.cargar(self.ruta)
        self.assertEqual(vacio["personas"], {})

    def test_version_distinta_avisa_en_vez_de_mezclar(self):
        self.ruta.write_text(
            json.dumps({"version": 99, "personas": {}}), encoding="utf-8"
        )
        with self.assertRaises(pv.PerfilesIncompatibles):
            pv.cargar(self.ruta)

    def test_json_roto_avisa(self):
        self.ruta.write_text("{no es json", encoding="utf-8")
        with self.assertRaises(pv.PerfilesIncompatibles):
            pv.cargar(self.ruta)

    def test_entradas_corruptas_se_descartan_sin_tumbar_el_fichero(self):
        self.ruta.write_text(
            json.dumps(
                {
                    "version": pv.VERSION_PERFILES,
                    "personas": {
                        "Ana Ruiz": {"centroide": ANA, "muestras": 2},
                        "Rota": {"centroide": "no soy un vector"},
                        "Vacia": {},
                        "NoDict": 3,
                    },
                }
            ),
            encoding="utf-8",
        )
        perfiles = pv.cargar(self.ruta)
        self.assertEqual(list(perfiles["personas"]), ["Ana Ruiz"])

    def test_guardar_es_atomico(self):
        pv.guardar(pv.perfiles_vacios(), self.ruta)
        self.assertTrue(self.ruta.exists())
        self.assertFalse(self.ruta.with_suffix(".json.tmp").exists())

    def test_ruta_por_variable_de_entorno(self):
        import os

        anterior = os.environ.get("TEAMS_PERFILES_VOZ")
        os.environ["TEAMS_PERFILES_VOZ"] = str(self.ruta)
        try:
            self.assertEqual(pv.ruta_perfiles(None), self.ruta)
            # El argumento explicito gana a la variable.
            self.assertEqual(pv.ruta_perfiles("otro.json"), Path("otro.json"))
        finally:
            if anterior is None:
                del os.environ["TEAMS_PERFILES_VOZ"]
            else:
                os.environ["TEAMS_PERFILES_VOZ"] = anterior


class TestTurnosYMuestras(unittest.TestCase):
    TURNOS = [
        (0.0, 2.0, "SPEAKER_00"),
        (2.0, 12.0, "SPEAKER_01"),
        (12.0, 15.0, "SPEAKER_00"),
    ]
    SEGMENTOS = [
        {"start": 0.0, "end": 2.0, "text": " Hola. "},
        {"start": 2.0, "end": 8.0, "text": "Ayer termine el despliegue del piloto."},
        {"start": 8.0, "end": 12.0, "text": "Hoy sigo con las pruebas."},
        {"start": 12.0, "end": 15.0, "text": "Perfecto, gracias."},
    ]

    def test_renombrar_turnos_conserva_los_no_reconocidos(self):
        salida = pv.renombrar_turnos(self.TURNOS, {"SPEAKER_01": "Ana Ruiz"})
        self.assertEqual([t[2] for t in salida], ["SPEAKER_00", "Ana Ruiz", "SPEAKER_00"])

    def test_renombrar_turnos_ignora_un_nombre_nulo(self):
        salida = pv.renombrar_turnos(self.TURNOS, {"SPEAKER_00": None})
        self.assertEqual(salida, self.TURNOS)

    def test_estadisticas_suman_tiempo_de_habla(self):
        stats = pv.estadisticas_hablantes(self.TURNOS, self.SEGMENTOS)
        self.assertAlmostEqual(stats["SPEAKER_00"]["segundos"], 5.0)
        self.assertEqual(stats["SPEAKER_00"]["turnos"], 2)
        self.assertAlmostEqual(stats["SPEAKER_01"]["segundos"], 10.0)

    def test_la_muestra_sale_del_turno_mas_largo(self):
        # El primer turno de SPEAKER_00 es un "Hola" que no distingue a nadie;
        # la muestra debe venir de su turno mas largo.
        stats = pv.estadisticas_hablantes(self.TURNOS, self.SEGMENTOS)
        self.assertIn("despliegue", stats["SPEAKER_01"]["muestra"])
        self.assertIn("gracias", stats["SPEAKER_00"]["muestra"].lower())

    def test_estadisticas_sin_segmentos_no_fallan(self):
        stats = pv.estadisticas_hablantes(self.TURNOS)
        self.assertEqual(stats["SPEAKER_01"]["muestra"], "")

    def test_informe_menciona_umbral_y_candidatos(self):
        perfiles = pv.perfiles_vacios()
        pv.actualizar(perfiles, "Ana Ruiz", ANA)
        clasificacion = pv.clasificar({"SPEAKER_00": ANA}, perfiles, 0.6)
        stats = pv.estadisticas_hablantes(self.TURNOS, self.SEGMENTOS)
        texto = pv.informe_clasificacion(clasificacion, stats, 0.6)
        self.assertIn("0.60", texto)
        self.assertIn("Ana Ruiz", texto)
        self.assertIn("SPEAKER_00", texto)


class TestCLI(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ruta = Path(self.tmp.name) / "perfiles_voz.json"
        perfiles = pv.perfiles_vacios()
        pv.actualizar(perfiles, "Ana Ruiz", ANA)
        pv.guardar(perfiles, self.ruta)

    def tearDown(self):
        self.tmp.cleanup()

    def _ejecutar(self, *argv):
        import contextlib
        import io

        salida = io.StringIO()
        with contextlib.redirect_stdout(salida), contextlib.redirect_stderr(salida):
            codigo = pv.main(["--perfiles", str(self.ruta), *argv])
        return codigo, salida.getvalue()

    def test_listar(self):
        codigo, texto = self._ejecutar("--listar")
        self.assertEqual(codigo, 0)
        self.assertIn("Ana Ruiz", texto)

    def test_olvidar_persiste(self):
        codigo, _ = self._ejecutar("--olvidar", "Ana Ruiz")
        self.assertEqual(codigo, 0)
        self.assertEqual(pv.cargar(self.ruta)["personas"], {})

    def test_olvidar_a_quien_no_esta_devuelve_error(self):
        codigo, _ = self._ejecutar("--olvidar", "Nadie")
        self.assertEqual(codigo, 1)

    def test_renombrar_persiste(self):
        codigo, _ = self._ejecutar("--renombrar", "Ana Ruiz", "Ana Ruiz Perez")
        self.assertEqual(codigo, 0)
        self.assertIn("Ana Ruiz Perez", pv.cargar(self.ruta)["personas"])


if __name__ == "__main__":
    unittest.main()
