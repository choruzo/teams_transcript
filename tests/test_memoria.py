# -*- coding: utf-8 -*-
"""Pruebas de `memoria.py`.

`unittest` de la stdlib, sin dependencias: el proyecto entero se apoya en que
`python tests/...` funcione en un servidor offline sin instalar nada.

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


class BaseTemporal(unittest.TestCase):
    """Cada prueba con su propia base en un directorio temporal."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.db = self.dir / "test.db"
        self.addCleanup(self._tmp.cleanup)

    def conectar(self, **kwargs):
        conn = memoria.conectar(self.db, **kwargs)
        self.addCleanup(conn.close)
        return conn

    def crear(self, conn, transcript, *, fecha="2026-09-01", **kwargs):
        meeting_id = memoria.crear_reunion(
            conn,
            fecha=fecha,
            transcript_path=str(transcript),
            **kwargs,
        )
        conn.commit()
        return meeting_id


class TestIdentidadEstable(BaseTemporal):
    """D1: reprocesar una transcripcion no puede cambiar su identidad."""

    def test_uid_derivado_del_nombre(self):
        conn = self.conectar()
        self.crear(conn, "/datos/20260907_090000_mixed.txt")
        fila = conn.execute("SELECT uid FROM meetings").fetchone()
        self.assertEqual(fila["uid"], "20260907_090000_mixed")

    def test_uid_y_id_sobreviven_al_reproceso(self):
        conn = self.conectar()
        ruta = "/datos/20260907_090000_mixed.txt"
        primero = self.crear(conn, ruta, titulo="Daily")
        uid_inicial = conn.execute("SELECT uid FROM meetings").fetchone()["uid"]

        segundo = self.crear(conn, ruta, titulo="Daily reprocesada")

        self.assertEqual(primero, segundo, "el id deberia reutilizarse")
        filas = conn.execute("SELECT uid, titulo FROM meetings").fetchall()
        self.assertEqual(len(filas), 1, "reprocesar sustituye, no duplica")
        self.assertEqual(filas[0]["uid"], uid_inicial)
        self.assertEqual(filas[0]["titulo"], "Daily reprocesada")

    def test_uid_distinto_por_transcripcion(self):
        conn = self.conectar()
        self.crear(conn, "/datos/20260907_090000_mixed.txt")
        self.crear(conn, "/datos/20260908_090000_mixed.txt", fecha="2026-09-02")
        uids = {f["uid"] for f in conn.execute("SELECT uid FROM meetings")}
        self.assertEqual(uids, {"20260907_090000_mixed", "20260908_090000_mixed"})

    def test_mismo_nombre_en_carpetas_distintas_no_colisiona(self):
        conn = self.conectar()
        self.crear(conn, "/datos/a/daily.txt")
        self.crear(conn, "/datos/b/daily.txt", fecha="2026-09-02")
        uids = [f["uid"] for f in conn.execute("SELECT uid FROM meetings ORDER BY id")]
        self.assertEqual(uids[0], "daily")
        self.assertNotEqual(uids[0], uids[1], "el segundo debe desempatarse")
        self.assertTrue(uids[1].startswith("daily-"))

    def test_reunion_por_uid(self):
        conn = self.conectar()
        ruta = "/datos/20260907_090000_mixed.txt"
        self.crear(conn, ruta, titulo="Daily")
        self.crear(conn, ruta, titulo="Reprocesada")  # cambia el contenido
        fila = memoria.reunion_por_uid(conn, "20260907_090000_mixed")
        self.assertIsNotNone(fila, "el uid debe resolver tras un reproceso")
        self.assertEqual(fila["titulo"], "Reprocesada")

    def test_uid_normaliza_caracteres_raros(self):
        self.assertEqual(
            memoria.uid_transcripcion("/x/Reunión Equipo (final).txt"),
            "reuni-n-equipo-final",
        )
        self.assertIsNone(memoria.uid_transcripcion(None))


class TestSoloLectura(BaseTemporal):
    """D5: un GET de la API no puede tocar el historico."""

    def test_rechaza_escrituras(self):
        self.crear(self.conectar(), "/datos/daily.txt")
        ro = self.conectar(solo_lectura=True)
        self.assertEqual(len(ro.execute("SELECT id FROM meetings").fetchall()), 1)
        with self.assertRaises(sqlite3.OperationalError):
            ro.execute("DELETE FROM meetings")

    def test_no_crea_la_base_si_no_existe(self):
        with self.assertRaises(FileNotFoundError):
            memoria.conectar(self.dir / "no_existe.db", solo_lectura=True)
        self.assertFalse((self.dir / "no_existe.db").exists())

    def test_lee_con_wal_sin_escritor_activo(self):
        """El caso que rompe la URI `mode=ro`: base en WAL y nadie escribiendo."""
        conn = self.conectar()
        self.crear(conn, "/datos/daily.txt")
        conn.close()
        ro = memoria.conectar(self.db, solo_lectura=True)
        self.addCleanup(ro.close)
        self.assertEqual(ro.execute("SELECT count(*) FROM meetings").fetchone()[0], 1)


class TestConcurrencia(BaseTemporal):
    def test_wal_activo(self):
        conn = self.conectar()
        modo = conn.execute("PRAGMA journal_mode").fetchone()[0]
        self.assertEqual(modo.lower(), "wal")

    def test_lectura_mientras_hay_escritura_sin_confirmar(self):
        escritor = self.conectar()
        self.crear(escritor, "/datos/daily.txt")
        escritor.execute(
            "INSERT INTO risks (meeting_id, descripcion) VALUES (1, 'a medias')"
        )  # sin commit
        lector = self.conectar(solo_lectura=True)
        self.assertEqual(
            lector.execute("SELECT count(*) FROM meetings").fetchone()[0],
            1,
            "WAL deberia permitir leer con una escritura en curso",
        )


