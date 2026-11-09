# -*- coding: utf-8 -*-
"""Pruebas del cliente LLM: la semilla fija y su degradacion.

No se llama a ningun servidor: se interceptan `_peticion` y `_abrir` para ver
exactamente que payload sale y como se reintenta cuando el backend no conoce
un campo opcional.
"""

import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))

import llm  # noqa: E402


class _Respuesta:
    def __init__(self, cuerpo):
        self._cuerpo = json.dumps(cuerpo).encode("utf-8")

    def read(self):
        return self._cuerpo

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


def _ok():
    return _Respuesta({"choices": [{"message": {"content": "ok"}}]})


class TestSemilla(unittest.TestCase):
    def setUp(self):
        self._previa = os.environ.pop("TEAMS_LLM_SEED", None)

    def tearDown(self):
        if self._previa is None:
            os.environ.pop("TEAMS_LLM_SEED", None)
        else:
            os.environ["TEAMS_LLM_SEED"] = self._previa

    def test_orden_argumento_entorno_defecto(self):
        self.assertEqual(llm.semilla(), llm.SEMILLA_POR_DEFECTO)
        self.assertEqual(llm.semilla(7), 7)
        os.environ["TEAMS_LLM_SEED"] = "9"
        self.assertEqual(llm.semilla(), 9)

    def test_un_valor_invalido_no_tumba_el_pipeline(self):
        os.environ["TEAMS_LLM_SEED"] = "no-es-un-numero"
        self.assertEqual(llm.semilla(), llm.SEMILLA_POR_DEFECTO)
        os.environ["TEAMS_LLM_SEED"] = "-3"
        self.assertEqual(llm.semilla(), llm.SEMILLA_POR_DEFECTO)


class TestChat(unittest.TestCase):
    def _llamar(self, **kwargs):
        capturados = []

        def peticion(_url, _clave, payload):
            capturados.append(dict(payload))
            return object()

        with mock.patch.object(llm, "_peticion", side_effect=peticion), mock.patch.object(
            llm, "_abrir", side_effect=lambda *_: _ok()
        ):
            self.assertEqual(llm.chat("http://x", None, "modelo", [], **kwargs), "ok")
        return capturados

    def test_manda_la_semilla_por_defecto(self):
        capturados = self._llamar()
        self.assertEqual(capturados[0]["seed"], llm.SEMILLA_POR_DEFECTO)

    def test_la_semilla_se_puede_cambiar(self):
        capturados = self._llamar(seed=99)
        self.assertEqual(capturados[0]["seed"], 99)

    def test_reintenta_sin_los_campos_opcionales(self):
        capturados = []

        def peticion(_url, _clave, payload):
            capturados.append(dict(payload))
            return object()

        respuestas = [
            llm.ErrorLLM("LiteLLM devolvio HTTP 400"),
            llm.ErrorLLM("LiteLLM devolvio HTTP 400"),
            _ok(),
        ]
        with mock.patch.object(llm, "_peticion", side_effect=peticion), mock.patch.object(
            llm, "_abrir", side_effect=respuestas
        ):
            self.assertEqual(
                llm.chat(
                    "http://x",
                    None,
                    "modelo",
                    [],
                    sin_razonamiento=True,
                    seed=5,
                ),
                "ok",
            )

        self.assertIn("seed", capturados[0])
        self.assertIn(llm.CLAVE_SIN_RAZONAMIENTO, capturados[0])
        self.assertIn("seed", capturados[1], "el primer reintento conserva la semilla")
        self.assertNotIn(llm.CLAVE_SIN_RAZONAMIENTO, capturados[1])
        self.assertNotIn("seed", capturados[2])
        self.assertNotIn(llm.CLAVE_SIN_RAZONAMIENTO, capturados[2])

    def test_un_timeout_no_se_reintenta(self):
        capturados = []

        def peticion(_url, _clave, payload):
            capturados.append(dict(payload))
            return object()

        with mock.patch.object(llm, "_peticion", side_effect=peticion), mock.patch.object(
            llm,
            "_abrir",
            side_effect=llm.ErrorLLM("LiteLLM no respondio en 5 s ('http://x')."),
        ):
            with self.assertRaises(llm.ErrorLLM):
                llm.chat("http://x", None, "modelo", [])
        self.assertEqual(len(capturados), 1)


if __name__ == "__main__":
    unittest.main()
