# -*- coding: utf-8 -*-
"""Pruebas de la Fase 7: correccion humana de acciones (esquema 3).

Contra una base temporal, como el resto. El criterio de aceptacion de la fase
es `TestReproceso.test_reprocesar_conserva_las_cinco_cosas`.

    python -m unittest discover -s tests -v
"""

import ast
import json
import re
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))

import memoria  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db = Path(self._tmp.name) / "test.db"
        self.conn = memoria.conectar(self.db)
        self.addCleanup(self.conn.close)

    # -- utilidades -------------------------------------------------------

    def procesar(self, nombre, fecha, acciones=(), arrastres=()):
        """Lo mismo que hace `summarize_teams.guardar_en_bd` con las acciones."""
        meeting_id = memoria.crear_reunion(
            self.conn, fecha=fecha, transcript_path=f"/datos/{nombre}.txt"
        )
        resultado = memoria.reconciliar_acciones(self.conn, meeting_id, list(acciones))
        memoria.aplicar_arrastres(self.conn, meeting_id, list(arrastres))
        self.conn.commit()
        return meeting_id, resultado

    def uid(self, descripcion):
        return self.conn.execute(
            "SELECT uid FROM actions WHERE descripcion_llm = ?", (descripcion,)
        ).fetchone()["uid"]

    def fila(self, uid):
        return self.conn.execute("SELECT * FROM actions WHERE uid = ?", (uid,)).fetchone()

    def id_de(self, uid):
        return self.fila(uid)["id"]

    def foto(self):
        """Todo lo que una operacion y su deshacer deben dejar igual."""
        return {
            tabla: [
                tuple(f)
                for f in self.conn.execute(f"SELECT * FROM {tabla} ORDER BY 1, 2")
            ]
            for tabla in ("actions", "action_mentions", "action_dependencias")
        }

    def poblar(self):
        """Dos dailys con los casos de los datos reales (seccion 'Que se ve')."""
        self.procesar(
            "lunes",
            "2026-09-07",
            [
                {"descripcion": "Seguir estabilizando la version de ULS", "persona": "Javi"},
                {"descripcion": "Continuar trabajo en banda base", "persona": "Javi"},
                {"descripcion": "Cerrar GCS4-1", "persona": "Pedro"},
            ],
        )
        self.uls = self.uid("Seguir estabilizando la version de ULS")
        self.banda = self.uid("Continuar trabajo en banda base")
        self.gcs = self.uid("Cerrar GCS4-1")
        self.procesar(
            "jueves",
            "2026-09-10",
            [
                {"descripcion": "Seguir con ULS y banda base", "persona": "Javi"},
                {"descripcion": "Hablar con Diego tras la entrega de GCS4-1", "persona": "Pedro"},
                {"descripcion": "Revisar la wiki", "persona": "equipo"},
            ],
            [
                {"action_id": self.id_de(self.uls), "estado": "en_progreso",
                 "comentario": "sigue"},
                {"action_id": self.id_de(self.gcs), "estado": "en_progreso"},
            ],
        )
        self.uls2 = self.uid("Seguir con ULS y banda base")
        self.diego = self.uid("Hablar con Diego tras la entrega de GCS4-1")
        self.wiki = self.uid("Revisar la wiki")


# --------------------------------------------------------------------------
# Esquema y migracion
# --------------------------------------------------------------------------


