"""Tests del trabajador de subidas (I10b), con un pipeline falso.

No se ejecuta Whisper ni se toca la GPU: lo que se prueba es la maquina de
estados, la construccion del comando, la reanudacion por ficheros y que un
fallo no deja el trabajo colgado.
"""

import json
import os
import sys
import tempfile
import threading
import time
import unittest
import wave
from pathlib import Path

import trabajador_teams as trabajador
import memoria
import trabajos

FAKE = '''\
import argparse
import os
import sys
import time
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("entrada")
parser.add_argument("--desde", default="transcribir")
args, _ = parser.parse_known_args()
audio = Path(args.entrada)
print(f"$ /x/transcribe_teams.py {audio}", flush=True)
dormir = float(os.environ.get("FAKE_DORMIR", "0"))
if dormir:
    print("FAKE_DURMIENDO", flush=True)
    time.sleep(dormir)
if args.desde == "transcribir":
    audio.with_suffix(".txt").write_text("transcripcion", encoding="utf-8")
salida = int(os.environ.get("FAKE_SALIDA", "0"))
if salida:
    sys.exit(salida)
print(f"$ /x/summarize_teams.py {audio.with_suffix('.txt')}", flush=True)
if os.environ.get("FAKE_503_UNA_VEZ"):
    marca = Path(os.environ["FAKE_503_MARCA"])
    if not marca.exists():
        print(
            "litellm ... HTTP 503 ... Loading model ... ServiceUnavailable",
            flush=True,
        )
        marca.write_text("x", encoding="utf-8")
        sys.exit(1)
audio.with_name(audio.stem + "_resumen.md").write_text(
    "# resumen", encoding="utf-8"
)
print("$ /x/indexar_teams.py --reunion x", flush=True)
sys.exit(0)
'''


def escribir_wav(ruta: Path, segundos: float = 0.2) -> bytes:
    with wave.open(str(ruta), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b"\x00\x00" * int(8000 * segundos))
    return ruta.read_bytes()


