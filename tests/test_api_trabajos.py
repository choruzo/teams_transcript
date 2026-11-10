"""Tests de la API de subidas (I10b). Se saltan sin FastAPI, como el resto."""

import hashlib
import io
import os
import tempfile
import unittest
import wave
from pathlib import Path

import trabajos

try:
    from fastapi.testclient import TestClient

    from api.main import app

    HAY_FASTAPI = True
except ImportError:  # pragma: no cover - depende del entorno
    HAY_FASTAPI = False


def wav_de_prueba(segundos: float = 1.0, frecuencia: int = 8000) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(frecuencia)
        w.writeframes(b"\x00\x00" * int(frecuencia * segundos))
    return buffer.getvalue()


@unittest.skipUnless(HAY_FASTAPI, "requiere requirements-api.txt")
class BaseTrabajosAPI(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.raiz = Path(self._tmp.name)
        self.db = self.raiz / "trabajos.db"
        self.subidas = self.raiz / "subidas"
        self.grabaciones = self.raiz / "grabaciones"
        self._entorno_previo = {
            clave: os.environ.get(clave)
            for clave in (
                "TEAMS_TRABAJOS_DB",
                "TEAMS_SUBIDAS_DIR",
                "TEAMS_GRABACIONES_DIR",
                "TEAMS_SUBIDAS",
                "TEAMS_SUBIDA_TROZO_MB",
                "TEAMS_AUTENTICACION",
            )
        }
        os.environ.update(
            {
                "TEAMS_TRABAJOS_DB": str(self.db),
                "TEAMS_SUBIDAS_DIR": str(self.subidas),
                "TEAMS_GRABACIONES_DIR": str(self.grabaciones),
                "TEAMS_SUBIDAS": "1",
                "TEAMS_SUBIDA_TROZO_MB": "0.01",
                "TEAMS_AUTENTICACION": "0",
            }
        )
        self.addCleanup(self._restaurar)
        self.cliente = TestClient(app)

    def _restaurar(self):
        for clave, valor in self._entorno_previo.items():
            if valor is None:
                os.environ.pop(clave, None)
            else:
                os.environ[clave] = valor

    def crear(self, datos: bytes, **cambios):
        cuerpo = {
            "nombre": "20261110_090000_mixed.wav",
            "tamano": len(datos),
            "fecha": "2026-11-10",
            "hora": "09:00",
            "tipo": "daily",
            "diarizar": True,
            "modelo": "large-v3",
        }
        cuerpo.update(cambios)
        return self.cliente.post("/api/trabajos", json=cuerpo)

    def subir(self, datos: bytes, uid: str, trozo: int, con_hash: bool = True):
        for indice in range(0, len(datos), trozo):
            parte = datos[indice : indice + trozo]
            cabeceras = (
                {"X-Sha256": hashlib.sha256(parte).hexdigest()}
                if con_hash
                else {}
            )
            respuesta = self.cliente.put(
                f"/api/trabajos/{uid}/partes/{indice // trozo}",
                content=parte,
                headers=cabeceras,
            )
            self.assertEqual(respuesta.status_code, 200, respuesta.text)
        return respuesta


class TestEstado(BaseTrabajosAPI):
    def test_estado_con_subidas_activas(self):
        respuesta = self.cliente.get("/api/trabajos/estado")
        self.assertEqual(respuesta.status_code, 200)
        datos = respuesta.json()
        self.assertTrue(datos["disponible"])
        self.assertFalse(datos["trabajador"])
        self.assertEqual(datos["en_cola"], 0)
        self.assertGreater(datos["maximo_bytes"], 0)

    def test_estado_apagado(self):
        os.environ["TEAMS_SUBIDAS"] = "0"
        datos = self.cliente.get("/api/trabajos/estado").json()
        self.assertFalse(datos["disponible"])
        self.assertIn("TEAMS_SUBIDAS=1", datos["motivo"])
        respuesta = self.crear(wav_de_prueba())
        self.assertEqual(respuesta.status_code, 503)
        self.assertIn("TEAMS_SUBIDAS=1", respuesta.json()["detail"])


class TestFlujoCompleto(BaseTrabajosAPI):
    def test_crear_subir_finalizar_y_listar(self):
        datos = wav_de_prueba(segundos=0.4)
        creado = self.crear(datos)
        self.assertEqual(creado.status_code, 201, creado.text)
        trabajo = creado.json()
        self.assertEqual(trabajo["estado"], "subiendo")
        self.assertEqual(trabajo["siguiente"], 0)
        self.assertEqual(trabajo["opciones"]["modelo"], "large-v3")

        self.subir(datos, trabajo["uid"], trabajo["trozo"])
        estado = self.cliente.get(f"/api/trabajos/{trabajo['uid']}/subida").json()
        self.assertTrue(estado["completo"])

        final = self.cliente.post(f"/api/trabajos/{trabajo['uid']}/finalizar")
        self.assertEqual(final.status_code, 200, final.text)
        self.assertEqual(final.json()["estado"], "encolado")
        self.assertIsNotNone(final.json()["duracion_seg"])

        lista = self.cliente.get("/api/trabajos").json()["trabajos"]
        self.assertEqual(len(lista), 1)
        self.assertEqual(lista[0]["uid"], trabajo["uid"])

        ficha = self.cliente.get(f"/api/trabajos/{trabajo['uid']}")
        self.assertEqual(ficha.status_code, 200)
        self.assertEqual(ficha.json()["sha256"], hashlib.sha256(datos).hexdigest())

    def test_reanudar_por_el_ultimo_trozo_confirmado(self):
        datos = wav_de_prueba(segundos=0.4)
        trabajo = self.crear(datos).json()
        trozo = trabajo["trozo"]
        primero = datos[:trozo]
        self.cliente.put(
            f"/api/trabajos/{trabajo['uid']}/partes/0", content=primero
        )
        estado = self.cliente.get(f"/api/trabajos/{trabajo['uid']}/subida").json()
        self.assertEqual(estado["recibidos"], len(primero))
        self.assertEqual(estado["siguiente"], 1)
        self.assertFalse(estado["completo"])

        # El navegador recarga la pagina y vuelve a pedir desde `recibidos`.
        for indice, inicio in enumerate(range(len(primero), len(datos), trozo), start=1):
            parte = datos[inicio : inicio + trozo]
            respuesta = self.cliente.put(
                f"/api/trabajos/{trabajo['uid']}/partes/{indice}", content=parte
            )
            self.assertEqual(respuesta.status_code, 200)
        self.assertEqual(
            self.cliente.post(f"/api/trabajos/{trabajo['uid']}/finalizar").status_code,
            200,
        )


class TestErrores(BaseTrabajosAPI):
    def test_rechaza_lo_que_no_es_wav(self):
        respuesta = self.crear(b"x" * 100, nombre="daily.mp3")
        self.assertEqual(respuesta.status_code, 422)
        self.assertIn(".wav", respuesta.json()["detail"])

    def test_finalizar_incompleto(self):
        datos = wav_de_prueba(segundos=0.4)
        trabajo = self.crear(datos).json()
        respuesta = self.cliente.post(f"/api/trabajos/{trabajo['uid']}/finalizar")
        self.assertEqual(respuesta.status_code, 422)
        self.assertIn("Faltan", respuesta.json()["detail"])

    def test_trozo_fuera_de_orden(self):
        datos = wav_de_prueba(segundos=0.4)
        trabajo = self.crear(datos).json()
        respuesta = self.cliente.put(
            f"/api/trabajos/{trabajo['uid']}/partes/1", content=b"x" * 10
        )
        self.assertEqual(respuesta.status_code, 409)
        self.assertEqual(respuesta.json()["detail"]["esperado"], 0)

    def test_trozo_demasiado_grande(self):
        datos = wav_de_prueba(segundos=0.4)
        trabajo = self.crear(datos).json()
        respuesta = self.cliente.put(
            f"/api/trabajos/{trabajo['uid']}/partes/0",
            content=b"x" * (trabajo["trozo"] + 1),
        )
        self.assertEqual(respuesta.status_code, 413)

    def test_duplicado_y_forzar(self):
        datos = wav_de_prueba(segundos=0.4)
        primero = self.crear(datos).json()
        self.subir(datos, primero["uid"], primero["trozo"])
        self.cliente.post(f"/api/trabajos/{primero['uid']}/finalizar")

        segundo = self.crear(datos, nombre="otra.wav").json()
        self.subir(datos, segundo["uid"], segundo["trozo"])
        choque = self.cliente.post(f"/api/trabajos/{segundo['uid']}/finalizar")
        self.assertEqual(choque.status_code, 409)
        self.assertEqual(choque.json()["detail"]["uid"], primero["uid"])
        forzado = self.cliente.post(
            f"/api/trabajos/{segundo['uid']}/finalizar", json={"forzar": True}
        )
        self.assertEqual(forzado.status_code, 200)

    def test_trabajo_inexistente(self):
        self.assertEqual(
            self.cliente.get("/api/trabajos/no-existe").status_code, 404
        )

    def test_estado_desconocido_al_listar(self):
        respuesta = self.cliente.get("/api/trabajos?estado=inventado")
        self.assertEqual(respuesta.status_code, 422)


class TestCancelarYReintentar(BaseTrabajosAPI):
    def test_cancelar_una_subida(self):
        trabajo = self.crear(wav_de_prueba()).json()
        respuesta = self.cliente.post(f"/api/trabajos/{trabajo['uid']}/cancelar")
        self.assertEqual(respuesta.status_code, 200)
        self.assertEqual(respuesta.json()["estado"], "cancelado")

    def test_reintentar_lo_que_fallo(self):
        datos = wav_de_prueba(segundos=0.4)
        trabajo = self.crear(datos).json()
        self.subir(datos, trabajo["uid"], trabajo["trozo"])
        self.cliente.post(f"/api/trabajos/{trabajo['uid']}/finalizar")

        conn = trabajos.conectar(self.db)
        try:
            audio = self.raiz / "audio.wav"
            audio.write_bytes(datos)
            trabajos.marcar(
                conn, trabajo["uid"], "error", audio_path=str(audio), mensaje="boom"
            )
        finally:
            conn.close()
        respuesta = self.cliente.post(f"/api/trabajos/{trabajo['uid']}/reintentar")
        self.assertEqual(respuesta.status_code, 200)
        self.assertEqual(respuesta.json()["estado"], "encolado")

    def test_reintentar_sin_audio_da_422(self):
        trabajo = self.crear(wav_de_prueba()).json()
        self.cliente.post(f"/api/trabajos/{trabajo['uid']}/cancelar")
        respuesta = self.cliente.post(f"/api/trabajos/{trabajo['uid']}/reintentar")
        self.assertEqual(respuesta.status_code, 422)


class TestEventos(BaseTrabajosAPI):
    def test_sse_emite_estado_y_fin(self):
        datos = wav_de_prueba(segundos=0.4)
        trabajo = self.crear(datos).json()
        self.subir(datos, trabajo["uid"], trabajo["trozo"])
        self.cliente.post(f"/api/trabajos/{trabajo['uid']}/finalizar")
        conn = trabajos.conectar(self.db)
        try:
            trabajos.marcar(conn, trabajo["uid"], "hecho", reunion_uid="reunion-1")
        finally:
            conn.close()

        with self.cliente.stream(
            "GET", f"/api/trabajos/{trabajo['uid']}/eventos"
        ) as respuesta:
            self.assertEqual(respuesta.status_code, 200)
            texto = "".join(respuesta.iter_text())
        self.assertIn("event: estado", texto)
        self.assertIn("event: fin", texto)
        self.assertIn("reunion-1", texto)

    def test_log(self):
        trabajo = self.crear(wav_de_prueba()).json()
        ruta = trabajos.ruta_log(trabajo["uid"])
        ruta.parent.mkdir(parents=True, exist_ok=True)
        ruta.write_text("hola\n", encoding="utf-8")
        respuesta = self.cliente.get(f"/api/trabajos/{trabajo['uid']}/log")
        self.assertEqual(respuesta.status_code, 200)
        self.assertIn("hola", respuesta.text)


class TestAutenticacion(BaseTrabajosAPI):
    def test_sin_sesion_da_401(self):
        os.environ["TEAMS_AUTENTICACION"] = "1"
        respuesta = self.cliente.get("/api/trabajos")
        self.assertEqual(respuesta.status_code, 401)


if __name__ == "__main__":
    unittest.main()
