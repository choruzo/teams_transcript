"""Pruebas del orquestador (`procesar_teams.py`).

Nunca se ejecuta ningun paso: lo que se prueba es lo unico que puede fallar en
silencio, que es *que* comando se habria lanzado y en que orden. Los tres
scripts encadenados ya tienen sus propias pruebas (o su verificacion a mano);
aqui lo que se vigila es el pegamento: que el resumen salga siempre con
--sin-indice, que el uid que se indexa sea el de la base y no el deducido del
nombre, y que el servicio del LLM se vuelva a arrancar aunque la
transcripcion reviente.
"""

import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import memoria  # noqa: E402
import procesar_teams as pt  # noqa: E402


def _args(entrada="grabaciones/x_mixed.wav", **extra):
    """Los argumentos ya parseados, como los deja el CLI."""
    args = pt.construir_parser().parse_args([entrada])
    args.raiz = Path("/proyecto")
    for clave, valor in extra.items():
        setattr(args, clave, valor)
    return args


class TestComandos(unittest.TestCase):
    def test_transcribir_lleva_los_flags_de_whisper(self):
        args = _args(diarize=True, modelo_whisper="large-v3", num_speakers=4)
        cmd = pt.comando_transcribir(args, Path("a/x_mixed.wav"))
        self.assertIn("--diarize", cmd)
        self.assertIn("--num-speakers", cmd)
        self.assertEqual(cmd[cmd.index("--model") + 1], "large-v3")
        self.assertTrue(cmd[1].endswith("transcribe_teams.py"))

    def test_transcribir_omite_lo_no_indicado(self):
        cmd = pt.comando_transcribir(_args(), Path("a/x_mixed.wav"))
        for ausente in ("--diarize", "--device", "--hf-token", "--sin-glosario"):
            self.assertNotIn(ausente, cmd)

    def test_resumir_siempre_desactiva_el_indice(self):
        # El indexado es un paso propio del orquestador: si `summarize` lo
        # hiciera tambien, se indexaria dos veces y un fallo suyo quedaria
        # mezclado con el del resumen.
        cmd = pt.comando_resumir(_args(), Path("a/x_mixed.txt"))
        self.assertIn("--sin-indice", cmd)

    def test_resumir_arrastres_excluyentes(self):
        con_n = pt.comando_resumir(_args(arrastres=3), Path("x.txt"))
        self.assertEqual(con_n[con_n.index("--arrastres") + 1], "3")
        sin = pt.comando_resumir(_args(arrastres=3, sin_arrastres=True), Path("x.txt"))
        self.assertIn("--sin-arrastres", sin)
        self.assertNotIn("--arrastres", sin)

    def test_indexar_acota_a_la_reunion(self):
        cmd = pt.comando_indexar(_args(), "20260907_090437_mixed")
        self.assertEqual(cmd[cmd.index("--reunion") + 1], "20260907_090437_mixed")

    def test_indexar_sin_uid_no_pasa_reunion(self):
        # Sin uid se indexa lo que falte: correcto, solo mas lento.
        self.assertNotIn("--reunion", pt.comando_indexar(_args(), None))

    def test_servicio_con_y_sin_sudo(self):
        self.assertEqual(
            pt.comando_servicio("stop", "llama-qwen3-8.service"),
            ["sudo", "-n", "systemctl", "stop", "llama-qwen3-8.service"],
        )
        self.assertEqual(
            pt.comando_servicio("start", "llama.service", sudo=False),
            ["systemctl", "start", "llama.service"],
        )


class TestConfiguracion(unittest.TestCase):
    def test_servicios_de_la_variable_de_entorno(self):
        import os

        previo = os.environ.get(pt.VARIABLE_SERVICIOS)
        os.environ[pt.VARIABLE_SERVICIOS] = " llama-qwen3-8.service , bge.service "
        try:
            self.assertEqual(
                pt.servicios_configurados(None),
                ["llama-qwen3-8.service", "bge.service"],
            )
            # El flag gana a la variable.
            self.assertEqual(pt.servicios_configurados(["otro.service"]), ["otro.service"])
        finally:
            if previo is None:
                del os.environ[pt.VARIABLE_SERVICIOS]
            else:
                os.environ[pt.VARIABLE_SERVICIOS] = previo

    def test_sin_servicios_no_hay_nada_que_mover(self):
        import os

        previo = os.environ.pop(pt.VARIABLE_SERVICIOS, None)
        try:
            self.assertEqual(pt.servicios_configurados(None), [])
        finally:
            if previo is not None:
                os.environ[pt.VARIABLE_SERVICIOS] = previo

    def test_plan_recortado(self):
        self.assertEqual(pt.plan("transcribir", "indexar"), list(pt.PASOS))
        self.assertEqual(pt.plan("resumir", "indexar"), ["resumir", "indexar"])
        self.assertEqual(pt.plan("resumir", "resumir"), ["resumir"])

    def test_ruta_de_la_transcripcion(self):
        audio = Path("grabaciones/20260907_090437_mixed.wav")
        self.assertEqual(
            pt.ruta_transcripcion(audio, None),
            Path("grabaciones/20260907_090437_mixed.txt"),
        )
        self.assertEqual(
            pt.ruta_transcripcion(audio, "salida"),
            Path("salida/20260907_090437_mixed.txt"),
        )