class TestMigracion(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db = Path(self._tmp.name) / "v2.db"

    def _base_v2(self):
        conn = sqlite3.connect(self.db)
        conn.executescript(
            """
            CREATE TABLE meetings (
              id INTEGER PRIMARY KEY, uid TEXT, fecha TEXT NOT NULL, titulo TEXT,
              tipo TEXT NOT NULL DEFAULT 'daily', audio_path TEXT,
              transcript_path TEXT, duracion_seg REAL, modelo_whisper TEXT,
              modelo_llm TEXT, resumen TEXT, datos_json TEXT, creado_en TEXT NOT NULL
            );
            CREATE TABLE personas (id INTEGER PRIMARY KEY, nombre TEXT NOT NULL UNIQUE,
              alias TEXT, activo INTEGER NOT NULL DEFAULT 1);
            CREATE TABLE actions (
              id INTEGER PRIMARY KEY, descripcion TEXT NOT NULL,
              persona_id INTEGER REFERENCES personas(id),
              meeting_id_origen INTEGER NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
              meeting_id_ultima INTEGER REFERENCES meetings(id),
              estado TEXT NOT NULL, menciones INTEGER NOT NULL DEFAULT 1, cerrada_en TEXT
            );
            INSERT INTO meetings (id, uid, fecha, transcript_path, datos_json, creado_en) VALUES
              (1, 'lunes', '2026-09-07', '/d/lunes.txt', NULL, 'x'),
              (2, 'jueves', '2026-09-10', '/d/jueves.txt',
               '{"arrastres": [{"action_id": 2, "estado": "completada", "comentario": "hecho"}]}',
               'x'),
              (3, 'viernes', '2026-09-11', '/d/viernes.txt', NULL, 'x');
            INSERT INTO actions VALUES
              (1, 'Sin tocar', NULL, 1, 1, 'abierta', 1, NULL),
              (2, 'Cerrada el jueves', NULL, 1, 2, 'completada', 2, '2026-09-12'),
              (3, 'Con tres menciones', NULL, 1, 3, 'bloqueada', 3, NULL),
              (4, 'Del jueves', NULL, 2, 2, 'abierta', 1, NULL);
            PRAGMA user_version = 2;
            """
        )
        conn.commit()
        conn.close()

    def test_rellena_uid_descripcion_llm_y_menciones(self):
        self._base_v2()
        cruda = sqlite3.connect(self.db)
        self.assertEqual(memoria.menciones_perdidas_en_migracion(cruda), 1)
        cruda.close()

        conn = memoria.conectar(self.db)
        self.addCleanup(conn.close)
        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 3)
        filas = conn.execute("SELECT * FROM actions ORDER BY id").fetchall()
        self.assertEqual(
            [f["uid"] for f in filas], ["lunes-a1", "lunes-a2", "lunes-a3", "jueves-a1"]
        )
        self.assertTrue(all(f["descripcion_llm"] == f["descripcion"] for f in filas))
        menciones = {
            (m["action_id"], m["meeting_id"]): (m["estado"], m["comentario"])
            for m in conn.execute("SELECT * FROM action_mentions")
        }
        self.assertEqual(
            menciones,
            {
                (1, 1): ("abierta", None),
                (2, 1): ("abierta", None),
                (2, 2): ("completada", "hecho"),
                (3, 1): ("abierta", None),
                (3, 3): ("bloqueada", None),
                (4, 2): ("abierta", None),
            },
        )
        # La cache no se toca al migrar: el contador de la de tres menciones
        # es mas fiel que las dos filas que se han podido reconstruir.
        self.assertEqual(filas[2]["menciones"], 3)

    def test_migracion_idempotente(self):
        self._base_v2()
        memoria.conectar(self.db).close()
        conn = memoria.conectar(self.db)
        self.addCleanup(conn.close)
        self.assertEqual(conn.execute("SELECT count(*) FROM action_mentions").fetchone()[0], 6)

    @unittest.skipUnless(
        (RAIZ / "datos" / "meetings.db").exists(), "no hay base real en datos/"
    )
    def test_copia_de_la_base_real(self):
        origen = sqlite3.connect(RAIZ / "datos" / "meetings.db")
        destino = sqlite3.connect(self.db)
        origen.backup(destino)
        origen.close()
        destino.close()

        conn = memoria.conectar(self.db)
        self.addCleanup(conn.close)
        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 3)
        total, con_uid, sin_mencion = conn.execute(
            """
            SELECT count(*), count(uid),
                   sum(NOT EXISTS (SELECT 1 FROM action_mentions am
                                    WHERE am.action_id = a.id))
              FROM actions a
            """
        ).fetchone()
        self.assertEqual(total, con_uid)
        self.assertEqual(sin_mencion or 0, 0)
        # Recalcular desde las menciones no debe cambiar ningun estado.
        antes = conn.execute("SELECT id, estado FROM actions ORDER BY id").fetchall()
        for fila in antes:
            memoria._recalcular_accion(conn, fila["id"])
        despues = conn.execute("SELECT id, estado FROM actions ORDER BY id").fetchall()
        self.assertEqual([tuple(f) for f in antes], [tuple(f) for f in despues])
        conn.rollback()


# --------------------------------------------------------------------------
# Emparejamiento (pura)
# --------------------------------------------------------------------------


