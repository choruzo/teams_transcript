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


class TestMetricas(BaseTemporal):
    """I1: los agregados del panel de salud, que salen de SQL y no del LLM."""

    def poblar(self, conn):
        # Dos semanas ISO distintas, una reunion sin duracion (D9) y una accion
        # arrastrada tres veces, que es el caso que la interfaz llama estancada.
        uno = self.crear(
            conn, "/datos/dia1.txt", fecha="2026-09-01", duracion_seg=1800
        )
        memoria.insertar_acciones(
            conn,
            uno,
            [
                {"descripcion": "Arrastrada", "persona": "Lara"},
                {"descripcion": "Suelta", "persona": "Pedro"},
            ],
        )
        memoria.insertar_riesgos(
            conn,
            uno,
            [
                {"descripcion": "R1", "area": "ULS", "severidad": "alta"},
                {"descripcion": "R2", "area": "", "severidad": "media"},
            ],
        )
        memoria.insertar_updates(
            conn, uno, [{"persona": "Lara", "trabajo": "algo"}]
        )
        dos = self.crear(conn, "/datos/dia2.txt", fecha="2026-09-08")
        tres = self.crear(
            conn, "/datos/dia3.txt", fecha="2026-09-09", duracion_seg=600
        )
        arrastrada = conn.execute(
            "SELECT id FROM actions WHERE descripcion = 'Arrastrada'"
        ).fetchone()["id"]
        for meeting_id in (dos, tres):
            memoria.aplicar_arrastres(
                conn, meeting_id, [{"action_id": arrastrada, "estado": "bloqueada"}]
            )
        conn.commit()
        return arrastrada

    def test_alcance_de_cada_bloque(self):
        """Las acciones son de hoy y de todo el historico; el resto, del periodo."""
        conn = self.conectar()
        self.poblar(conn)
        recorte = memoria.metricas(conn, desde="2026-09-08", hasta="2026-09-09")
        self.assertEqual(recorte["reuniones"], 2, "las reuniones si se filtran")
        self.assertEqual(
            recorte["acciones_abiertas"], 2, "las acciones no dependen del periodo"
        )
        self.assertEqual(recorte["riesgos"]["total"], 0, "los riesgos si")

    def test_estancadas_usan_el_umbral_compartido(self):
        conn = self.conectar()
        self.poblar(conn)
        datos = memoria.metricas(conn)
        self.assertEqual(datos["umbral_estancamiento"], memoria.UMBRAL_ESTANCAMIENTO)
        self.assertEqual(datos["estancadas"], 1, "3 menciones sin cerrar")
        self.assertEqual(datos["acciones"]["bloqueada"], 1)
        self.assertEqual(datos["acciones"]["abierta"], 1)

    def test_semanas_iso_y_duracion_desconocida(self):
        conn = self.conectar()
        self.poblar(conn)
        datos = memoria.metricas(conn)
        semanas = {s["semana"]: s for s in datos["semanas"]}
        self.assertEqual(sorted(semanas), ["2026-W36", "2026-W37"])
        self.assertEqual(semanas["2026-W36"]["minutos"], 30)
        # D9: la reunion sin duracion se cuenta aparte en vez de sumar cero en
        # silencio, para que la interfaz pueda advertirlo.
        self.assertEqual(datos["reuniones_sin_duracion"], 1)
        self.assertEqual(semanas["2026-W37"]["sin_duracion"], 1)
        self.assertEqual(semanas["2026-W37"]["minutos"], 10)

    def test_riesgos_agrupados_sin_hablar_de_abiertos(self):
        conn = self.conectar()
        self.poblar(conn)
        riesgos = memoria.metricas(conn)["riesgos"]
        self.assertEqual(riesgos["total"], 2)
        self.assertEqual(riesgos["por_severidad"], {"alta": 1, "media": 1})
        self.assertIn({"area": "ULS", "n": 1}, riesgos["por_area"])
        self.assertIn({"area": "sin área", "n": 1}, riesgos["por_area"])

    def test_personas_sin_actividad_no_aparecen(self):
        conn = self.conectar()
        self.poblar(conn)
        memoria.obtener_o_crear_persona(conn, "Fantasma")
        conn.commit()
        nombres = [p["persona"] for p in memoria.metricas(conn)["personas"]]
        self.assertIn("Lara", nombres)
        self.assertNotIn("Fantasma", nombres, "sin roster (D7) no se inventa gente")

    def test_base_vacia_no_revienta(self):
        """El estado inicial de un despliegue nuevo es una base sin nada."""
        datos = memoria.metricas(self.conectar())
        self.assertEqual(datos["reuniones"], 0)
        self.assertEqual(datos["acciones_abiertas"], 0)
        self.assertEqual(datos["semanas"], [])
        self.assertEqual(datos["riesgos"]["total"], 0)