class BaseTrabajador(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.raiz = Path(self._tmp.name)
        self.db_cola = self.raiz / "trabajos.db"
        self.db = self.raiz / "meetings.db"
        self.indice = self.raiz / "indice.db"
        self.subidas = self.raiz / "subidas"
        self.grabaciones = self.raiz / "grabaciones"
        self.pipeline = self.raiz / "pipeline_falso.py"
        self.pipeline.write_text(FAKE, encoding="utf-8")
        self.conn = trabajos.conectar(self.db_cola)
        self.addCleanup(self.conn.close)
        self._entorno_previo = {
            clave: os.environ.get(clave)
            for clave in (
                "FAKE_DORMIR",
                "FAKE_SALIDA",
                "FAKE_503_UNA_VEZ",
                "FAKE_503_MARCA",
                "TEAMS_SUBIDAS_DIR",
            )
        }
        os.environ["TEAMS_SUBIDAS_DIR"] = str(self.subidas)
        self.addCleanup(self._restaurar)
        trabajador.PARANDO = False

    def _restaurar(self):
        for clave, valor in self._entorno_previo.items():
            if valor is None:
                os.environ.pop(clave, None)
            else:
                os.environ[clave] = valor

    def argv(self, **cambios):
        datos = [
            "--db-cola", str(self.db_cola),
            "--db", str(self.db),
            "--indice", str(self.indice),
            "--subidas", str(self.subidas),
            "--grabaciones", str(self.grabaciones),
            "--pipeline", str(self.pipeline),
            "--python", sys.executable,
            "--una-vez",
        ]
        for clave, valor in cambios.items():
            if valor is True:
                datos.append(f"--{clave.replace('_', '-')}")
            elif valor is not None:
                datos.extend([f"--{clave.replace('_', '-')}", str(valor)])
        return datos

    def args(self, **cambios):
        return trabajador._parser().parse_args(self.argv(**cambios))

    def encolar(self, segundos: float = 0.2) -> dict:
        origen = self.raiz / "origen.wav"
        datos = escribir_wav(origen, segundos)
        trabajo = trabajos.crear(
            self.conn,
            nombre_original="20261110_090000_mixed.wav",
            tamano=len(datos),
            fecha_reunion="2026-11-10",
            hora_reunion="09:00",
            tipo="daily",
            opciones={"diarizar": True, "modelo": "large-v3"},
            trozo=4096,
            ruta_subidas_directorio=self.subidas,
        )
        for indice in range(0, len(datos), 4096):
            trabajos.anotar_parte(
                self.conn,
                trabajo["uid"],
                indice // 4096,
                datos[indice : indice + 4096],
            )
        return trabajos.finalizar(self.conn, trabajo["uid"])


class TestComando(BaseTrabajador):
    def test_incluye_modelo_tipo_fecha_y_diarizacion(self):
        trabajo = {
            "uid": "x",
            "tipo": "retro",
            "fecha_reunion": "2026-11-05",
            "opciones": json.dumps({"diarizar": True, "modelo": "medium"}),
        }
        cmd = trabajador.construir_comando(
            self.args(servicio="llama-qwen3-8.service"),
            trabajo,
            Path("/x/audio.wav"),
            "transcribir",
            db=self.db,
            indice=self.indice,
        )
        self.assertIn("--diarize", cmd)
        self.assertEqual(cmd[cmd.index("--modelo-whisper") + 1], "medium")
        self.assertEqual(cmd[cmd.index("--tipo") + 1], "retro")
        self.assertEqual(cmd[cmd.index("--fecha") + 1], "2026-11-05")
        self.assertEqual(cmd[cmd.index("--servicio") + 1], "llama-qwen3-8.service")
        self.assertEqual(cmd[cmd.index("--desde") + 1], "transcribir")

    def test_sin_diarizar_y_con_titulo(self):
        trabajo = {
            "uid": "x",
            "tipo": "daily",
            "fecha_reunion": "2026-11-05",
            "opciones": json.dumps({"diarizar": True, "titulo": "Daily de equipo"}),
        }
        cmd = trabajador.construir_comando(
            self.args(sin_diarizar=True), trabajo, Path("/x/a.wav"), "resumir",
            db=self.db, indice=self.indice,
        )
        self.assertNotIn("--diarize", cmd)
        self.assertEqual(cmd[cmd.index("--titulo") + 1], "Daily de equipo")

    def test_modelo_por_defecto(self):
        trabajo = {"uid": "x", "tipo": "daily", "fecha_reunion": "2026-11-05",
                   "opciones": "{}"}
        cmd = trabajador.construir_comando(
            self.args(), trabajo, Path("/x/a.wav"), "indexar",
            db=self.db, indice=self.indice,
        )
        self.assertEqual(cmd[cmd.index("--modelo-whisper") + 1], "large-v3")


class TestReanudar(BaseTrabajador):
    def test_paso_inicial(self):
        audio = self.raiz / "a.wav"
        escribir_wav(audio)
        self.assertEqual(trabajador.paso_inicial(audio), "transcribir")
        audio.with_suffix(".txt").write_text("hola", encoding="utf-8")
        self.assertEqual(trabajador.paso_inicial(audio), "resumir")
        audio.with_name("a_resumen.md").write_text("# r", encoding="utf-8")
        self.assertEqual(trabajador.paso_inicial(audio), "indexar")

    def test_un_txt_vacio_no_cuenta(self):
        audio = self.raiz / "a.wav"
        escribir_wav(audio)
        audio.with_suffix(".txt").write_text("", encoding="utf-8")
        self.assertEqual(trabajador.paso_inicial(audio), "transcribir")


class TestEjecucion(BaseTrabajador):
    def test_procesa_un_trabajo_y_lo_deja_hecho(self):
        trabajo = self.encolar()
        codigo = trabajador.main(self.argv())
        self.assertEqual(codigo, 0)
        final = trabajos.detalle(self.conn, trabajo["uid"])
        self.assertEqual(final["estado"], "hecho")
        self.assertEqual(final["codigo_salida"], 0)
        audio = Path(final["audio_path"])
        self.assertTrue(audio.exists())
        self.assertEqual(audio.parent, self.grabaciones)
        self.assertTrue(audio.with_suffix(".txt").exists())
        self.assertTrue(
            audio.with_name(f"{audio.stem}_resumen.md").exists()
        )
        log, _ = trabajos.leer_log(trabajo["uid"])
        self.assertIn("transcribe_teams.py", log)
        self.assertIn("indexar_teams.py", log)

    def test_guarda_el_uid_de_la_reunion(self):
        """La busqueda tiene que ser por el `.txt`, no por el `.wav`."""
        conn = memoria.conectar(self.db)
        try:
            memoria.crear_reunion(
                conn,
                fecha="2026-11-10",
                transcript_path=str(
                    self.grabaciones / "20261110_090000_mixed.txt"
                ),
            )
            conn.commit()
        finally:
            conn.close()
        trabajo = self.encolar()
        trabajador.main(self.argv())
        final = trabajos.detalle(self.conn, trabajo["uid"])
        self.assertEqual(final["estado"], "hecho")
        self.assertEqual(final["reunion_uid"], "20261110_090000_mixed")

    def test_un_fallo_del_pipeline_queda_como_error(self):
        os.environ["FAKE_SALIDA"] = "3"
        trabajo = self.encolar()
        trabajador.main(self.argv())
        final = trabajos.detalle(self.conn, trabajo["uid"])
        self.assertEqual(final["estado"], "error")
        self.assertEqual(final["codigo_salida"], 3)
        self.assertIn("transcribe_teams.py", final["mensaje"])

    def test_un_trabajo_que_quedo_en_curso_se_marca_interrumpido(self):
        trabajo = self.encolar()
        trabajos.reclamar(self.conn)
        trabajador.main(self.argv())
        final = trabajos.detalle(self.conn, trabajo["uid"])
        self.assertEqual(final["estado"], "interrumpido")

    def test_reintentar_reanuda_desde_el_resumen(self):
        os.environ["FAKE_SALIDA"] = "1"
        trabajo = self.encolar()
        trabajador.main(self.argv())
        final = trabajos.detalle(self.conn, trabajo["uid"])
        self.assertEqual(final["estado"], "error")

        # El fallo dejo el .txt: al reintentar el comando debe empezar por
        # resumir, no por transcribir otra vez.
        os.environ["FAKE_SALIDA"] = "0"
        trabajos.reintentar(self.conn, trabajo["uid"])
        final = trabajador.ejecutar(
            self.conn,
            trabajos.reclamar(self.conn),
            self.args(),
        )
        self.assertEqual(final["estado"], "hecho")
        log, _ = trabajos.leer_log(trabajo["uid"])
        self.assertIn("--desde resumir", log)
        self.assertEqual(log.count("--desde transcribir"), 1)
        self.assertIn("summarize_teams.py", log)

    def test_reintenta_el_resumen_si_litellm_aun_carga(self):
        """El 503 'Loading model' es transitorio: no puede perder el trabajo."""
        os.environ["FAKE_503_UNA_VEZ"] = "1"
        os.environ["FAKE_503_MARCA"] = str(self.raiz / "marca_503")
        trabajo = self.encolar()
        trabajador.main(self.argv(espera_reintento=0))
        final = trabajos.detalle(self.conn, trabajo["uid"])
        self.assertEqual(final["estado"], "hecho")
        self.assertIsNone(final["mensaje"])
        log, _ = trabajos.leer_log(trabajo["uid"])
        self.assertIn("Loading model", log)
        self.assertEqual(log.count(f"== {trabajo['uid']} =="), 2)


@unittest.skipUnless(os.name == "posix", "la cancelacion real es de Linux")
class TestCancelacion(BaseTrabajador):
    def test_cancelar_un_trabajo_en_curso_lo_para(self):
        os.environ["FAKE_DORMIR"] = "30"
        trabajo = self.encolar()
        hilo = threading.Thread(
            target=trabajador.main,
            args=(self.argv(),),
            daemon=True,
        )
        hilo.start()
        limite = time.monotonic() + 10
        while time.monotonic() < limite:
            fila = trabajos.detalle(self.conn, trabajo["uid"])
            log, _ = trabajos.leer_log(trabajo["uid"])
            if fila["estado"] == "transcribiendo" and "FAKE_DURMIENDO" in log:
                break
            time.sleep(0.1)
        else:
            self.fail("El trabajador no llego a transcribir")
        trabajos.cancelar(self.conn, trabajo["uid"])
        hilo.join(timeout=15)
        self.assertFalse(hilo.is_alive(), "el trabajador no termino")
        final = trabajos.detalle(self.conn, trabajo["uid"])
        self.assertEqual(final["estado"], "cancelado")


if __name__ == "__main__":
    unittest.main()