class TestEmparejar(unittest.TestCase):
    def test_identica_ignora_acentos_y_puntuacion(self):
        plan = memoria.emparejar_acciones(
            [{"descripcion_llm": "Revisar la versión de ULS."}],
            [{"descripcion": "revisar la version de uls"}],
        )
        self.assertEqual(plan["parejas"], [(0, 0, 1.0, "identica")])

    def test_similar_por_encima_del_umbral(self):
        plan = memoria.emparejar_acciones(
            [{"descripcion_llm": "Seguir estabilizando la version de ULS"}],
            [{"descripcion": "Seguir estabilizando la version ULS"}],
        )
        self.assertEqual(len(plan["parejas"]), 1)
        self.assertEqual(plan["parejas"][0][3], "similar")

    def test_por_debajo_del_umbral_no_empareja(self):
        plan = memoria.emparejar_acciones(
            [{"descripcion_llm": "Cerrar GCS4-1"}],
            [{"descripcion": "Actualizar la wiki del proyecto"}],
        )
        self.assertEqual(plan["parejas"], [])
        self.assertEqual(plan["sin_pareja"], [0])
        self.assertEqual(plan["nuevas"], [0])

    def test_exclusiva_y_desempate_por_responsable(self):
        existentes = [
            {"descripcion_llm": "Revisar el informe de Coverity", "persona": "Ana"},
            {"descripcion_llm": "Revisar el informe de Coverity", "persona": "Beto"},
        ]
        nuevas = [{"descripcion": "Revisar informe de Coverity", "persona": "Beto"}]
        plan = memoria.emparejar_acciones(existentes, nuevas)
        self.assertEqual([(i, j) for i, j, *_ in plan["parejas"]], [(1, 0)])
        self.assertEqual(plan["sin_pareja"], [0])

    def test_compara_con_la_descripcion_del_modelo_no_con_la_corregida(self):
        plan = memoria.emparejar_acciones(
            [{"descripcion": "Texto reescrito a mano", "descripcion_llm": "Cerrar GCS4-1"}],
            [{"descripcion": "Cerrar GCS4-1"}],
        )
        self.assertEqual(len(plan["parejas"]), 1)


# --------------------------------------------------------------------------
# Operaciones y su deshacer
# --------------------------------------------------------------------------