class TestCarriles(BaseTemporal):
    """I1: cada accion como un tramo entre su origen y su ultima mencion."""

    def preparar(self, conn):
        uno = self.crear(conn, "/datos/dia1.txt", fecha="2026-09-01")
        memoria.insertar_acciones(
            conn, uno, [{"descripcion": "Arrastrada", "persona": "Lara"}]
        )
        accion = conn.execute("SELECT id FROM actions").fetchone()["id"]
        for fecha in ("2026-09-02", "2026-09-03"):
            otra = self.crear(conn, f"/datos/{fecha}.txt", fecha=fecha)
            memoria.aplicar_arrastres(
                conn, otra, [{"action_id": accion, "estado": "en_progreso"}]
            )
        conn.commit()
        return accion

    def test_tramo_de_origen_a_ultima_mencion(self):
        conn = self.conectar()
        self.preparar(conn)
        carril = memoria.carriles_acciones(conn)[0]
        self.assertEqual(carril["origen_fecha"], "2026-09-01")
        self.assertEqual(carril["ultima_fecha"], "2026-09-03")
        self.assertEqual(carril["menciones"], 3)
        self.assertTrue(carril["estancada"])
        self.assertEqual(carril["persona"], "Lara")

    def test_una_accion_sin_arrastres_es_un_punto(self):
        conn = self.conectar()
        meeting_id = self.crear(conn, "/datos/solo.txt", fecha="2026-09-01")
        memoria.insertar_acciones(conn, meeting_id, [{"descripcion": "Sola"}])
        conn.commit()
        carril = memoria.carriles_acciones(conn)[0]
        self.assertEqual(carril["origen_fecha"], carril["ultima_fecha"])
        self.assertFalse(carril["estancada"])

    def test_se_devuelven_las_que_solapan_el_periodo(self):
        """Una accion vieja que sigue viva es justo la que hay que ver hoy."""
        conn = self.conectar()
        self.preparar(conn)
        dentro = memoria.carriles_acciones(conn, desde="2026-09-03")
        self.assertEqual(len(dentro), 1, "nacio antes, pero llega hasta aqui")
        fuera = memoria.carriles_acciones(conn, desde="2026-09-04")
        self.assertEqual(fuera, [])

    def test_solo_abiertas_excluye_las_cerradas(self):
        conn = self.conectar()
        accion = self.preparar(conn)
        cierre = self.crear(conn, "/datos/cierre.txt", fecha="2026-09-04")
        memoria.aplicar_arrastres(
            conn, cierre, [{"action_id": accion, "estado": "completada"}]
        )
        conn.commit()
        self.assertEqual(len(memoria.carriles_acciones(conn)), 1)
        self.assertEqual(memoria.carriles_acciones(conn, solo_abiertas=True), [])


class TestConexionEntreHilos(BaseTemporal):
    """La API abre en un hilo del pool y puede cerrar en otro."""

    def test_cerrar_desde_otro_hilo(self):
        import threading

        memoria.conectar(self.db).close()  # crear el fichero
        conn = memoria.conectar(self.db, solo_lectura=True, entre_hilos=True)
        fallo = []

        def usar_y_cerrar():
            try:
                conn.execute("SELECT count(*) FROM meetings").fetchone()
                conn.close()
            except Exception as exc:  # noqa: BLE001
                fallo.append(exc)

        hilo = threading.Thread(target=usar_y_cerrar)
        hilo.start()
        hilo.join()
        self.assertEqual(fallo, [], "sin entre_hilos esto aborta con ProgrammingError")

    def test_por_defecto_sigue_comprobando_el_hilo(self):
        """El pipeline no lo necesita y la comprobacion es una red de seguridad."""
        import threading

        conn = self.conectar()
        fallo = []

        def usar():
            try:
                conn.execute("SELECT 1")
            except Exception as exc:  # noqa: BLE001
                fallo.append(exc)

        hilo = threading.Thread(target=usar)
        hilo.start()
        hilo.join()
        self.assertIsInstance(fallo[0], sqlite3.ProgrammingError)


if __name__ == "__main__":
    unittest.main(verbosity=2)
