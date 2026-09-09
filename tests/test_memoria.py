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


class TestVistaDeReunion(BaseTemporal):
    """Las consultas que alimentan la vista de reunion (I2).

    Se comprueban contra las tablas y no contra `datos_json` a proposito: es la
    decision de la fase, y la que hara que una correccion humana (I6) se vea en
    la reunion donde se corrigio.
    """

    def poblar(self):
        conn = self.conectar()
        self.lunes = self.crear(conn, "/d/lunes.txt", fecha="2026-09-07")
        mapa = memoria.registrar_hablantes(
            conn,
            self.lunes,
            [
                {"etiqueta": "SPEAKER_00", "nombre": "Javi", "confianza": "alta"},
                # Nombre generico: no crea persona, y la etiqueta queda sin
                # resolver en vez de inventarse a alguien.
                {"etiqueta": "SPEAKER_01", "nombre": "no identificado"},
            ],
        )
        memoria.insertar_segmentos(
            conn,
            self.lunes,
            [
                {"texto": "uno", "inicio": 0.0, "fin": 1.0, "etiqueta": "SPEAKER_00"},
                {"texto": "dos", "inicio": 1.0, "fin": 2.0, "etiqueta": "SPEAKER_01"},
                {"texto": "tres", "inicio": 2.0, "fin": 3.0, "etiqueta": "SPEAKER_00"},
            ],
            mapa,
        )
        memoria.insertar_updates(
            conn, self.lunes, [{"persona": "Javi", "trabajo": "la API"}]
        )
        memoria.insertar_riesgos(
            conn, self.lunes, [{"descripcion": "se cae", "severidad": "alta"}]
        )
        memoria.insertar_acciones(
            conn, self.lunes, [{"descripcion": "Cerrar el informe", "persona": "Javi"}]
        )
        self.martes = self.crear(conn, "/d/martes.txt", fecha="2026-09-08")
        conn.commit()
        return conn

    def test_lo_de_cada_reunion_es_suyo(self):
        conn = self.poblar()
        self.assertEqual(len(memoria.updates_de_reunion(conn, self.lunes)), 1)
        self.assertEqual(len(memoria.riesgos_de_reunion(conn, self.lunes)), 1)
        self.assertEqual(len(memoria.acciones_de_reunion(conn, self.lunes)), 1)
        for consulta in (
            memoria.updates_de_reunion,
            memoria.riesgos_de_reunion,
            memoria.acciones_de_reunion,
            memoria.hablantes_de_reunion,
        ):
            with self.subTest(consulta=consulta.__name__):
                self.assertEqual(consulta(conn, self.martes), [])

    def test_hablante_sin_nombre_se_queda_sin_persona(self):
        conn = self.poblar()
        filas = {f["etiqueta"]: f for f in memoria.hablantes_de_reunion(conn, self.lunes)}
        self.assertEqual(filas["SPEAKER_00"]["persona"], "Javi")
        self.assertIsNone(filas["SPEAKER_01"]["persona"])

    def test_el_arrastre_sale_en_la_reunion_que_lo_menciono_y_no_en_la_suya(self):
        conn = self.poblar()
        accion = memoria.acciones_de_reunion(conn, self.lunes)[0]
        memoria.aplicar_arrastres(
            conn, self.martes, [{"action_id": accion["id"], "estado": "en_progreso"}]
        )
        conn.commit()

        # Nace el lunes: sigue siendo suya, aunque ahora la ultima mencion sea
        # el martes. Y el martes la ve como arrastre, no como propia.
        propia = memoria.acciones_de_reunion(conn, self.lunes)[0]
        self.assertEqual(propia["ultima_uid"], "martes")
        self.assertEqual(propia["estado"], "en_progreso")
        self.assertEqual(memoria.acciones_de_reunion(conn, self.martes), [])

        arrastres = memoria.arrastres_de_reunion(conn, self.martes)
        self.assertEqual([a["descripcion"] for a in arrastres], ["Cerrar el informe"])
        self.assertEqual(arrastres[0]["origen_uid"], "lunes")
        # La reunion donde nacio no se lista a si misma como arrastre.
        self.assertEqual(memoria.arrastres_de_reunion(conn, self.lunes), [])

    def test_estancada_con_el_mismo_umbral_que_el_markdown(self):
        conn = self.poblar()
        accion = memoria.acciones_de_reunion(conn, self.lunes)[0]
        self.assertFalse(accion["estancada"])
        conn.execute(
            "UPDATE actions SET menciones = ? WHERE id = ?",
            (memoria.UMBRAL_ESTANCAMIENTO, accion["id"]),
        )
        conn.commit()
        self.assertTrue(memoria.acciones_de_reunion(conn, self.lunes)[0]["estancada"])

    def test_una_accion_cerrada_no_esta_estancada(self):
        conn = self.poblar()
        conn.execute(
            "UPDATE actions SET menciones = ?, estado = 'completada'",
            (memoria.UMBRAL_ESTANCAMIENTO + 5,),
        )
        conn.commit()
        self.assertFalse(memoria.acciones_de_reunion(conn, self.lunes)[0]["estancada"])

    def test_segmentos_en_orden_y_paginados(self):
        conn = self.poblar()
        self.assertEqual(memoria.contar_segmentos(conn, self.lunes), 3)
        todos = memoria.segmentos_de_reunion(conn, self.lunes)
        self.assertEqual([s["texto"] for s in todos], ["uno", "dos", "tres"])
        # El hablante llega resuelto o crudo, segun se pudiera identificar.
        self.assertEqual(todos[0]["persona"], "Javi")
        self.assertIsNone(todos[1]["persona"])
        self.assertEqual(todos[1]["etiqueta"], "SPEAKER_01")

        pagina = memoria.segmentos_de_reunion(
            conn, self.lunes, limite=2, desplazamiento=2
        )
        self.assertEqual([s["idx"] for s in pagina], [2])


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