class TestOperaciones(Base):
    def setUp(self):
        super().setUp()
        self.poblar()

    def test_corregir_y_deshacer_cada_campo(self):
        for campo, valor in (
            ("descripcion", "Estabilizar ULS 3.2"),
            ("persona", "Lara"),
            ("estado", "completada"),
        ):
            with self.subTest(campo=campo):
                antes = self.foto()
                ficha = memoria.corregir_accion(self.conn, self.uls, **{campo: valor})
                self.assertNotEqual(self.foto(), antes)
                correccion = ficha["correcciones"][-1]
                self.assertEqual(correccion["campo"], campo)
                memoria.deshacer_correccion(self.conn, correccion["id"])
                self.assertEqual(self.foto(), antes)

    def test_cerrar_pone_fecha_y_reabrir_la_quita(self):
        ficha = memoria.corregir_accion(self.conn, self.uls, estado="completada")
        self.assertEqual(ficha["estado"], "completada")
        self.assertIsNotNone(ficha["cerrada_en"])
        ficha = memoria.corregir_accion(self.conn, self.uls, estado="abierta")
        self.assertIsNone(ficha["cerrada_en"])

    def test_estado_invalido_se_rechaza(self):
        with self.assertRaises(memoria.CorreccionInvalida):
            memoria.corregir_accion(self.conn, self.uls, estado="casi")
        with self.assertRaises(memoria.AccionNoEncontrada):
            memoria.corregir_accion(self.conn, "no-existe", estado="abierta")

    def test_descartar_y_restaurar_con_dependencias(self):
        memoria.anadir_dependencia(self.conn, self.diego, self.gcs)
        antes = self.foto()
        ficha = memoria.descartar_accion(self.conn, self.gcs, "no es una tarea")
        self.assertIsNotNone(ficha["descartada_en"])
        self.assertEqual(ficha["bloquea_a"], [], "la dependencia hacia ella se borra")
        memoria.restaurar_accion(self.conn, self.gcs)
        self.assertEqual(self.foto(), antes)

    def test_fusionar_y_separar(self):
        # La duplicada ya absorbia otra y tiene una dependencia propia.
        memoria.fusionar_acciones(self.conn, self.uls2, self.banda)
        memoria.anadir_dependencia(self.conn, self.uls2, self.gcs)
        antes = self.foto()

        ficha = memoria.fusionar_acciones(self.conn, self.uls, self.uls2)
        self.assertEqual(
            sorted(a["uid"] for a in ficha["absorbidas"]), sorted([self.uls2, self.banda]),
            "no hay fusiones en cadena: lo absorbido pasa a la principal",
        )
        self.assertEqual([d["uid"] for d in ficha["depende_de"]], [self.gcs])

        memoria.separar_acciones(self.conn, self.uls2)
        self.assertEqual(self.foto(), antes)

    def test_la_fusion_une_menciones_sin_contar_dos_veces(self):
        # uls: lunes + jueves. uls2: jueves. La union son dos reuniones.
        antes = self.fila(self.uls)["estado"]
        memoria.fusionar_acciones(self.conn, self.uls, self.uls2)
        principal = self.fila(self.uls)
        self.assertEqual(principal["menciones"], 2)
        self.assertEqual(principal["estado"], antes, "el estado de la principal no cambia")
        self.assertEqual(len(memoria.detalle_accion(self.conn, self.uls)["menciones_detalle"]), 3)

    def test_anadir_y_quitar_dependencia(self):
        antes = self.foto()
        memoria.anadir_dependencia(self.conn, self.diego, self.gcs)
        memoria.quitar_dependencia(self.conn, self.diego, self.gcs)
        self.assertEqual(self.foto(), antes)

    def test_ciclos_y_consigo_misma_rechazados(self):
        memoria.anadir_dependencia(self.conn, self.diego, self.gcs)
        memoria.anadir_dependencia(self.conn, self.gcs, self.uls)
        with self.assertRaises(memoria.CorreccionInvalida):
            memoria.anadir_dependencia(self.conn, self.uls, self.diego)
        with self.assertRaises(memoria.CorreccionInvalida):
            memoria.anadir_dependencia(self.conn, self.uls, self.uls)

    def test_no_se_depende_de_descartadas_ni_absorbidas(self):
        memoria.descartar_accion(self.conn, self.wiki)
        memoria.fusionar_acciones(self.conn, self.uls, self.uls2)
        for otra in (self.wiki, self.uls2):
            with self.subTest(otra=otra), self.assertRaises(memoria.CorreccionInvalida):
                memoria.anadir_dependencia(self.conn, self.diego, otra)

    def test_fusionar_una_absorbida_rechazado(self):
        memoria.fusionar_acciones(self.conn, self.uls, self.uls2)
        with self.assertRaises(memoria.CorreccionInvalida):
            memoria.fusionar_acciones(self.conn, self.banda, self.uls2)
        with self.assertRaises(memoria.CorreccionInvalida):
            memoria.fusionar_acciones(self.conn, self.uls2, self.banda)

    def test_cerrar_con_dependencias_abiertas_avisa_pero_no_bloquea(self):
        memoria.anadir_dependencia(self.conn, self.diego, self.gcs)
        ficha = memoria.corregir_accion(self.conn, self.diego, estado="completada")
        self.assertEqual(ficha["estado"], "completada")
        self.assertEqual(len(ficha["avisos"]), 1)
        self.assertIn(self.gcs, ficha["avisos"][0])

    def test_un_fallo_no_deja_la_operacion_a_medias(self):
        antes = self.foto()
        with self.assertRaises(memoria.CorreccionInvalida):
            memoria.corregir_accion(
                self.conn, self.uls, descripcion="Nueva", estado="inventado"
            )
        self.assertEqual(self.foto(), antes)

    def test_origen_desconocido_rechazado(self):
        with self.assertRaises(memoria.CorreccionInvalida):
            memoria.descartar_accion(self.conn, self.wiki, origen="telepatia")


# --------------------------------------------------------------------------
# Reconciliacion al reprocesar
# --------------------------------------------------------------------------