class TestUid(unittest.TestCase):
    """El uid se lee de la base, no se deduce del nombre del fichero."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ruta = Path(self.tmp.name) / "meetings.db"
        self.transcripcion = Path(self.tmp.name) / "daily.txt"
        self.transcripcion.write_text("hola\n", encoding="utf-8")
        self.addCleanup(self.tmp.cleanup)

    def _reunion(self, uid: str, transcript_path: str) -> None:
        conn = memoria.conectar(self.ruta)
        try:
            conn.execute(
                "INSERT INTO meetings (uid, fecha, tipo, transcript_path, creado_en) "
                "VALUES (?, '2026-09-07', 'daily', ?, '2026-09-07T10:00:00')",
                (uid, transcript_path),
            )
            conn.commit()
        finally:
            conn.close()

    def test_devuelve_el_uid_registrado(self):
        # `uid_transcripcion` daria "daily"; la base dice otra cosa porque el
        # nombre ya estaba cogido y se desempato con un hash.
        self._reunion("daily-ab12cd", str(self.transcripcion.resolve()))
        self.assertEqual(
            pt.uid_de_reunion(str(self.ruta), self.transcripcion), "daily-ab12cd"
        )

    def test_sin_reunion_devuelve_none(self):
        memoria.conectar(self.ruta).close()
        self.assertIsNone(pt.uid_de_reunion(str(self.ruta), self.transcripcion))

    def test_base_ilegible_no_rompe_el_pipeline(self):
        # El indice es accesorio: que no se pueda mirar la base no puede
        # tumbar un procesado cuyo resumen ya esta guardado.
        self.assertIsNone(
            pt.uid_de_reunion(str(Path(self.tmp.name) / "no-existe.db"), self.transcripcion)
        )


class TestFlujo(unittest.TestCase):
    """El orden real de los pasos, con los tres scripts sustituidos.

    No se ejecuta nada: se anota que se habria hecho. Lo que se comprueba es
    la secuencia (parar el LLM -> transcribir -> arrancarlo -> esperar ->
    resumir -> indexar) y, sobre todo, que el `start` ocurre tambien cuando la
    transcripcion falla: sin eso, un fallo de Whisper dejaria el chat de la
    web caido hasta que alguien lo notara.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.audio = base / "20260907_090437_mixed.wav"
        self.audio.write_bytes(b"RIFF")
        # Como si Whisper ya la hubiera dejado: main comprueba que exista.
        (base / "20260907_090437_mixed.txt").write_text("hola", encoding="utf-8")

    def _correr(self, extra=(), fallar=None):
        registro: list[tuple[str, str]] = []

        def falso_ejecutar(cmd, nombre, simular=False):
            registro.append(("paso", nombre))
            if nombre == fallar:
                raise pt.ErrorPaso(nombre, 3)

        def falso_mover(accion, servicios, sudo, simular):
            for servicio in servicios:
                registro.append((accion, servicio))

        argv = ["procesar_teams.py", str(self.audio), *extra]
        parches = [
            mock.patch.object(pt, "ejecutar", falso_ejecutar),
            mock.patch.object(pt, "mover_servicios", falso_mover),
            mock.patch.object(pt, "esperar_al_llm", lambda args, simular: registro.append(("espera", str(args.espera)))),
            mock.patch.object(pt, "uid_de_reunion", lambda db, t: "20260907_090437_mixed"),
            mock.patch.object(sys, "argv", argv),
        ]
        codigo = 0
        with contextlib.ExitStack() as pila:
            # main() cuenta lo que hace por stdout; aqui solo estorba.
            pila.enter_context(contextlib.redirect_stdout(io.StringIO()))
            for parche in parches:
                pila.enter_context(parche)
            try:
                pt.main()
            except SystemExit as exc:  # noqa: PERF203
                codigo = exc.code or 0
        return registro, codigo

    def test_secuencia_completa(self):
        registro, codigo = self._correr(["--servicio", "llama.service"])
        self.assertEqual(codigo, 0)
        self.assertEqual(
            registro,
            [
                ("stop", "llama.service"),
                ("paso", "transcribir"),
                ("start", "llama.service"),
                ("espera", str(pt.ESPERA_POR_DEFECTO)),
                ("paso", "resumir"),
                ("paso", "indexar"),
            ],
        )

    def test_el_servicio_se_arranca_aunque_falle_whisper(self):
        registro, codigo = self._correr(["--servicio", "llama.service"], fallar="transcribir")
        self.assertEqual(codigo, 3)
        self.assertIn(("start", "llama.service"), registro)
        # Y no se sigue: nada que resumir ni que indexar.
        self.assertNotIn(("paso", "resumir"), registro)
        self.assertNotIn(("paso", "indexar"), registro)

    def test_un_txt_empieza_por_el_resumen(self):
        transcripcion = self.audio.with_suffix(".txt")
        registro: list = []
        with contextlib.ExitStack() as pila:
            pila.enter_context(contextlib.redirect_stdout(io.StringIO()))
            pila.enter_context(mock.patch.object(pt, "ejecutar", lambda cmd, nombre, simular=False: registro.append(nombre)))
            pila.enter_context(mock.patch.object(pt, "uid_de_reunion", lambda db, t: None))
            pila.enter_context(mock.patch.object(sys, "argv", ["p", str(transcripcion)]))
            pt.main()
        self.assertEqual(registro, ["resumir", "indexar"])

    def test_hasta_recorta(self):
        registro, codigo = self._correr(["--hasta", "resumir"])
        self.assertEqual(codigo, 0)
        self.assertNotIn(("paso", "indexar"), registro)


if __name__ == "__main__":
    unittest.main()