class TestRutaLocal(BaseTemporal):
    """D2: las rutas guardadas son de otra maquina."""

    def _con_entorno(self, origen, local):
        os.environ[memoria.VARIABLE_RAIZ_ORIGEN] = origen
        os.environ[memoria.VARIABLE_RAIZ_LOCAL] = local
        self.addCleanup(os.environ.pop, memoria.VARIABLE_RAIZ_ORIGEN, None)
        self.addCleanup(os.environ.pop, memoria.VARIABLE_RAIZ_LOCAL, None)

    def test_sin_variables_devuelve_la_ruta_tal_cual(self):
        self.assertEqual(
            memoria.ruta_local("/home/x/grabaciones/a.wav"),
            Path("/home/x/grabaciones/a.wav"),
        )
        self.assertIsNone(memoria.ruta_local(None))

    def test_remapea_el_prefijo(self):
        self._con_entorno("/home/x/grabaciones", "/grabaciones")
        self.assertEqual(
            memoria.ruta_local("/home/x/grabaciones/2026/a.wav"),
            Path("/grabaciones/2026/a.wav"),
        )

    def test_remapea_ruta_windows_leida_en_otro_sitio(self):
        self._con_entorno(r"C:\Users\x\grabaciones", "/grabaciones")
        self.assertEqual(
            memoria.ruta_local(r"C:\Users\x\grabaciones\a.wav"),
            Path("/grabaciones/a.wav"),
        )

    def test_ruta_ajena_al_prefijo_no_se_toca(self):
        self._con_entorno("/home/x/grabaciones", "/grabaciones")
        self.assertEqual(memoria.ruta_local("/otro/a.wav"), Path("/otro/a.wav"))


class TestMigracion(BaseTemporal):
    """D6: primera migracion real del proyecto (esquema 1 -> 2)."""

    def _base_v1(self):
        """Una base como las que dejaba el codigo anterior: sin `uid`."""
        conn = sqlite3.connect(self.db)
        conn.executescript(
            """
            CREATE TABLE meetings (
              id INTEGER PRIMARY KEY, fecha TEXT NOT NULL, titulo TEXT,
              tipo TEXT NOT NULL DEFAULT 'daily', audio_path TEXT,
              transcript_path TEXT, duracion_seg REAL, modelo_whisper TEXT,
              modelo_llm TEXT, resumen TEXT, datos_json TEXT,
              creado_en TEXT NOT NULL
            );
            INSERT INTO meetings (id, fecha, transcript_path, creado_en)
            VALUES (1, '2026-09-07', '/datos/20260907_090000_mixed.txt', '2026-09-07'),
                   (2, '2026-09-08', '/datos/20260908_090000_mixed.txt', '2026-09-08');
            PRAGMA user_version = 1;
            """
        )
        conn.commit()
        conn.close()

    def test_rellena_uid_en_bases_existentes(self):
        self._base_v1()
        conn = self.conectar()
        filas = conn.execute("SELECT id, uid FROM meetings ORDER BY id").fetchall()
        self.assertEqual(
            [f["uid"] for f in filas],
            ["20260907_090000_mixed", "20260908_090000_mixed"],
        )
        self.assertEqual(
            conn.execute("PRAGMA user_version").fetchone()[0], memoria.ESQUEMA_VERSION
        )

    def test_migracion_idempotente(self):
        self._base_v1()
        self.conectar().close()
        conn = self.conectar()  # segunda apertura, ya migrada
        self.assertEqual(conn.execute("SELECT count(*) FROM meetings").fetchone()[0], 2)

    def test_rechaza_esquema_del_futuro(self):
        self.conectar().close()
        cru = sqlite3.connect(self.db)
        cru.execute(f"PRAGMA user_version = {memoria.ESQUEMA_VERSION + 1}")
        cru.commit()
        cru.close()
        with self.assertRaises(RuntimeError):
            memoria.conectar(self.db)


class TestArrastresSiguenBien(BaseTemporal):
    """Red de seguridad: D0 no puede haber roto la Fase 2."""

    def test_reproceso_idempotente_con_arrastres(self):
        conn = self.conectar()
        primera = self.crear(conn, "/datos/dia1.txt", fecha="2026-09-01")
        memoria.insertar_acciones(
            conn,
            primera,
            [{"persona": "Javi", "descripcion": "Reinstalar ULS", "estado": "abierta"}],
        )
        conn.commit()

        accion = conn.execute("SELECT id FROM actions").fetchone()["id"]

        def procesar_dia2():
            previa = memoria.id_reunion_por_transcripcion(conn, "/datos/dia2.txt")
            abiertas = memoria.acciones_abiertas(conn, 5, excluir_meeting_id=previa)
            segunda = self.crear(conn, "/datos/dia2.txt", fecha="2026-09-02")
            memoria.aplicar_arrastres(
                conn, segunda, [{"action_id": accion, "estado": "bloqueada"}]
            )
            conn.commit()
            return [f["id"] for f in abiertas]

        self.assertEqual(procesar_dia2(), [accion], "deberia ofrecerse la accion")
        estado_1 = dict(conn.execute("SELECT * FROM actions").fetchone())

        self.assertEqual(procesar_dia2(), [accion], "y volver a ofrecerse al reprocesar")
        estado_2 = dict(conn.execute("SELECT * FROM actions").fetchone())

        self.assertEqual(estado_1, estado_2, "el reproceso debe ser idempotente")
        self.assertEqual(estado_2["menciones"], 2)
        self.assertEqual(estado_2["estado"], "bloqueada")


if __name__ == "__main__":
    unittest.main(verbosity=2)