class TestReproceso(Base):
    JUEVES_REDACTADO_DE_OTRA_FORMA = [
        {"descripcion": "Seguir con ULS y la banda base", "persona": "Javi"},
        {"descripcion": "Hablar con Diego tras entregar GCS4-1", "persona": "Pedro"},
        {"descripcion": "Revisar la wiki", "persona": "equipo"},
    ]

    def reprocesar_jueves(self, acciones=None, arrastres=None):
        return self.procesar(
            "jueves",
            "2026-09-10",
            self.JUEVES_REDACTADO_DE_OTRA_FORMA if acciones is None else acciones,
            (
                [{"action_id": self.id_de(self.gcs), "estado": "en_progreso"}]
                if arrastres is None
                else arrastres
            ),
        )

    def test_reprocesar_conserva_las_cinco_cosas(self):
        """Criterio de aceptacion de la Fase 7."""
        self.poblar()
        memoria.corregir_accion(self.conn, self.diego, persona="Lara",
                                descripcion="Llamar a Diego cuando salga GCS4-1")
        memoria.descartar_accion(self.conn, self.wiki, "no es una tarea")
        memoria.fusionar_acciones(self.conn, self.uls, self.uls2)
        memoria.anadir_dependencia(self.conn, self.diego, self.gcs)
        ids = {u: self.id_de(u) for u in (self.diego, self.wiki, self.uls, self.uls2)}

        _, resultado = self.reprocesar_jueves()

        self.assertEqual(resultado["nuevas"], 0, "no debe regenerar ninguna")
        self.assertEqual({u: self.id_de(u) for u in ids}, ids, "id y uid se conservan")
        diego = memoria.detalle_accion(self.conn, self.diego)
        self.assertEqual(diego["descripcion"], "Llamar a Diego cuando salga GCS4-1")
        self.assertEqual(diego["descripcion_llm"], "Hablar con Diego tras entregar GCS4-1")
        self.assertEqual(diego["persona"], "Lara")
        self.assertEqual([d["uid"] for d in diego["depende_de"]], [self.gcs])
        self.assertIsNotNone(self.fila(self.wiki)["descartada_en"])
        self.assertEqual(self.fila(self.uls2)["absorbida_por"], ids[self.uls])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM actions").fetchone()[0], 6)

    def test_reproceso_sin_correcciones_es_idempotente(self):
        self.poblar()
        antes = self.foto()
        self.procesar(
            "jueves",
            "2026-09-10",
            [
                {"descripcion": "Seguir con ULS y banda base", "persona": "Javi"},
                {"descripcion": "Hablar con Diego tras la entrega de GCS4-1", "persona": "Pedro"},
                {"descripcion": "Revisar la wiki", "persona": "equipo"},
            ],
            [
                {"action_id": self.id_de(self.uls), "estado": "en_progreso",
                 "comentario": "sigue"},
                {"action_id": self.id_de(self.gcs), "estado": "en_progreso"},
            ],
        )
        self.assertEqual(self.foto(), antes)

    def test_protegida_sin_pareja_queda_para_revisar_y_la_otra_desaparece(self):
        self.poblar()
        memoria.anadir_dependencia(self.conn, self.diego, self.gcs)
        _, resultado = self.reprocesar_jueves(
            acciones=[{"descripcion": "Algo totalmente distinto", "persona": "Ana"}]
        )
        self.assertEqual(resultado["revisar"], 1)
        self.assertEqual(self.fila(self.diego)["revisar"], 1)
        self.assertIsNone(self.fila(self.wiki), "sin proteger se borra, como antes")
        self.assertIsNone(self.fila(self.uls2))
        nueva = self.uid("Algo totalmente distinto")
        self.assertNotIn(nueva, (self.wiki, self.uls2), "un uid borrado no se reutiliza")

    def test_una_pareja_posterior_quita_la_marca_de_revisar(self):
        self.poblar()
        memoria.descartar_accion(self.conn, self.wiki)
        self.reprocesar_jueves(acciones=[])
        self.assertEqual(self.fila(self.wiki)["revisar"], 1)
        self.reprocesar_jueves()
        self.assertEqual(self.fila(self.wiki)["revisar"], 0)

    def test_descartada_regenerada_sigue_descartada(self):
        self.poblar()
        memoria.descartar_accion(self.conn, self.wiki)
        self.reprocesar_jueves()
        self.assertIsNotNone(self.fila(self.wiki)["descartada_en"])

    def test_estado_corregido_frente_a_reuniones_anteriores_y_posteriores(self):
        self.poblar()
        correccion = memoria.corregir_accion(self.conn, self.gcs, estado="completada")
        # La correccion se hizo el dia 12, entre el jueves (10) y el lunes (14).
        self.conn.execute(
            "UPDATE correcciones SET creado_en = '2026-09-12 10:00:00' WHERE id = ?",
            (correccion["correcciones"][-1]["id"],),
        )
        self.conn.commit()

        self.reprocesar_jueves(arrastres=[{"action_id": self.id_de(self.gcs),
                                           "estado": "bloqueada"}])
        self.assertEqual(self.fila(self.gcs)["estado"], "completada",
                         "reprocesar el jueves no deshace lo corregido el 12")
        self.assertEqual(self.fila(self.gcs)["cerrada_en"], "2026-09-12")
        mencion = self.conn.execute(
            "SELECT estado FROM action_mentions am JOIN meetings m ON m.id = am.meeting_id "
            "WHERE am.action_id = ? AND m.uid = 'jueves'", (self.id_de(self.gcs),)
        ).fetchone()
        self.assertEqual(mencion["estado"], "bloqueada", "la mencion se registra igual")

        self.procesar("lunes2", "2026-09-14", [],
                      [{"action_id": self.id_de(self.gcs), "estado": "bloqueada"}])
        self.assertEqual(self.fila(self.gcs)["estado"], "bloqueada",
                         "una reunion posterior es informacion nueva")

    def test_deshacer_arrastres_recupera_el_estado_real(self):
        """Adios al 'las cerradas vuelven a abierta' de la v2."""
        self.poblar()
        self.procesar("viernes", "2026-09-11", [],
                      [{"action_id": self.id_de(self.gcs), "estado": "completada"}])
        self.assertEqual(self.fila(self.gcs)["cerrada_en"], "2026-09-11")
        self.procesar("viernes", "2026-09-11", [], [])
        gcs = self.fila(self.gcs)
        self.assertEqual(gcs["estado"], "en_progreso", "el que dijo el jueves")
        self.assertIsNone(gcs["cerrada_en"])
        self.assertEqual(gcs["menciones"], 2)

    def test_la_fila_de_la_reunion_se_actualiza_en_su_sitio(self):
        self.poblar()
        jueves = self.conn.execute("SELECT id FROM meetings WHERE uid = 'jueves'").fetchone()[0]
        meeting_id, _ = self.reprocesar_jueves()
        self.assertEqual(meeting_id, jueves)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM meetings").fetchone()[0], 2)

    def test_simulacion_no_escribe(self):
        self.poblar()
        antes = self.foto()
        jueves = memoria.id_reunion_por_transcripcion(self.conn, "/datos/jueves.txt")
        informe = memoria.simular_reconciliacion(
            self.conn, jueves, [{"descripcion": "Revisar la wiki"},
                                {"descripcion": "Otra cosa"}]
        )
        self.assertEqual(self.foto(), antes)
        self.assertEqual([p["uid"] for p in informe["parejas"]], [self.wiki])
        self.assertEqual(len(informe["sin_pareja"]), 2)
        self.assertEqual(informe["nuevas"], ["Otra cosa"])