class TestTableroDeAcciones(BaseTemporal):
    """Las consultas del tablero (I3): filtrar, contar y ordenar.

    Todo lo que la vista ensena sale de aqui, asi que lo que se comprueba es
    justo lo que el plan pide de esa pantalla: que las estancadas se marquen
    con el mismo umbral que el `.md`, que los recuentos de los filtros no
    mientan y que un filtro mal escrito no devuelva una lista vacia.
    """

    def poblar(self):
        conn = self.conectar()
        self.junio = self.crear(conn, "/d/junio.txt", fecha="2026-06-01", titulo="Junio")
        self.julio = self.crear(conn, "/d/julio.txt", fecha="2026-07-01", titulo="Julio")
        memoria.insertar_acciones(
            conn,
            self.junio,
            [
                {"descripcion": "Revisar la migración de sesión", "persona": "Ana"},
                {"descripcion": "Cerrar el ticket al 100 %", "persona": None},
            ],
        )
        memoria.insertar_acciones(
            conn, self.julio, [{"descripcion": "Preparar la demo", "persona": "Bea"}]
        )
        self.vieja, self.sin_duenio, self.nueva = [
            fila["id"] for fila in conn.execute("SELECT id FROM actions ORDER BY id")
        ]
        # La de Ana se arrastra hasta julio y llega al umbral; la que no tiene
        # responsable se quedo en junio y se cerro alli mismo.
        conn.execute(
            "UPDATE actions SET menciones = 3, meeting_id_ultima = ? WHERE id = ?",
            (self.julio, self.vieja),
        )
        conn.execute(
            "UPDATE actions SET estado = 'completada', cerrada_en = '2026-06-01'"
            " WHERE id = ?",
            (self.sin_duenio,),
        )
        conn.commit()
        return conn

    def ids(self, filas):
        return sorted(fila["id"] for fila in filas)

    def test_devuelve_el_tramo_y_los_titulos_de_las_dos_reuniones(self):
        conn = self.poblar()
        fila = memoria.listar_acciones(conn, estados=["abierta"])[0]
        self.assertEqual(fila["id"], self.vieja)
        self.assertEqual(fila["origen_titulo"], "Junio")
        self.assertEqual(fila["ultima_titulo"], "Julio")
        self.assertEqual(fila["origen_fecha"], "2026-06-01")
        self.assertEqual(fila["ultima_fecha"], "2026-07-01")

    def test_estancada_con_el_mismo_umbral_que_el_markdown(self):
        conn = self.poblar()
        estancadas = memoria.listar_acciones(conn, estancadas=True)
        self.assertEqual(self.ids(estancadas), [self.vieja])
        self.assertTrue(estancadas[0]["estancada"])
        # La particion tiene que ser exacta: cada accion esta o no esta.
        resto = memoria.listar_acciones(conn, estancadas=False)
        self.assertEqual(self.ids(resto), sorted([self.sin_duenio, self.nueva]))

    def test_una_cerrada_con_muchas_menciones_no_esta_estancada(self):
        conn = self.poblar()
        conn.execute("UPDATE actions SET menciones = 9 WHERE id = ?", (self.sin_duenio,))
        conn.commit()
        self.assertEqual(
            self.ids(memoria.listar_acciones(conn, estancadas=True)), [self.vieja]
        )

    def test_texto_ignora_acentos_y_mayusculas(self):
        conn = self.poblar()
        for consulta in ("MIGRACION", "migración", "Sesion"):
            with self.subTest(consulta=consulta):
                self.assertEqual(
                    self.ids(memoria.listar_acciones(conn, texto=consulta)),
                    [self.vieja],
                )

    def test_los_comodines_del_texto_son_texto(self):
        """Quien teclea "100 %" busca eso, no "lo que sea"."""
        conn = self.poblar()
        self.assertEqual(
            self.ids(memoria.listar_acciones(conn, texto="100 %")), [self.sin_duenio]
        )
        self.assertEqual(memoria.listar_acciones(conn, texto="_____"), [])

    def test_sin_responsable_es_un_filtro_propio(self):
        conn = self.poblar()
        self.assertEqual(
            self.ids(memoria.listar_acciones(conn, persona=memoria.SIN_RESPONSABLE)),
            [self.sin_duenio],
        )
        # Y el nombre no distingue mayusculas, como el resto del modulo.
        self.assertEqual(
            self.ids(memoria.listar_acciones(conn, persona="ana")), [self.vieja]
        )

    def test_dias_sin_tocar_cuenta_desde_la_ultima_mencion(self):
        conn = self.poblar()
        hoy = conn.execute("SELECT date('now')").fetchone()[0]
        de_hoy = self.crear(conn, "/d/hoy.txt", fecha=hoy, titulo="Hoy")
        conn.execute(
            "UPDATE actions SET meeting_id_ultima = ? WHERE id = ?",
            (de_hoy, self.nueva),
        )
        conn.commit()
        dias = {f["id"]: f["dias_sin_tocar"] for f in memoria.listar_acciones(conn)}
        self.assertEqual(dias[self.nueva], 0)
        self.assertGreater(dias[self.vieja], 0)
        # El filtro es "al menos N dias", asi que lo mencionado hoy se queda
        # fuera y lo de hace meses entra.
        pendientes = self.ids(memoria.listar_acciones(conn, dias_sin_tocar=1))
        self.assertNotIn(self.nueva, pendientes)
        self.assertIn(self.vieja, pendientes)

    def test_el_periodo_incluye_lo_que_solapa(self):
        """Una accion de junio que se menciono en julio cuenta en julio."""
        conn = self.poblar()
        self.assertIn(
            self.vieja,
            self.ids(memoria.listar_acciones(conn, desde="2026-06-15")),
        )
        self.assertNotIn(
            self.nueva,
            self.ids(memoria.listar_acciones(conn, hasta="2026-06-30")),
        )

    def test_contar_es_el_total_del_filtro_no_el_de_la_pagina(self):
        conn = self.poblar()
        self.assertEqual(memoria.contar_acciones(conn), 3)
        self.assertEqual(len(memoria.listar_acciones(conn, limite=1)), 1)
        self.assertEqual(memoria.contar_acciones(conn, estados=["abierta"]), 2)

    def test_paginacion_sin_solapes_ni_huecos(self):
        conn = self.poblar()
        primera = memoria.listar_acciones(conn, limite=2)
        segunda = memoria.listar_acciones(conn, limite=2, desplazamiento=2)
        vistos = [f["id"] for f in primera] + [f["id"] for f in segunda]
        self.assertEqual(sorted(vistos), sorted([self.vieja, self.sin_duenio, self.nueva]))

    def test_los_recuentos_ignoran_su_propio_filtro(self):
        """Un chip pulsado tiene que seguir diciendo a donde lleva soltarlo."""
        conn = self.poblar()
        por_estado = memoria.acciones_por_estado(conn, estados=["completada"])
        self.assertEqual(por_estado["abierta"], 2)
        self.assertEqual(por_estado["completada"], 1)
        # Todos los estados aparecen: un cero explicito es informacion.
        self.assertEqual(set(por_estado), set(memoria.ESTADOS_ACCION))
        nombres = {
            f["persona"]: f["n"]
            for f in memoria.responsables_de_acciones(conn, persona="Ana")
        }
        self.assertEqual(nombres, {"Ana": 1, "Bea": 1, memoria.SIN_RESPONSABLE: 1})

    def test_los_recuentos_si_respetan_los_demas_filtros(self):
        conn = self.poblar()
        por_estado = memoria.acciones_por_estado(conn, persona="Ana")
        self.assertEqual(por_estado["abierta"], 1)
        self.assertEqual(por_estado["completada"], 0)

    def test_ordenes(self):
        conn = self.poblar()
        # Por defecto, lo estancado arriba.
        self.assertEqual(memoria.listar_acciones(conn)[0]["id"], self.vieja)
        antiguas = memoria.listar_acciones(conn, orden="antiguedad")
        self.assertEqual(antiguas[-1]["id"], self.nueva)
        recientes = memoria.listar_acciones(conn, orden="reciente")
        self.assertEqual(recientes[-1]["id"], self.sin_duenio)
        # Las que no tienen responsable van al final del orden por persona.
        self.assertEqual(
            memoria.listar_acciones(conn, orden="persona")[-1]["id"], self.sin_duenio
        )

    def test_un_orden_inventado_no_revienta_la_consulta(self):
        """`memoria` no valida entradas de usuario: eso es cosa de la API.

        Pero interpolar el ORDER BY obliga a que un valor desconocido caiga en
        el de por defecto y no acabe dentro del SQL.
        """
        conn = self.poblar()
        filas = memoria.listar_acciones(conn, orden="a.id; DROP TABLE actions")
        self.assertEqual(len(filas), 3)
        self.assertEqual(memoria.contar_acciones(conn), 3)

    def test_el_tablero_se_puede_leer_en_solo_lectura(self):
        """Es como lo abre la API, y el filtro de texto registra una funcion."""
        self.poblar()
        conn = self.conectar(solo_lectura=True)
        self.assertEqual(
            self.ids(memoria.listar_acciones(conn, texto="migracion")), [self.vieja]
        )


