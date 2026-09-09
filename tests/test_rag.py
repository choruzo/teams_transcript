"""Pruebas del indice semantico (`rag.py`).

Nunca se llama al modelo de embeddings: se inyecta `EmbebedorFalso`, que es
determinista y calcula vectores a partir de las palabras del texto, de modo
que la similitud coseno significa algo sin depender de la red. Es lo que
permite que estas pruebas corran en el servidor offline.

Las que necesitan `sqlite-vec` se saltan solas, igual que las de la API se
saltan sin FastAPI: el venv de Whisper no tiene por que tener la extension.
"""

import hashlib
import math
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import memoria  # noqa: E402
import rag  # noqa: E402

HAY_VEC = rag.hay_extension()

DIMENSIONES = 24


def _vector(texto: str) -> list[float]:
    """Bolsa de palabras proyectada a un espacio pequenio, normalizada.

    Dos textos que comparten vocabulario acaban cerca; uno que no comparte
    nada, lejos. No es un modelo de lenguaje, pero para comprobar que la
    fusion ordena bien es exactamente lo que hace falta.
    """
    vector = [0.0] * DIMENSIONES
    for palabra in memoria.sin_acentos(texto).split():
        posicion = int(hashlib.sha1(palabra.encode()).hexdigest(), 16) % DIMENSIONES
        vector[posicion] += 1.0
    norma = math.sqrt(sum(v * v for v in vector)) or 1.0
    return [v / norma for v in vector]


class EmbebedorFalso:
    def __init__(self, dimensiones: int = DIMENSIONES):
        self.dimensiones = dimensiones
        self.llamadas = 0
        self.textos: list[str] = []

    def __call__(self, textos: list[str]) -> list[list[float]]:
        self.llamadas += 1
        self.textos.extend(textos)
        return [_vector(t)[: self.dimensiones] for t in textos]