# --------------------------------------------------------------------------
# Arrastres y lecturas
# --------------------------------------------------------------------------


class TestArrastres(Base):
    def setUp(self):
        super().setUp()
        self.poblar()

    def test_no_se_ofrecen_descartadas_ni_absorbidas(self):
        memoria.descartar_accion(self.conn, self.wiki)
        memoria.fusionar_acciones(self.conn, self.uls, self.uls2)
        ofrecidas = {a["uid"] for a in memoria.acciones_abiertas(self.conn, 5)}
        self.assertNotIn(self.wiki, ofrecidas)
        self.assertNotIn(self.uls2, ofrecidas)
        self.assertIn(self.uls, ofrecidas)

    def test_lleva_sus_dependencias_abiertas_y_la_descripcion_corregida(self):
        memoria.anadir_dependencia(self.conn, self.diego, self.gcs)
        memoria.corregir_accion(self.conn, self.diego, descripcion="Llamar a Diego")
        diego = next(a for a in memoria.acciones_abiertas(self.conn, 5)
                     if a["uid"] == self.diego)
        self.assertEqual(diego["bloqueada_por"], [self.id_de(self.gcs)])
        self.assertEqual(diego["descripcion"], "Llamar a Diego")

        memoria.corregir_accion(self.conn, self.gcs, estado="completada")
        diego = next(a for a in memoria.acciones_abiertas(self.conn, 5)
                     if a["uid"] == self.diego)
        self.assertEqual(diego["bloqueada_por"], [], "una dependencia cerrada no bloquea")

    def test_formato_para_el_modelo(self):
        try:
            import summarize_teams
        except ImportError as exc:  # pragma: no cover - depende del entorno
            self.skipTest(f"summarize_teams no se puede importar: {exc}")
        memoria.anadir_dependencia(self.conn, self.diego, self.gcs)
        texto = summarize_teams.formatear_acciones_abiertas(
            memoria.acciones_abiertas(self.conn, 5)
        )
        self.assertIn(f"bloqueada por [{self.id_de(self.gcs)}])", texto)
        self.assertIn("bloqueada por [n]", summarize_teams.ARRASTRES_PROMPT)


