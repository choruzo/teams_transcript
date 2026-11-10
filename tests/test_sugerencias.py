"""Pruebas de las sugerencias de duplicados (Fase 8).

El modelo de embeddings nunca se llama: se inyecta un embebedor fijo que
devuelve vectores conocidos, de modo que la similitud coseno es una decision
del test y no una medida de la red. Es lo que permite correrlas en el servidor
offline y comprobar los umbrales con precision.
"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import memoria  # noqa: E402
import rag  # noqa: E402
import report_teams  # noqa: E402


class EmbebedorFijo:
    """Un vector por texto, escrito a mano en cada prueba."""

    def __init__(self, vectores: dict):
        self.vectores = vectores
        self.llamadas = 0
        self.textos: list[str] = []

    def __call__(self, textos: list[str]) -> list[list[float]]:
        self.llamadas += 1
        self.textos.extend(textos)
        return [self.vectores[texto] for texto in textos]


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.db = self.dir / "meetings.db"
        self.indice = self.dir / "indice.db"

    def historico(self):
        conn = memoria.conectar(self.db)
        self.addCleanup(conn.close)
        return conn

    def idx(self):
        conn = rag.conectar(self.indice)
        self.addCleanup(conn.close)
        return conn

    def reunion(self, conn, nombre, fecha="2026-09-01"):
        meeting_id = memoria.crear_reunion(
            conn,
            fecha=fecha,
            tipo="daily",
            titulo=f"Daily del {fecha}",
            transcript_path=f"/datos/{nombre}.txt",
        )
        conn.commit()
        return meeting_id

    def accion(self, conn, meeting_id, descripcion, persona="Ana", estado="abierta"):
        memoria.insertar_acciones(
            conn,
            meeting_id,
            [{"descripcion": descripcion, "persona": persona, "estado": estado}],
        )
        conn.commit()
        return conn.execute(
            "SELECT uid FROM actions WHERE descripcion_llm = ? "
            "ORDER BY id DESC LIMIT 1",
            (descripcion,),
        ).fetchone()["uid"]

    def dos_acciones(self, conn, meeting_id, descripcion, personas):
        memoria.insertar_acciones(
            conn,
            meeting_id,
            [
                {"descripcion": descripcion, "persona": persona, "estado": "abierta"}
                for persona in personas
            ],
        )
        conn.commit()
        filas = conn.execute(
            "SELECT uid FROM actions WHERE descripcion_llm = ? ORDER BY id",
            (descripcion,),
        ).fetchall()
        return filas[0]["uid"], filas[1]["uid"]

    def indexar(self, conn, conn_idx, embebedor):
        return rag.indexar_acciones(
            conn_idx, conn, embebedor=embebedor, modelo="falso"
        )


# --------------------------------------------------------------------------
# Almacen de rechazos (memoria.py, esquema 5)
# --------------------------------------------------------------------------


class TestRechazos(Base):
    def test_el_esquema_5_crea_la_tabla_y_sobrevive_al_reinicio(self):
        conn = self.historico()
        self.assertEqual(
            conn.execute("PRAGMA user_version").fetchone()[0],
            memoria.ESQUEMA_VERSION,
        )
        meeting_id = self.reunion(conn, "lunes")
        a = self.accion(conn, meeting_id, "Cerrar el certificado")
        b = self.accion(conn, meeting_id, "Preparar la demo", persona="Pablo")
        memoria.rechazar_sugerencia(conn, a, b)
        conn.close()

        otra = self.historico()
        self.assertEqual(memoria.sugerencias_rechazadas(otra), {(a, b)})

    def test_el_rechazo_es_simetrico_e_idempotente(self):
        conn = self.historico()
        meeting_id = self.reunion(conn, "lunes")
        a = self.accion(conn, meeting_id, "Cerrar el certificado")
        b = self.accion(conn, meeting_id, "Preparar la demo", persona="Pablo")

        memoria.rechazar_sugerencia(conn, b, a)
        memoria.rechazar_sugerencia(conn, a, b)

        filas = conn.execute("SELECT * FROM sugerencias_rechazadas").fetchall()
        self.assertEqual(len(filas), 1)
        self.assertEqual((filas[0]["action_a_uid"], filas[0]["action_b_uid"]), (a, b))

    def test_un_uid_inexistente_se_rechaza(self):
        conn = self.historico()
        meeting_id = self.reunion(conn, "lunes")
        a = self.accion(conn, meeting_id, "Cerrar el certificado")
        with self.assertRaises(memoria.AccionNoEncontrada):
            memoria.rechazar_sugerencia(conn, a, "no-existe")

    def test_una_accion_consigo_misma_no_se_rechaza(self):
        conn = self.historico()
        meeting_id = self.reunion(conn, "lunes")
        a = self.accion(conn, meeting_id, "Cerrar el certificado")
        with self.assertRaises(memoria.CorreccionInvalida):
            memoria.rechazar_sugerencia(conn, a, a)


class TestConsultas(Base):
    def setUp(self):
        super().setUp()
        self.conn = self.historico()
        self.lunes = self.reunion(self.conn, "lunes")
        self.jueves = self.reunion(self.conn, "jueves", fecha="2026-09-02")

    def test_sugerir_solo_mira_las_abiertas_y_vigentes(self):
        abierta = self.accion(self.conn, self.lunes, "Cerrar el certificado")
        otra_abierta = self.accion(
            self.conn, self.lunes, "Preparar la demo", persona="Pablo"
        )
        cerrada = self.accion(
            self.conn, self.jueves, "Repasar el tablero", estado="completada"
        )
        descartada = self.accion(self.conn, self.jueves, "Limpiar el backlog")
        memoria.descartar_accion(self.conn, descartada)

        uids = {fila["uid"] for fila in memoria.acciones_para_sugerir(self.conn)}
        self.assertEqual(uids, {abierta, otra_abierta})
        self.assertNotIn(cerrada, uids)

    def test_el_indice_incluye_cerradas_y_absorbidas_para_calibrar(self):
        principal = self.accion(self.conn, self.lunes, "Cerrar el certificado")
        duplicada = self.accion(
            self.conn, self.jueves, "Cerrar certificado", persona="Pablo"
        )
        cerrada = self.accion(
            self.conn, self.jueves, "Repasar el tablero", estado="completada"
        )
        memoria.fusionar_acciones(self.conn, principal, duplicada)

        uids = {fila["uid"] for fila in memoria.acciones_para_indice(self.conn)}
        self.assertTrue({principal, duplicada, cerrada} <= uids)
        self.assertEqual(
            {fila["uid"] for fila in memoria.acciones_para_sugerir(self.conn)},
            {principal},
        )

        referencia = memoria.fusiones_de_referencia(self.conn)
        self.assertEqual(
            [(f["principal_uid"], f["duplicada_uid"]) for f in referencia],
            [(principal, duplicada)],
        )


# --------------------------------------------------------------------------
# Indexado de vectores (rag.py)
# --------------------------------------------------------------------------


class TestIndexarAcciones(Base):
    def setUp(self):
        super().setUp()
        self.conn = self.historico()
        self.lunes = self.reunion(self.conn, "lunes")
        self.jueves = self.reunion(self.conn, "jueves", fecha="2026-09-02")
        self.a = self.accion(self.conn, self.lunes, "Cerrar el certificado de eqsim")
        self.b = self.accion(
            self.conn, self.jueves, "Cerrar certificado de eqsim", persona="Pablo"
        )
        self.embebedor = EmbebedorFijo(
            {
                "Cerrar el certificado de eqsim": [1.0, 0.0, 0.0],
                "Cerrar certificado de eqsim": [1.0, 0.0, 0.0],
            }
        )

    def test_guarda_el_vector_y_no_gasta_llamadas_si_no_cambia_nada(self):
        conn_idx = self.idx()
        resumen = self.indexar(self.conn, conn_idx, self.embebedor)
        self.assertEqual(resumen["acciones"], 2)
        self.assertEqual(resumen["nuevas"], 2)
        self.assertEqual(len(rag.cargar_vectores(conn_idx)), 2)

        llamadas = self.embebedor.llamadas
        resumen = self.indexar(self.conn, conn_idx, self.embebedor)
        self.assertEqual(resumen["al dia"], 2)
        self.assertEqual(resumen["nuevas"], 0)
        self.assertEqual(self.embebedor.llamadas, llamadas)

    def test_corregir_la_descripcion_reindexa_solo_esa(self):
        conn_idx = self.idx()
        self.indexar(self.conn, conn_idx, self.embebedor)
        self.embebedor.vectores["Cerrar el certificado de eqsim (v2)"] = [0.0, 1.0, 0.0]

        memoria.corregir_accion(
            self.conn, self.a, descripcion="Cerrar el certificado de eqsim (v2)"
        )
        resumen = self.indexar(self.conn, conn_idx, self.embebedor)

        self.assertEqual(resumen["actualizadas"], 1)
        self.assertEqual(resumen["al dia"], 1)
        texto = conn_idx.execute(
            "SELECT texto FROM acciones_indexadas WHERE uid = ?", (self.a,)
        ).fetchone()["texto"]
        self.assertEqual(texto, "Cerrar el certificado de eqsim (v2)")

    def test_borrar_la_accion_retira_su_vector(self):
        conn_idx = self.idx()
        self.indexar(self.conn, conn_idx, self.embebedor)
        self.conn.execute("DELETE FROM actions WHERE uid = ?", (self.a,))
        self.conn.commit()

        resumen = self.indexar(self.conn, conn_idx, self.embebedor)
        self.assertEqual(resumen["retiradas"], 1)
        self.assertIsNone(
            conn_idx.execute(
                "SELECT uid FROM acciones_indexadas WHERE uid = ?", (self.a,)
            ).fetchone()
        )

    def test_cambiar_de_modelo_se_rechaza_con_instrucciones(self):
        conn_idx = self.idx()
        self.indexar(self.conn, conn_idx, self.embebedor)
        self.accion(self.conn, self.jueves, "Preparar la demo", persona="Pablo")
        self.embebedor.vectores["Preparar la demo"] = [0.0, 1.0, 0.0]
        with self.assertRaises(rag.ErrorIndice) as caso:
            rag.indexar_acciones(
                conn_idx, self.conn, embebedor=self.embebedor, modelo="otro"
            )
        self.assertIn("--reconstruir", str(caso.exception))

    def test_sin_acciones_no_rompe_y_vacia_el_indice(self):
        conn_idx = self.idx()
        self.indexar(self.conn, conn_idx, self.embebedor)
        self.conn.execute("DELETE FROM actions")
        self.conn.commit()
        resumen = self.indexar(self.conn, conn_idx, self.embebedor)
        self.assertEqual(resumen["acciones"], 0)
        self.assertEqual(resumen["retiradas"], 2)
        self.assertEqual(rag.cargar_vectores(conn_idx), {})

    def test_la_info_cuenta_las_acciones_y_su_modelo(self):
        conn_idx = self.idx()
        self.indexar(self.conn, conn_idx, self.embebedor)
        datos = rag.info(conn_idx)
        self.assertEqual(datos["acciones"], 2)
        self.assertEqual(datos["modelo_acciones"], "falso")

    def test_un_indice_anterior_a_la_fase_8_no_rompe_la_info(self):
        conn_idx = self.idx()
        conn_idx.execute("DROP TABLE acciones_indexadas")
        conn_idx.commit()
        self.assertEqual(rag.info(conn_idx)["acciones"], 0)
        self.assertEqual(rag.cargar_vectores(conn_idx), {})


# --------------------------------------------------------------------------
# Parejas y umbral
# --------------------------------------------------------------------------


class TestParejas(Base):
    def setUp(self):
        super().setUp()
        self.conn = self.historico()
        self.lunes = self.reunion(self.conn, "lunes")
        self.jueves = self.reunion(self.conn, "jueves", fecha="2026-09-02")
        self.embebedor = EmbebedorFijo(
            {
                "Cerrar el certificado de eqsim": [1.0, 0.0, 0.0],
                "Cerrar certificado de eqsim": [1.0, 0.0, 0.0],
                "Preparar la demo": [0.0, 1.0, 0.0],
                "Repasar el tablero": [0.0, 0.0, 1.0],
            }
        )

    def _indexadas(self):
        conn_idx = self.idx()
        self.indexar(self.conn, conn_idx, self.embebedor)
        return conn_idx, memoria.acciones_para_sugerir(self.conn)

    def test_propone_la_pareja_parecida(self):
        self.accion(self.conn, self.lunes, "Cerrar el certificado de eqsim")
        self.accion(
            self.conn, self.jueves, "Cerrar certificado de eqsim", persona="Pablo"
        )
        conn_idx, acciones = self._indexadas()

        parejas = rag.parejas_similares(conn_idx, acciones)
        self.assertEqual(len(parejas), 1)
        self.assertAlmostEqual(parejas[0]["similitud"], 1.0)
        self.assertIn("certificado", parejas[0]["a_descripcion"])

    def test_no_propone_lo_que_no_llega_al_umbral(self):
        self.accion(self.conn, self.lunes, "Preparar la demo")
        self.accion(self.conn, self.jueves, "Repasar el tablero", persona="Pablo")
        conn_idx, acciones = self._indexadas()
        self.assertEqual(rag.parejas_similares(conn_idx, acciones), [])

    def test_respeta_el_rechazo_humano(self):
        a = self.accion(self.conn, self.lunes, "Cerrar el certificado de eqsim")
        b = self.accion(
            self.conn, self.jueves, "Cerrar certificado de eqsim", persona="Pablo"
        )
        conn_idx, acciones = self._indexadas()
        memoria.rechazar_sugerencia(self.conn, a, b)

        self.assertEqual(
            rag.parejas_similares(
                conn_idx,
                acciones,
                rechazadas=memoria.sugerencias_rechazadas(self.conn),
            ),
            [],
        )

    def test_misma_reunion_con_distinto_responsable_no_se_propone(self):
        self.dos_acciones(
            self.conn,
            self.lunes,
            "Cerrar el certificado de eqsim",
            ["Ana", "Pablo"],
        )
        conn_idx, acciones = self._indexadas()
        self.assertEqual(rag.parejas_similares(conn_idx, acciones), [])

    def test_misma_reunion_con_el_mismo_responsable_si_se_propone(self):
        self.dos_acciones(
            self.conn,
            self.lunes,
            "Cerrar el certificado de eqsim",
            ["Ana", "Ana"],
        )
        conn_idx, acciones = self._indexadas()
        self.assertEqual(len(rag.parejas_similares(conn_idx, acciones)), 1)

    def test_una_sola_pareja_por_accion(self):
        viernes = self.reunion(self.conn, "viernes", fecha="2026-09-03")
        self.accion(self.conn, self.lunes, "Cerrar el certificado de eqsim")
        self.accion(
            self.conn, self.jueves, "Cerrar certificado de eqsim", persona="Pablo"
        )
        self.embebedor.vectores["Repasar el tablero"] = [1.0, 0.0, 0.0]
        self.accion(self.conn, viernes, "Repasar el tablero", persona="Luis")
        conn_idx, acciones = self._indexadas()

        mejores = rag.parejas_similares(conn_idx, acciones)
        self.assertEqual(len(mejores), 1)  # tres acciones, una pareja
        todas = rag.parejas_similares(conn_idx, acciones, mejores=False)
        self.assertEqual(len(todas), 3)

    def test_sin_vectores_no_hay_sugerencias(self):
        self.accion(self.conn, self.lunes, "Cerrar el certificado de eqsim")
        self.accion(
            self.conn, self.jueves, "Cerrar certificado de eqsim", persona="Pablo"
        )
        self.assertEqual(
            rag.parejas_similares(self.idx(), memoria.acciones_para_sugerir(self.conn)),
            [],
        )


class TestCalibracion(Base):
    def test_ensena_las_fusiones_de_referencia_aunque_no_lleguen(self):
        conn = self.historico()
        lunes = self.reunion(conn, "lunes")
        jueves = self.reunion(conn, "jueves", fecha="2026-09-02")
        principal = self.accion(conn, lunes, "Cerrar el certificado de eqsim")
        duplicada = self.accion(
            conn, jueves, "Cerrar certificado de eqsim", persona="Pablo"
        )
        memoria.fusionar_acciones(conn, principal, duplicada)

        embebedor = EmbebedorFijo(
            {
                "Cerrar el certificado de eqsim": [1.0, 0.0],
                "Cerrar certificado de eqsim": [0.7, 0.714142842854285],
            }
        )
        datos = rag.calibrar_acciones(
            memoria.acciones_para_indice(conn),
            memoria.fusiones_de_referencia(conn),
            embebedor=embebedor,
            umbral=0.9,
            minimo=0.5,
        )

        self.assertAlmostEqual(datos["referencias"][0]["similitud"], 0.7, places=3)
        self.assertFalse(datos["referencias"][0]["propuesta"])
        # La absorbida no sale en `parejas`: el motor nunca la propondria.
        self.assertEqual(datos["parejas"], [])

    def test_una_pareja_abierta_bajo_el_umbral_se_ensena_pero_no_se_propone(self):
        conn = self.historico()
        lunes = self.reunion(conn, "lunes")
        jueves = self.reunion(conn, "jueves", fecha="2026-09-02")
        self.accion(conn, lunes, "Cerrar el certificado de eqsim")
        self.accion(conn, jueves, "Cerrar el certificado", persona="Pablo")

        datos = rag.calibrar_acciones(
            memoria.acciones_para_indice(conn),
            [],
            embebedor=EmbebedorFijo(
                {
                    "Cerrar el certificado de eqsim": [1.0, 0.0],
                    "Cerrar el certificado": [0.7, 0.714142842854285],
                }
            ),
            umbral=0.9,
            minimo=0.5,
        )
        self.assertEqual(len(datos["parejas"]), 1)
        self.assertFalse(datos["parejas"][0]["propuesta"])
        self.assertIsNone(datos["parejas"][0]["motivo"])

    def test_la_calibracion_ensena_las_sugerencias_reales(self):
        conn = self.historico()
        lunes = self.reunion(conn, "lunes")
        martes = self.reunion(conn, "martes", fecha="2026-09-02")
        miercoles = self.reunion(conn, "miercoles", fecha="2026-09-03")
        self.accion(conn, lunes, "Cerrar el certificado", persona="Ana")
        self.accion(conn, martes, "Cerrar certificado", persona="Ana")
        self.accion(conn, miercoles, "Cerrar el certificado", persona="Pablo")

        datos = rag.calibrar_acciones(
            memoria.acciones_para_indice(conn),
            [],
            embebedor=EmbebedorFijo(
                {
                    "Cerrar el certificado": [1.0, 0.0],
                    "Cerrar certificado": [1.0, 0.0],
                }
            ),
            umbral=0.5,
            minimo=0.5,
        )
        self.assertEqual(len(datos["parejas"]), 3)
        self.assertEqual(
            sum(1 for pareja in datos["parejas"] if pareja["sugerida"]), 1
        )

    def test_sin_acciones_devuelve_listas_vacias(self):
        conn = self.historico()
        datos = rag.calibrar_acciones([], [], embebedor=EmbebedorFijo({}))
        self.assertEqual(datos["parejas"], [])
        self.assertEqual(datos["referencias"], [])
        self.assertEqual(datos["llamadas"], 0)


# --------------------------------------------------------------------------
# Informe
# --------------------------------------------------------------------------


class TestInforme(Base):
    def setUp(self):
        super().setUp()
        self.conn = self.historico()
        lunes = self.reunion(self.conn, "lunes")
        jueves = self.reunion(self.conn, "jueves", fecha="2026-09-02")
        self.accion(self.conn, lunes, "Cerrar el certificado de eqsim")
        self.accion(
            self.conn, jueves, "Cerrar certificado de eqsim", persona="Pablo"
        )
        conn_idx = self.idx()
        self.indexar(
            self.conn,
            conn_idx,
            EmbebedorFijo(
                {
                    "Cerrar el certificado de eqsim": [1.0, 0.0],
                    "Cerrar certificado de eqsim": [1.0, 0.0],
                }
            ),
        )

    def test_carga_las_sugerencias_del_indice(self):
        sugerencias, aviso = report_teams.cargar_sugerencias(
            self.conn, ruta_indice=self.indice
        )
        self.assertIsNone(aviso)
        self.assertEqual(len(sugerencias), 1)

    def test_sin_indice_avisa_y_el_informe_sigue(self):
        sugerencias, aviso = report_teams.cargar_sugerencias(
            self.conn, ruta_indice=self.dir / "no-existe.db"
        )
        self.assertEqual(sugerencias, [])
        self.assertIn("indexar_teams.py --acciones", aviso)

        datos = report_teams.recopilar(
            self.conn, sugerencias=sugerencias, aviso_sugerencias=aviso
        )
        markdown = report_teams.renderizar_markdown(datos)
        self.assertIn("Posibles duplicados", markdown)
        self.assertIn(aviso, markdown)

    def test_el_informe_avisa_de_que_son_probables(self):
        sugerencias, aviso = report_teams.cargar_sugerencias(
            self.conn, ruta_indice=self.indice
        )
        datos = report_teams.recopilar(
            self.conn, sugerencias=sugerencias, aviso_sugerencias=aviso
        )
        markdown = report_teams.renderizar_markdown(datos)
        self.assertIn("Posibles duplicados", markdown)
        self.assertIn("probables", markdown)
        self.assertIn(f"{rag.UMBRAL_SUGERENCIA:.2f}", markdown)

    def test_el_json_incluye_las_sugerencias_sin_uids_para_el_modelo(self):
        sugerencias, aviso = report_teams.cargar_sugerencias(
            self.conn, ruta_indice=self.indice
        )
        datos = report_teams.recopilar(
            self.conn, sugerencias=sugerencias, aviso_sugerencias=aviso
        )
        para_el_modelo = report_teams._para_el_modelo(datos)
        self.assertNotIn("a_uid", para_el_modelo["sugerencias"][0])
        self.assertNotIn("b_uid", para_el_modelo["sugerencias"][0])


if __name__ == "__main__":
    unittest.main()
