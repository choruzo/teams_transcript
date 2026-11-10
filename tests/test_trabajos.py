"""Tests de la cola de trabajos (I10b): todo stdlib, sin FastAPI ni GPU."""

import os
import sqlite3
import struct
import tempfile
import unittest
import wave
from datetime import datetime, timedelta
from pathlib import Path

import trabajos


def escribir_wav(ruta: Path, *, segundos: float = 1.0, frecuencia: int = 8000) -> Path:
    """Un WAV PCM valido y pequeno, del tamano que haga falta."""
    with wave.open(str(ruta), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(frecuencia)
        w.writeframes(b"\x00\x00" * int(frecuencia * segundos))
    return ruta


class BaseTrabajos(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.raiz = Path(self._tmp.name)
        self.db = self.raiz / "trabajos.db"
        self.subidas = self.raiz / "subidas"
        self.grabaciones = self.raiz / "grabaciones"
        self._subidas_previa = os.environ.get("TEAMS_SUBIDAS_DIR")
        os.environ["TEAMS_SUBIDAS_DIR"] = str(self.subidas)
        self.addCleanup(self._restaurar_subidas)
        self.conn = trabajos.conectar(self.db)
        self.addCleanup(self.conn.close)

    def _restaurar_subidas(self):
        if self._subidas_previa is None:
            os.environ.pop("TEAMS_SUBIDAS_DIR", None)
        else:
            os.environ["TEAMS_SUBIDAS_DIR"] = self._subidas_previa

    def crear(self, **cambios):
        datos = {
            "nombre_original": "20261110_090000_mixed.wav",
            "tamano": 8,
            "fecha_reunion": "2026-11-10",
            "hora_reunion": "09:00",
            "trozo": 4,
            "ruta_subidas_directorio": self.subidas,
        }
        datos.update(cambios)
        return trabajos.crear(self.conn, **datos)

    def subir_todo(self, trabajo, datos: bytes, trozo: int):
        for indice in range(0, len(datos), trozo):
            trabajos.anotar_parte(
                self.conn, trabajo["uid"], indice // trozo, datos[indice : indice + trozo]
            )


class TestCreacion(BaseTrabajos):
    def test_crea_un_trabajo_y_su_parcial(self):
        trabajo = self.crear()
        self.assertEqual(trabajo["estado"], "subiendo")
        self.assertEqual(trabajo["bytes_confirmados"], 0)
        self.assertEqual(trabajo["trozos"], 2)
        parcial = trabajos.ruta_parcial(trabajo, self.subidas)
        self.assertTrue(parcial.exists())
        self.assertEqual(parcial.stat().st_size, 0)

    def test_rechaza_lo_que_no_es_wav(self):
        with self.assertRaises(trabajos.TrabajoInvalido):
            self.crear(nombre_original="daily.mp3")

    def test_rechaza_tamano_cero_o_negativo(self):
        for tamano in (0, -1):
            with self.assertRaises(trabajos.TrabajoInvalido):
                self.crear(tamano=tamano)

    def test_rechaza_un_tipo_desconocido(self):
        with self.assertRaises(trabajos.TrabajoInvalido):
            self.crear(tipo="juntanza")

    def test_rechaza_fecha_u_hora_mal_formadas(self):
        with self.assertRaises(trabajos.TrabajoInvalido):
            self.crear(fecha_reunion="10/11/2026")
        with self.assertRaises(trabajos.TrabajoInvalido):
            self.crear(hora_reunion="9:00")

    def test_rechaza_una_fecha_u_hora_que_no_existen(self):
        with self.assertRaises(trabajos.TrabajoInvalido):
            self.crear(fecha_reunion="2026-99-99")
        with self.assertRaises(trabajos.TrabajoInvalido):
            self.crear(hora_reunion="25:00")

    def test_el_uid_es_unico(self):
        uno = self.crear()
        otro = self.crear()
        self.assertNotEqual(uno["uid"], otro["uid"])


class TestTroceado(BaseTrabajos):
    def test_sube_en_orden_y_reanuda(self):
        trabajo = self.crear(tamano=10, trozo=4)
        datos = b"0123456789"
        self.assertEqual(
            trabajos.anotar_parte(self.conn, trabajo["uid"], 0, datos[:4])["siguiente"],
            1,
        )
        estado = trabajos.anotar_parte(self.conn, trabajo["uid"], 1, datos[4:8])
        self.assertEqual(estado["recibidos"], 8)
        self.assertEqual(estado["siguiente"], 2)
        estado = trabajos.anotar_parte(self.conn, trabajo["uid"], 2, datos[8:])
        self.assertTrue(estado["completo"])
        self.assertEqual(
            trabajos.ruta_parcial(trabajo, self.subidas).read_bytes(), datos
        )

    def test_reenviar_un_trozo_confirmado_es_idempotente(self):
        trabajo = self.crear(tamano=8, trozo=4)
        trabajos.anotar_parte(self.conn, trabajo["uid"], 0, b"aaaa")
        estado = trabajos.anotar_parte(self.conn, trabajo["uid"], 0, b"aaaa")
        self.assertEqual(estado["recibidos"], 4)

    def test_reenviar_otro_contenido_en_un_trozo_confirmado_falla(self):
        trabajo = self.crear(tamano=8, trozo=4)
        trabajos.anotar_parte(self.conn, trabajo["uid"], 0, b"aaaa")
        with self.assertRaises(trabajos.TrabajoInvalido):
            trabajos.anotar_parte(self.conn, trabajo["uid"], 0, b"bbbb")

    def test_trozo_fuera_de_orden(self):
        trabajo = self.crear(tamano=12, trozo=4)
        with self.assertRaises(trabajos.ParteFueraDeOrden) as contexto:
            trabajos.anotar_parte(self.conn, trabajo["uid"], 1, b"bbbb")
        self.assertEqual(contexto.exception.esperado, 0)

    def test_tamano_de_trozo_incorrecto(self):
        trabajo = self.crear(tamano=12, trozo=4)
        with self.assertRaises(trabajos.TrabajoInvalido):
            trabajos.anotar_parte(self.conn, trabajo["uid"], 0, b"bbb")

    def test_hash_del_trozo(self):
        import hashlib

        trabajo = self.crear(tamano=8, trozo=4)
        bueno = hashlib.sha256(b"aaaa").hexdigest()
        trabajos.anotar_parte(
            self.conn, trabajo["uid"], 0, b"aaaa", sha256=bueno
        )
        with self.assertRaises(trabajos.TrabajoInvalido):
            trabajos.anotar_parte(
                self.conn, trabajo["uid"], 1, b"bbbb", sha256="0" * 64
            )

    def test_si_el_parcial_crecio_de_mas_se_trunca(self):
        trabajo = self.crear(tamano=8, trozo=4)
        parcial = trabajos.ruta_parcial(trabajo, self.subidas)
        parcial.write_bytes(b"basura")
        trabajos.anotar_parte(self.conn, trabajo["uid"], 0, b"aaaa")
        self.assertEqual(parcial.read_bytes(), b"aaaa")

    def test_si_el_parcial_encogio_se_rechaza(self):
        trabajo = self.crear(tamano=8, trozo=4)
        trabajos.anotar_parte(self.conn, trabajo["uid"], 0, b"aaaa")
        trabajos.ruta_parcial(trabajo, self.subidas).write_bytes(b"a")
        with self.assertRaises(trabajos.TrabajoInvalido):
            trabajos.anotar_parte(self.conn, trabajo["uid"], 1, b"bbbb")

    def test_no_se_sube_a_un_trabajo_ya_encolado(self):
        trabajo = self.crear(tamano=8, trozo=4)
        trabajos.marcar(self.conn, trabajo["uid"], "encolado")
        with self.assertRaises(trabajos.TrabajoInvalido):
            trabajos.anotar_parte(self.conn, trabajo["uid"], 0, b"aaaa")


class TestFinalizar(BaseTrabajos):
    def _trabajo_con_wav(self, segundos=0.05):
        # Primero se sabe el tamano, luego se crea el trabajo.
        ruta = self.raiz / "origen.wav"
        escribir_wav(ruta, segundos=segundos)
        datos = ruta.read_bytes()
        trabajo = self.crear(tamano=len(datos), trozo=4096)
        self.subir_todo(trabajo, datos, 4096)
        return trabajo, datos

    def test_finaliza_un_wav_valido_y_calcula_duracion_y_hash(self):
        trabajo, datos = self._trabajo_con_wav(segundos=1)
        final = trabajos.finalizar(self.conn, trabajo["uid"])
        self.assertEqual(final["estado"], "encolado")
        self.assertGreater(final["duracion_seg"], 0.9)
        self.assertEqual(final["sha256"], __import__("hashlib").sha256(datos).hexdigest())

    def test_finalizar_dos_veces_es_idempotente(self):
        trabajo, _ = self._trabajo_con_wav()
        uno = trabajos.finalizar(self.conn, trabajo["uid"])
        otro = trabajos.finalizar(self.conn, trabajo["uid"])
        self.assertEqual(uno["uid"], otro["uid"])
        self.assertEqual(otro["estado"], "encolado")

    def test_no_finaliza_si_faltan_bytes(self):
        trabajo = self.crear(tamano=8, trozo=4)
        trabajos.anotar_parte(self.conn, trabajo["uid"], 0, b"aaaa")
        with self.assertRaises(trabajos.SubidaIncompleta):
            trabajos.finalizar(self.conn, trabajo["uid"])

    def test_no_finaliza_un_fichero_que_no_es_wav(self):
        datos = b"no soy un wav" * 10
        trabajo = self.crear(tamano=len(datos), trozo=64)
        self.subir_todo(trabajo, datos, 64)
        with self.assertRaises(trabajos.TrabajoInvalido):
            trabajos.finalizar(self.conn, trabajo["uid"])
        self.assertEqual(
            trabajos.detalle(self.conn, trabajo["uid"])["estado"], "subiendo"
        )

    def test_detecta_el_duplicado_y_permite_forzar(self):
        primero, datos = self._trabajo_con_wav()
        trabajos.finalizar(self.conn, primero["uid"])
        segundo = self.crear(tamano=len(datos), trozo=4096)
        self.subir_todo(segundo, datos, 4096)
        with self.assertRaises(trabajos.TrabajoDuplicado) as contexto:
            trabajos.finalizar(self.conn, segundo["uid"])
        self.assertEqual(contexto.exception.uid, primero["uid"])
        forzado = trabajos.finalizar(self.conn, segundo["uid"], forzar=True)
        self.assertEqual(forzado["estado"], "encolado")

    def test_valida_wav_extensible(self):
        # WAVE_FORMAT_EXTENSIBLE (0xFFFE), que `wave.open` rechaza.
        datos = b"\x00\x00" * 100
        fmt = struct.pack(
            "<HHIIHH", 0xFFFE, 1, 8000, 16000, 2, 16
        ) + struct.pack("<H", 22) + b"\x00" * 22
        cuerpo = (
            b"WAVE"
            + b"fmt " + struct.pack("<I", len(fmt)) + fmt
            + b"data" + struct.pack("<I", len(datos)) + datos
        )
        ruta = self.raiz / "extensible.wav"
        ruta.write_bytes(b"RIFF" + struct.pack("<I", len(cuerpo)) + cuerpo)
        info = trabajos.validar_wav(ruta)
        self.assertEqual(info["canales"], 1)
        self.assertGreater(info["duracion_seg"], 0)

    def test_rechaza_un_wav_cortado(self):
        ruta = self.raiz / "cortado.wav"
        escribir_wav(ruta, segundos=0.1)
        datos = ruta.read_bytes()[:-100]
        ruta.write_bytes(datos)
        with self.assertRaises(trabajos.TrabajoInvalido):
            trabajos.validar_wav(ruta)


class TestCola(BaseTrabajos):
    def _encolado(self):
        ruta = self.raiz / "origen.wav"
        escribir_wav(ruta, segundos=0.05)
        datos = ruta.read_bytes()
        trabajo = self.crear(tamano=len(datos), trozo=4096)
        self.subir_todo(trabajo, datos, 4096)
        return trabajos.finalizar(self.conn, trabajo["uid"])

    def test_reclamar_solo_una_vez(self):
        trabajo = self._encolado()
        tomado = trabajos.reclamar(self.conn)
        self.assertEqual(tomado["uid"], trabajo["uid"])
        self.assertEqual(tomado["estado"], "preparando")
        self.assertIsNone(trabajos.reclamar(self.conn))

    def test_marcar_actualiza_estado_y_campos(self):
        trabajo = self._encolado()
        trabajos.reclamar(self.conn)
        actualizado = trabajos.marcar(
            self.conn, trabajo["uid"], "transcribiendo", paso="transcribiendo"
        )
        self.assertEqual(actualizado["estado"], "transcribiendo")
        self.assertEqual(actualizado["paso"], "transcribiendo")

    def test_marcar_rechaza_estados_y_campos_desconocidos(self):
        trabajo = self._encolado()
        with self.assertRaises(trabajos.TrabajoInvalido):
            trabajos.marcar(self.conn, trabajo["uid"], "inventado")
        with self.assertRaises(trabajos.TrabajoInvalido):
            trabajos.marcar(self.conn, trabajo["uid"], "error", ruta_parcial="/x")

    def test_marcar_un_uid_inexistente(self):
        with self.assertRaises(trabajos.TrabajoNoEncontrado):
            trabajos.marcar(self.conn, "no-existe", "error")

    def test_cancelar_en_cola(self):
        trabajo = self._encolado()
        cancelado = trabajos.cancelar(self.conn, trabajo["uid"])
        self.assertEqual(cancelado["estado"], "cancelado")
        self.assertIsNone(trabajos.reclamar(self.conn))

    def test_cancelar_en_curso_solo_pone_la_bandera(self):
        trabajo = self._encolado()
        trabajos.reclamar(self.conn)
        pedido = trabajos.cancelar(self.conn, trabajo["uid"])
        self.assertEqual(pedido["estado"], "preparando")
        self.assertEqual(pedido["cancelar"], 1)

    def test_cancelar_una_subida_borra_el_parcial(self):
        trabajo = self.crear(tamano=8, trozo=4)
        parcial = trabajos.ruta_parcial(trabajo, self.subidas)
        trabajos.cancelar(self.conn, trabajo["uid"])
        self.assertFalse(parcial.exists())

    def test_no_se_cancela_lo_terminado(self):
        trabajo = self._encolado()
        trabajos.marcar(self.conn, trabajo["uid"], "hecho")
        with self.assertRaises(trabajos.TrabajoInvalido):
            trabajos.cancelar(self.conn, trabajo["uid"])

    def test_reintentar_solo_lo_que_fallo_y_con_audio(self):
        trabajo = self._encolado()
        audio = self.raiz / "audio.wav"
        audio.write_bytes(b"x")
        trabajos.marcar(
            self.conn, trabajo["uid"], "error", audio_path=str(audio), mensaje="boom"
        )
        de_nuevo = trabajos.reintentar(self.conn, trabajo["uid"])
        self.assertEqual(de_nuevo["estado"], "encolado")
        self.assertIsNone(de_nuevo["mensaje"])

    def test_reintentar_sin_audio_no_puede(self):
        trabajo = self._encolado()
        trabajos.ruta_parcial(trabajo, self.subidas).unlink()
        trabajos.marcar(self.conn, trabajo["uid"], "error")
        with self.assertRaises(trabajos.TrabajoInvalido):
            trabajos.reintentar(self.conn, trabajo["uid"])

    def test_reintentar_antes_de_mover_el_audio(self):
        """Un fallo al preparar el audio no obliga a volver a subirlo."""
        trabajo = self._encolado()
        trabajos.marcar(self.conn, trabajo["uid"], "error", mensaje="fallo")
        de_nuevo = trabajos.reintentar(self.conn, trabajo["uid"])
        self.assertEqual(de_nuevo["estado"], "encolado")
        self.assertIsNone(de_nuevo["audio_path"])

    def test_marcar_interrumpidos_al_arrancar(self):
        trabajo = self._encolado()
        trabajos.reclamar(self.conn)
        uids = trabajos.marcar_interrumpidos(self.conn)
        self.assertEqual(uids, [trabajo["uid"]])
        fila = trabajos.detalle(self.conn, trabajo["uid"])
        self.assertEqual(fila["estado"], "interrumpido")
        self.assertIn("detuvo", fila["mensaje"])

    def test_latido_y_estado_del_trabajador(self):
        self.assertIsNone(trabajos.estado_trabajador(self.conn))
        trabajos.latido(self.conn)
        vivo, fila = trabajos.trabajador_vivo(self.conn)
        self.assertTrue(vivo)
        self.assertEqual(fila["pid"], os.getpid())

    def test_un_latido_viejo_no_esta_vivo(self):
        trabajos.latido(self.conn)
        viejo = (datetime.now() - timedelta(seconds=120)).strftime("%Y-%m-%d %H:%M:%S")
        with self.conn:
            self.conn.execute("UPDATE trabajador SET latido_en = ?", (viejo,))
        vivo, _ = trabajos.trabajador_vivo(self.conn)
        self.assertFalse(vivo)

    def test_limpiar_parciales_abandonados(self):
        trabajo = self.crear(tamano=8, trozo=4)
        viejo = (datetime.now() - timedelta(hours=48)).strftime("%Y-%m-%d %H:%M:%S")
        with self.conn:
            self.conn.execute(
                "UPDATE trabajos SET actualizado_en = ? WHERE uid = ?",
                (viejo, trabajo["uid"]),
            )
        borrados = trabajos.limpiar_parciales(self.conn, horas=24)
        self.assertEqual(borrados, 1)
        self.assertFalse(trabajos.ruta_parcial(trabajo, self.subidas).exists())
        with self.assertRaises(trabajos.TrabajoNoEncontrado):
            trabajos.detalle(self.conn, trabajo["uid"])

    def test_no_limpia_lo_reciente(self):
        trabajo = self.crear(tamano=8, trozo=4)
        self.assertEqual(trabajos.limpiar_parciales(self.conn, horas=24), 0)
        self.assertTrue(trabajos.ruta_parcial(trabajo, self.subidas).exists())


class TestNombres(BaseTrabajos):
    def test_respeta_un_nombre_con_el_patron(self):
        trabajo = self.crear()
        destino = trabajos.nombre_destino(trabajo, self.grabaciones)
        self.assertEqual(destino.name, "20261110_090000_mixed.wav")

    def test_genera_el_nombre_con_la_fecha_elegida(self):
        trabajo = self.crear(
            nombre_original="reunion de equipo.wav",
            fecha_reunion="2026-11-05",
            hora_reunion="14:30",
        )
        destino = trabajos.nombre_destino(trabajo, self.grabaciones)
        self.assertEqual(destino.name, "20261105_143000_mixed.wav")

    def test_no_pisa_un_nombre_ocupado(self):
        self.grabaciones.mkdir()
        (self.grabaciones / "20261110_090000_mixed.wav").write_bytes(b"x")
        trabajo = self.crear()
        destino = trabajos.nombre_destino(trabajo, self.grabaciones)
        self.assertEqual(destino.name, "20261110_090000_mixed-2.wav")

    def test_mover_a_grabaciones(self):
        ruta = self.raiz / "origen.wav"
        escribir_wav(ruta, segundos=0.05)
        datos = ruta.read_bytes()
        trabajo = self.crear(tamano=len(datos), trozo=4096)
        self.subir_todo(trabajo, datos, 4096)
        trabajos.finalizar(self.conn, trabajo["uid"])
        destino = trabajos.mover_a_grabaciones(trabajo, self.grabaciones)
        self.assertTrue(destino.exists())
        self.assertFalse(trabajos.ruta_parcial(trabajo, self.subidas).exists())
        self.assertEqual(destino.read_bytes(), datos)


class TestLogs(BaseTrabajos):
    def test_leer_log_inexistente_no_falla(self):
        texto, tamano = trabajos.leer_log("no-existe")
        self.assertEqual(texto, "")
        self.assertEqual(tamano, 0)

    def test_lee_solo_el_final(self):
        uid = "20261110_090000_abcdef"
        ruta = trabajos.ruta_log(uid)
        ruta.parent.mkdir(parents=True, exist_ok=True)
        ruta.write_text("linea " * 5000, encoding="utf-8")
        texto, tamano = trabajos.leer_log(uid, maximo=100)
        self.assertEqual(len(texto), 100)
        self.assertEqual(tamano, ruta.stat().st_size)


if __name__ == "__main__":
    unittest.main()