class TestLecturasIgnoranDescartadasYAbsorbidas(Base):
    """Una descartada que se cuela en `metricas()` y no en el tablero es
    exactamente la contradiccion que esta herramienta existe para evitar."""

    # Funciones que pueden leer `actions` directamente: escriben, migran o son
    # la ficha de detalle (que tambien ensena lo descartado).
    LISTA_BLANCA = {
        "_migrar_a_3",
        "menciones_perdidas_en_migracion",
        "_uid_accion",
        "_derivar_accion",
        "_recalcular_accion",
        "_marcar_para_reconciliar",
        "_acciones_a_reconciliar",
        "accion_protegida",
        "reconciliar_acciones",
        "aplicar_arrastres",
        "_accion_por_uid",
        "deshacer_correccion",
        "fusionar_acciones",
        "detalle_accion",
    }

    def test_ninguna_lectura_nueva_de_la_tabla_actions(self):
        fuente = (RAIZ / "memoria.py").read_text(encoding="utf-8")
        patron = re.compile(r"(?<!DELETE )\bFROM\s+actions\b")
        infractores = []
        for nodo in ast.parse(fuente).body:
            if isinstance(nodo, ast.FunctionDef):
                nombre = nodo.name
            elif isinstance(nodo, ast.Assign) and nodo.targets[0].id != "_ESQUEMA":
                nombre = nodo.targets[0].id
            else:
                continue
            if nombre in self.LISTA_BLANCA:
                continue
            if patron.search(ast.get_source_segment(fuente, nodo) or ""):
                infractores.append(nombre)
        self.assertEqual(infractores, [], "usa la vista acciones_vigentes")

    def test_cada_consulta_publica(self):
        self.poblar()
        memoria.descartar_accion(self.conn, self.wiki)
        memoria.fusionar_acciones(self.conn, self.uls, self.uls2)
        ocultas = {self.wiki, self.uls2}
        jueves = memoria.id_reunion_por_transcripcion(self.conn, "/datos/jueves.txt")
        vigentes = 4  # uls, banda, gcs y diego

        def uids(filas):
            return {f["uid"] for f in filas}

        self.assertFalse(ocultas & uids(memoria.listar_acciones(self.conn)))
        self.assertEqual(memoria.contar_acciones(self.conn), vigentes)
        self.assertEqual(sum(memoria.acciones_por_estado(self.conn).values()), vigentes)
        self.assertNotIn("equipo", [r["persona"] for r in
                                    memoria.responsables_de_acciones(self.conn)])
        self.assertFalse(ocultas & uids(memoria.acciones_de_reunion(self.conn, jueves)))
        self.assertFalse(ocultas & uids(memoria.arrastres_de_reunion(self.conn, jueves)))
        self.assertEqual(
            {r["uid"]: r["n_acciones"] for r in memoria.listar_reuniones(self.conn)},
            {"lunes": 3, "jueves": 1},
        )
        self.assertEqual(memoria.resumen_bd(self.conn)["acciones"], vigentes)
        self.assertEqual(sum(memoria.metricas(self.conn)["acciones"].values()), vigentes)
        self.assertEqual(len(memoria.carriles_acciones(self.conn)), vigentes)
        self.assertFalse(ocultas & uids(memoria.acciones_sin_mencion(self.conn, minimo=0)))
        self.assertNotIn(
            "equipo", [p["persona"] for p in memoria.personas_sin_actualizar(self.conn)]
        )
        memoria.corregir_accion(self.conn, self.banda, estado="completada")
        self.conn.execute(
            "UPDATE actions SET estado = 'completada', cerrada_en = '2026-09-10' "
            "WHERE uid = ?", (self.wiki,)
        )
        self.assertEqual(
            uids(memoria.acciones_cerradas(self.conn)), {self.banda},
            "la descartada no cuenta aunque este cerrada",
        )


if __name__ == "__main__":
    unittest.main()