class BaseIndice(unittest.TestCase):
    """Un historico y un indice, ambos temporales."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.db = self.dir / "meetings.db"
        self.indice = self.dir / "indice.db"
        self.embebedor = EmbebedorFalso()

    def historico(self):
        conn = memoria.conectar(self.db)
        self.addCleanup(conn.close)
        return conn

    def idx(self):
        conn = rag.conectar(self.indice)
        self.addCleanup(conn.close)
        return conn

    def reunion(self, conn, *, fecha="2026-09-01", tipo="daily", textos=(), nombre=None):
        nombre = nombre or f"{fecha.replace('-', '')}_090000_mixed"
        meeting_id = memoria.crear_reunion(
            conn,
            fecha=fecha,
            tipo=tipo,
            titulo=f"{tipo} del {fecha}",
            transcript_path=f"/datos/{nombre}.txt",
        )
        segmentos = [
            {"texto": texto, "inicio": float(i * 5), "fin": float(i * 5 + 5),
             "etiqueta": hablante}
            for i, (hablante, texto) in enumerate(textos)
        ]
        # El mismo camino que sigue el pipeline: primero el mapeo
        # etiqueta -> persona y luego los segmentos, para que `persona_id`
        # quede resuelto y el troceador vea los nombres.
        mapa = memoria.registrar_hablantes(
            conn,
            meeting_id,
            [{"etiqueta": quien, "nombre": quien} for quien, _ in textos],
        )
        memoria.insertar_segmentos(conn, meeting_id, segmentos, mapa)
        conn.commit()
        return meeting_id

    def indexar(self, conn_hist, conn_idx, **kwargs):
        return rag.indexar(
            conn_idx, conn_hist, embebedor=self.embebedor, modelo="falso", **kwargs
        )


CHARLA = [
    ("Javi", "Ayer estuve con la imputacion de horas en Planner y quedo cerrada."),
    ("Javi", "Hoy sigo con el informe de Coverity, que tiene unos cuantos avisos."),
    ("Pablo", "Yo estoy bloqueado con el certificado del entorno de eqsim."),
    ("Pablo", "Sin ese certificado no puedo lanzar la non-regression completa."),
    ("Ana", "Vale, lo miro esta tarde y te digo algo."),
]


# --------------------------------------------------------------------------
# Troceado
# --------------------------------------------------------------------------


class TestTroceado(BaseIndice):
    def _segmentos(self, textos):
        return [
            {"idx": i, "texto": t, "inicio": float(i), "fin": float(i + 1),
             "etiqueta": None, "persona": quien}
            for i, (quien, t) in enumerate(textos)
        ]

    def test_agrupa_lineas_cortas_en_un_solo_fragmento(self):
        # Cinco lineas de charla no llegan al objetivo: son un fragmento.
        fragmentos = rag.trocear(
            self._segmentos(CHARLA), uid="u1", fecha="2026-09-01", tipo="daily"
        )
        self.assertEqual(len(fragmentos), 1)
        self.assertEqual(fragmentos[0]["idx_inicio"], 0)
        self.assertEqual(fragmentos[0]["idx_fin"], 4)
        self.assertEqual(fragmentos[0]["personas"], "Javi | Pablo | Ana")

    def test_corta_al_pasar_del_maximo(self):
        largo = [("Javi", "palabra " * 100) for _ in range(6)]
        fragmentos = rag.trocear(
            self._segmentos(largo), uid="u1", fecha="2026-09-01", tipo="daily"
        )
        self.assertGreater(len(fragmentos), 1)
        for fragmento in fragmentos:
            self.assertLessEqual(len(fragmento["texto"]), rag.MAXIMO_CARACTERES + 900)

    def test_los_fragmentos_se_solapan_pero_no_repiten_el_inicio(self):
        largo = [("Javi", "palabra " * 30) for _ in range(12)]
        fragmentos = rag.trocear(
            self._segmentos(largo), uid="u1", fecha="2026-09-01", tipo="daily"
        )
        inicios = [f["idx_inicio"] for f in fragmentos]
        # Sin esto, el indice UNIQUE(uid, idx_inicio) reventaria al insertar.
        self.assertEqual(len(inicios), len(set(inicios)))
        self.assertEqual(inicios, sorted(inicios))

    def test_un_unico_segmento_gigante_no_bucle(self):
        gigante = [("Javi", "x" * 5000)]
        fragmentos = rag.trocear(
            self._segmentos(gigante), uid="u1", fecha="2026-09-01", tipo="daily"
        )
        self.assertEqual(len(fragmentos), 1)

    def test_los_segmentos_vacios_se_ignoran(self):
        con_huecos = [("Javi", "hola"), ("Javi", "   "), ("Ana", "adios")]
        fragmentos = rag.trocear(
            self._segmentos(con_huecos), uid="u1", fecha="2026-09-01", tipo="daily"
        )
        self.assertEqual(len(fragmentos), 1)
        self.assertNotIn("   ", fragmentos[0]["texto"])

    def test_el_texto_a_embeber_lleva_cabecera_y_el_guardado_no(self):
        fragmentos = rag.trocear(
            self._segmentos(CHARLA), uid="u1", fecha="2026-09-01", tipo="daily"
        )
        embebido = rag.texto_a_embeber(fragmentos[0])
        self.assertIn("Daily del 2026-09-01", embebido)
        self.assertIn("Javi", embebido)
        self.assertNotIn("Daily del", fragmentos[0]["texto"])


# --------------------------------------------------------------------------
# Indexacion
# --------------------------------------------------------------------------


class TestIndexacion(BaseIndice):
    def test_indexa_una_reunion_y_cuenta_los_fragmentos(self):
        conn = self.historico()
        self.reunion(conn, textos=CHARLA)
        resumen = self.indexar(conn, self.idx())
        self.assertEqual(resumen["indexadas"], 1)
        self.assertEqual(resumen["chunks"], 1)

    def test_volver_a_indexar_sin_cambios_no_gasta_ni_una_llamada(self):
        conn = self.historico()
        self.reunion(conn, textos=CHARLA)
        conn_idx = self.idx()
        self.indexar(conn, conn_idx)
        llamadas = self.embebedor.llamadas
        resumen = self.indexar(conn, conn_idx)
        self.assertEqual(resumen["al dia"], 1)
        self.assertEqual(resumen["indexadas"], 0)
        self.assertEqual(self.embebedor.llamadas, llamadas)

    def test_reprocesar_una_reunion_la_reindexa(self):
        conn = self.historico()
        self.reunion(conn, textos=CHARLA)
        conn_idx = self.idx()
        self.indexar(conn, conn_idx)

        # Reprocesar: misma transcripcion, otro contenido. Conserva el uid.
        self.reunion(conn, textos=CHARLA + [("Ana", "Anadimos una linea nueva.")])
        resumen = self.indexar(conn, conn_idx)
        self.assertEqual(resumen["reindexadas"], 1)
        fragmentos = conn_idx.execute("SELECT texto FROM chunks").fetchall()
        self.assertIn("linea nueva", "\n".join(f["texto"] for f in fragmentos))

    def test_una_reunion_sin_segmentos_se_omite_sin_romper(self):
        conn = self.historico()
        memoria.crear_reunion(
            conn, fecha="2026-09-01", transcript_path="/datos/vacia.txt"
        )
        conn.commit()
        resumen = self.indexar(conn, self.idx())
        self.assertEqual(resumen["omitidas"], 1)
        self.assertEqual(resumen["chunks"], 0)

    def test_cambiar_de_modelo_se_rechaza_con_instrucciones(self):
        conn = self.historico()
        self.reunion(conn, textos=CHARLA)
        conn_idx = self.idx()
        self.indexar(conn, conn_idx)
        self.reunion(conn, fecha="2026-09-02", textos=CHARLA)
        with self.assertRaises(rag.ErrorIndice) as caso:
            rag.indexar(conn_idx, conn, embebedor=self.embebedor, modelo="otro-modelo")
        self.assertIn("--reconstruir", str(caso.exception))

    def test_reconstruir_acepta_el_modelo_nuevo(self):
        conn = self.historico()
        self.reunion(conn, textos=CHARLA)
        conn_idx = self.idx()
        self.indexar(conn, conn_idx)
        resumen = rag.indexar(
            conn_idx, conn, embebedor=self.embebedor, modelo="otro-modelo",
            reconstruir=True,
        )
        self.assertEqual(resumen["indexadas"], 1)
        self.assertEqual(rag.leer_meta(conn_idx)["modelo"], "otro-modelo")

    def test_una_reunion_borrada_del_historico_se_retira_del_indice(self):
        conn = self.historico()
        self.reunion(conn, textos=CHARLA)
        conn_idx = self.idx()
        self.indexar(conn, conn_idx)
        conn.execute("DELETE FROM meetings")
        conn.commit()
        self.assertEqual(rag.limpiar_huerfanas(conn_idx, conn), 1)
        self.assertEqual(conn_idx.execute("SELECT count(*) FROM chunks").fetchone()[0], 0)

    def test_un_uid_inexistente_se_dice_en_vez_de_no_hacer_nada(self):
        conn = self.historico()
        with self.assertRaises(rag.ErrorIndice) as caso:
            self.indexar(conn, self.idx(), uids=["no-existe"])
        self.assertIn("no-existe", str(caso.exception))

    def test_info_describe_el_estado(self):
        conn = self.historico()
        self.reunion(conn, textos=CHARLA)
        conn_idx = self.idx()
        self.indexar(conn, conn_idx)
        datos = rag.info(conn_idx)
        self.assertEqual(datos["reuniones"], 1)
        self.assertEqual(datos["modelo"], "falso")
        self.assertEqual(datos["dimensiones"], DIMENSIONES)
        self.assertEqual(datos["densa"], HAY_VEC)


# --------------------------------------------------------------------------
# Busqueda
# --------------------------------------------------------------------------


class TestBusqueda(BaseIndice):
    def poblar(self):
        conn = self.historico()
        self.reunion(conn, fecha="2026-09-01", textos=CHARLA, nombre="lunes")
        self.reunion(
            conn,
            fecha="2026-09-02",
            tipo="retro",
            nombre="martes",
            textos=[
                ("Ana", "En la retro hablamos del despliegue y de los tiempos de espera."),
                ("Javi", "El certificado sigue sin llegar, es el mismo problema."),
            ],
        )
        conn_idx = self.idx()
        self.indexar(conn, conn_idx)
        return conn, conn_idx

    def test_encuentra_por_palabra_literal(self):
        conn, conn_idx = self.poblar()
        resultado = rag.buscar(
            conn, conn_idx, texto="Coverity", embebedor=self.embebedor
        )
        self.assertTrue(resultado["fragmentos"])
        self.assertIn("Coverity", resultado["fragmentos"][0]["texto"])

    def test_los_acentos_dan_igual(self):
        conn, conn_idx = self.poblar()
        resultado = rag.buscar(conn, conn_idx, texto="imputación")
        self.assertTrue(resultado["fragmentos"])

    def test_el_filtro_de_fecha_acota(self):
        conn, conn_idx = self.poblar()
        resultado = rag.buscar(
            conn, conn_idx, texto="certificado", embebedor=self.embebedor,
            filtros={"desde": "2026-09-02"},
        )
        for fragmento in resultado["fragmentos"]:
            self.assertEqual(fragmento["uid"], "martes")

    def test_el_filtro_de_persona_acota(self):
        conn, conn_idx = self.poblar()
        resultado = rag.buscar(
            conn, conn_idx, texto="certificado", embebedor=self.embebedor,
            filtros={"persona": "Ana"},
        )
        for fragmento in resultado["fragmentos"]:
            self.assertIn("Ana", fragmento["personas"])

    def test_una_consulta_sin_palabras_no_revienta_fts5(self):
        conn, conn_idx = self.poblar()
        # `consulta_fts` devuelve None y la rama lexica se queda vacia; con
        # embebedor, la densa sigue respondiendo.
        resultado = rag.buscar(conn, conn_idx, texto="***", embebedor=self.embebedor)
        self.assertIn(resultado["modo"], ("hibrida", "vacia"))

    def test_sin_indice_responde_solo_con_fts5_y_lo_dice(self):
        conn, _ = self.poblar()
        resultado = rag.buscar(conn, None, texto="Coverity")
        self.assertEqual(resultado["modo"], "lexica")
        self.assertIn("literal", resultado["aviso"])
        self.assertTrue(resultado["fragmentos"])
        self.assertEqual(
            resultado["fragmentos"][0]["idx_inicio"],
            resultado["fragmentos"][0]["idx_fin"],
        )

    def test_un_embebedor_que_falla_degrada_a_lexica_con_aviso(self):
        conn, conn_idx = self.poblar()

        def roto(_textos):
            raise RuntimeError("el modelo no responde")

        resultado = rag.buscar(conn, conn_idx, texto="Coverity", embebedor=roto)
        self.assertEqual(resultado["modo"], "lexica")
        self.assertIn("no responde", resultado["aviso"])
        self.assertTrue(resultado["fragmentos"])

    def test_el_presupuesto_de_caracteres_recorta(self):
        conn, conn_idx = self.poblar()
        resultado = rag.buscar(
            conn, conn_idx, texto="certificado", embebedor=self.embebedor,
            presupuesto=50,
        )
        self.assertLessEqual(len(resultado["fragmentos"]), 1)

    def test_cada_fragmento_apunta_a_un_segmento_que_existe(self):
        conn, conn_idx = self.poblar()
        resultado = rag.buscar(
            conn, conn_idx, texto="certificado", embebedor=self.embebedor
        )
        for fragmento in resultado["fragmentos"]:
            fila = memoria.reunion_por_uid(conn, fragmento["uid"])
            self.assertIsNotNone(fila, "la cita apunta a una reunion que no existe")
            existe = conn.execute(
                "SELECT 1 FROM segments WHERE meeting_id = ? AND idx = ?",
                (fila["id"], fragmento["idx_inicio"]),
            ).fetchone()
            self.assertIsNotNone(existe, "la cita apunta a un segmento que no existe")


class TestFusion(unittest.TestCase):
    def test_lo_que_aparece_en_las_dos_listas_sube(self):
        fusion = rag._rrf({"lexica": [1, 2, 3], "densa": [3, 4, 5]})
        primero = fusion[0]
        self.assertEqual(primero[0], 3)
        self.assertEqual(sorted(primero[2]), ["densa", "lexica"])

    def test_una_sola_lista_conserva_su_orden(self):
        fusion = rag._rrf({"lexica": [7, 8, 9]})
        self.assertEqual([i for i, _, _ in fusion], [7, 8, 9])

    def test_sin_listas_no_hay_resultados(self):
        self.assertEqual(rag._rrf({}), [])


@unittest.skipUnless(HAY_VEC, "requiere sqlite-vec")
class TestVectorial(BaseIndice):
    """Lo que solo se puede comprobar con la extension cargada."""

    def test_la_rama_densa_encuentra_sin_compartir_ninguna_palabra(self):
        conn = self.historico()
        self.reunion(
            conn,
            textos=[
                ("Javi", "certificado entorno eqsim bloqueado"),
                ("Ana", "gatos perros pajaros elefantes"),
            ],
        )
        conn_idx = self.idx()
        self.indexar(conn, conn_idx)
        vector = _vector("certificado entorno eqsim bloqueado")
        ids = rag._rama_densa(conn_idx, vector, {}, 5)
        self.assertTrue(ids)

    def test_el_filtro_se_empuja_al_in_de_sqlite_vec(self):
        conn = self.historico()
        self.reunion(conn, fecha="2026-09-01", nombre="a", textos=CHARLA)
        self.reunion(conn, fecha="2026-09-05", nombre="b", textos=CHARLA)
        conn_idx = self.idx()
        self.indexar(conn, conn_idx)
        vector = _vector("certificado")
        ids = rag._rama_densa(conn_idx, vector, {"desde": "2026-09-05"}, 10)
        uids = {
            conn_idx.execute("SELECT uid FROM chunks WHERE id = ?", (i,)).fetchone()["uid"]
            for i in ids
        }
        self.assertEqual(uids, {"b"})

    def test_un_filtro_que_no_deja_nada_devuelve_vacio(self):
        conn = self.historico()
        self.reunion(conn, textos=CHARLA)
        conn_idx = self.idx()
        self.indexar(conn, conn_idx)
        self.assertEqual(rag._rama_densa(conn_idx, _vector("x"), {"uid": "nada"}, 5), [])

    def test_la_busqueda_es_hibrida_cuando_hay_vectores(self):
        conn = self.historico()
        self.reunion(conn, textos=CHARLA)
        conn_idx = self.idx()
        self.indexar(conn, conn_idx)
        resultado = rag.buscar(
            conn, conn_idx, texto="Coverity", embebedor=self.embebedor
        )
        self.assertEqual(resultado["modo"], "hibrida")
        self.assertIsNone(resultado["aviso"])

    def test_reindexar_no_deja_vectores_sueltos(self):
        conn = self.historico()
        self.reunion(conn, textos=CHARLA)
        conn_idx = self.idx()
        self.indexar(conn, conn_idx)
        self.reunion(conn, textos=CHARLA[:2])
        self.indexar(conn, conn_idx)
        chunks = conn_idx.execute("SELECT count(*) FROM chunks").fetchone()[0]
        vectores = conn_idx.execute("SELECT count(*) FROM chunks_vec").fetchone()[0]
        self.assertEqual(chunks, vectores)


class TestConexion(BaseIndice):
    def test_solo_lectura_exige_que_el_fichero_exista(self):
        with self.assertRaises(FileNotFoundError):
            rag.conectar(self.dir / "no-existe.db", solo_lectura=True)

    def test_solo_lectura_no_deja_escribir(self):
        conn = self.historico()
        self.reunion(conn, textos=CHARLA)
        self.indexar(conn, self.idx())
        lector = rag.conectar(self.indice, solo_lectura=True)
        self.addCleanup(lector.close)
        with self.assertRaises(sqlite3.OperationalError):
            lector.execute("DELETE FROM chunks")

    def test_una_version_de_indice_distinta_se_reconstruye_sola(self):
        conn = self.historico()
        self.reunion(conn, textos=CHARLA)
        conn_idx = self.idx()
        self.indexar(conn, conn_idx)
        conn_idx.execute("PRAGMA user_version = 99")
        conn_idx.commit()
        conn_idx.close()
        # Al no guardar nada irrecuperable, "migrar" el indice es vaciarlo.
        nuevo = rag.conectar(self.indice)
        self.addCleanup(nuevo.close)
        self.assertEqual(nuevo.execute("SELECT count(*) FROM chunks").fetchone()[0], 0)

    def test_la_ruta_sale_del_entorno_si_no_se_pasa(self):
        import os

        previo = os.environ.get(rag.VARIABLE_ENTORNO)
        os.environ[rag.VARIABLE_ENTORNO] = str(self.dir / "otro.db")
        try:
            self.assertEqual(rag.ruta_indice(), self.dir / "otro.db")
            self.assertEqual(rag.ruta_indice("explicita.db"), Path("explicita.db"))
        finally:
            if previo is None:
                os.environ.pop(rag.VARIABLE_ENTORNO, None)
            else:
                os.environ[rag.VARIABLE_ENTORNO] = previo


if __name__ == "__main__":
    unittest.main(verbosity=2)