class TestBusqueda(BaseTemporal):
    """La busqueda de texto completo (I4), sobre `segments_fts`.

    Lo que se prueba es el criterio de aceptacion del plan -- un termino del
    glosario aparece con acentos o sin ellos y se puede llegar a su reunion --
    y, sobre todo, que lo que se teclea en una caja de busqueda no puede
    romper la consulta ni colarse como sintaxis de FTS5.
    """

    def poblar(self):
        conn = self.conectar()
        junio = self.crear(conn, "/d/junio.txt", fecha="2026-06-01", titulo="Junio")
        julio = self.crear(
            conn, "/d/julio.txt", fecha="2026-07-01", titulo="Julio", tipo="retro"
        )
        ana = memoria.obtener_o_crear_persona(conn, "Ana")
        memoria.insertar_segmentos(
            conn,
            junio,
            [
                {
                    "texto": "La sesión de refactorización quedó a medias",
                    "inicio": 0.0,
                    "fin": 4.0,
                    "etiqueta": "SPEAKER_00",
                },
                {
                    "texto": "El certificado de pre caduca el viernes",
                    "inicio": 4.0,
                    "fin": 8.0,
                },
            ],
            {"SPEAKER_00": ana},
        )
        memoria.insertar_segmentos(
            conn,
            julio,
            [{"texto": "Retomamos la sesion de refactorizacion", "inicio": 0.0, "fin": 3.0}],
        )
        conn.commit()
        return conn

    def buscar(self, conn, texto, **kwargs):
        return memoria.buscar_segmentos(conn, memoria.consulta_fts(texto), **kwargs)

    # --- la traduccion de lo tecleado a sintaxis FTS5 ---

    def test_cada_palabra_se_cita(self):
        self.assertEqual(
            memoria.consulta_fts("despliegue certificado"), '"despliegue" "certificado"'
        )

    def test_frase_entre_comillas_y_prefijo(self):
        self.assertEqual(memoria.consulta_fts('"entorno de pre"'), '"entorno de pre"')
        self.assertEqual(memoria.consulta_fts("refact*"), '"refact"*')

    def test_sin_nada_buscable_devuelve_none(self):
        """Distinto de "no hay resultados": no habia nada que buscar."""
        for texto in ("", None, "   ", "***", "?!-"):
            with self.subTest(texto=texto):
                self.assertIsNone(memoria.consulta_fts(texto))

    def test_los_operadores_de_fts5_se_buscan_como_texto(self):
        """Ni un error de sintaxis ni un operador colado: se busca lo tecleado."""
        conn = self.poblar()
        for texto in ("sesión OR (certificado", 'NEAR("x" "y")', 'sesión"', "a-b_c"):
            with self.subTest(texto=texto):
                consulta = memoria.consulta_fts(texto)
                self.assertIsNotNone(consulta)
                memoria.buscar_segmentos(conn, consulta)  # no lanza

    # --- lo que devuelve ---

    def test_los_acentos_dan_igual_en_los_dos_sentidos(self):
        conn = self.poblar()
        for texto in ("sesion", "sesión", "SESIÓN"):
            with self.subTest(texto=texto):
                self.assertEqual(len(self.buscar(conn, texto)), 2)

    def test_no_hay_stemming_pero_si_prefijo(self):
        """Se dice en pantalla porque se nota: `refactor` no encuentra nada."""
        conn = self.poblar()
        self.assertEqual(len(self.buscar(conn, "refactor")), 0)
        self.assertEqual(len(self.buscar(conn, "refactor*")), 2)

    def test_dos_palabras_son_una_y_implicita(self):
        conn = self.poblar()
        self.assertEqual(len(self.buscar(conn, "certificado viernes")), 1)
        self.assertEqual(len(self.buscar(conn, "certificado sesion")), 0)

    def test_devuelve_la_reunion_y_el_ancla_del_segmento(self):
        conn = self.poblar()
        fila = self.buscar(conn, "certificado")[0]
        self.assertEqual(fila["uid"], "junio")
        self.assertEqual(fila["titulo"], "Junio")
        self.assertEqual(fila["idx"], 1)
        self.assertEqual(fila["inicio"], 4.0)

    def test_el_hablante_viaja_con_la_coincidencia(self):
        conn = self.poblar()
        fila = self.buscar(conn, "refactorización", uid="junio")[0]
        self.assertEqual(fila["persona"], "Ana")
        self.assertEqual(fila["etiqueta"], "SPEAKER_00")

    def test_el_fragmento_marca_el_termino(self):
        conn = self.poblar()
        fragmento = self.buscar(conn, "certificado")[0]["fragmento"]
        self.assertIn(
            memoria.MARCA_INICIO + "certificado" + memoria.MARCA_FIN, fragmento
        )

    def test_filtros_de_periodo_tipo_y_reunion(self):
        conn = self.poblar()
        self.assertEqual(len(self.buscar(conn, "sesion", desde="2026-07-01")), 1)
        self.assertEqual(len(self.buscar(conn, "sesion", hasta="2026-06-30")), 1)
        self.assertEqual(len(self.buscar(conn, "sesion", tipo="retro")), 1)
        self.assertEqual(len(self.buscar(conn, "sesion", uid="junio")), 1)

    def test_el_desglose_por_reunion_ignora_el_filtro_de_reunion(self):
        """Acotar a una reunion no puede borrar la lista desde la que se acota."""
        conn = self.poblar()
        filas = memoria.reuniones_de_busqueda(
            conn, memoria.consulta_fts("sesion"), uid="junio"
        )
        self.assertEqual([f["uid"] for f in filas], ["julio", "junio"])
        self.assertEqual([f["n"] for f in filas], [1, 1])
        # El resto de filtros si se respetan: son lo que se esta mirando.
        filas = memoria.reuniones_de_busqueda(
            conn, memoria.consulta_fts("sesion"), tipo="retro"
        )
        self.assertEqual([f["uid"] for f in filas], ["julio"])

    def test_contar_no_depende_de_la_pagina(self):
        conn = self.poblar()
        consulta = memoria.consulta_fts("sesion")
        self.assertEqual(memoria.contar_busqueda(conn, consulta), 2)
        self.assertEqual(len(memoria.buscar_segmentos(conn, consulta, limite=1)), 1)
        segunda = memoria.buscar_segmentos(conn, consulta, limite=1, desplazamiento=1)
        self.assertEqual(len(segunda), 1)

    def test_ordenes_por_fecha(self):
        conn = self.poblar()
        consulta = memoria.consulta_fts("sesion")
        recientes = memoria.buscar_segmentos(conn, consulta, orden="reciente")
        self.assertEqual([f["fecha"] for f in recientes], ["2026-07-01", "2026-06-01"])
        antiguas = memoria.buscar_segmentos(conn, consulta, orden="antiguo")
        self.assertEqual([f["fecha"] for f in antiguas], ["2026-06-01", "2026-07-01"])

    def test_un_orden_desconocido_no_llega_al_sql(self):
        conn = self.poblar()
        filas = memoria.buscar_segmentos(
            conn, memoria.consulta_fts("sesion"), orden="s.idx; DROP TABLE meetings"
        )
        self.assertEqual(len(filas), 2)
        self.assertTrue(conn.execute("SELECT count(*) FROM meetings").fetchone()[0])

    def test_se_puede_buscar_en_solo_lectura(self):
        """La API abre asi: si `MATCH` necesitara escribir, no serviria."""
        self.poblar().close()
        conn = self.conectar(solo_lectura=True)
        self.assertEqual(len(self.buscar(conn, "certificado")), 1)

    def test_reprocesar_no_deja_fantasmas_en_el_indice(self):
        """La tabla FTS es de contenido externo: la sincronizan tres triggers."""
        conn = self.poblar()
        meeting_id = self.crear(
            conn, "/d/junio.txt", fecha="2026-06-01", titulo="Junio otra vez"
        )
        memoria.insertar_segmentos(
            conn, meeting_id, [{"texto": "Ahora hablamos de otra cosa", "inicio": 0.0}]
        )
        conn.commit()
        self.assertEqual(len(self.buscar(conn, "certificado")), 0)
        self.assertEqual(len(self.buscar(conn, "cosa")), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
