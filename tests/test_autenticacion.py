# -*- coding: utf-8 -*-
"""Pruebas de `autenticacion.py`: claves, usuarios y sesiones.

Solo stdlib, como el modulo: tienen que correr tambien en el venv de Whisper,
donde no esta FastAPI.

    python -m unittest tests.test_autenticacion -v
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import autenticacion  # noqa: E402


class BaseAutenticacion(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db = Path(self._tmp.name) / "usuarios.db"
        self.conn = autenticacion.conectar(self.db)
        self.addCleanup(self.conn.close)


class TestClaves(BaseAutenticacion):
    def test_hash_y_verificacion(self):
        guardado = autenticacion.hash_clave("secreta")
        self.assertTrue(autenticacion.verificar_clave("secreta", guardado))
        self.assertFalse(autenticacion.verificar_clave("otra", guardado))

    def test_dos_hashes_de_la_misma_clave_son_distintos(self):
        """La sal es aleatoria: dos usuarios con la misma clave no la comparten."""
        self.assertNotEqual(
            autenticacion.hash_clave("secreta"), autenticacion.hash_clave("secreta")
        )

    def test_hash_corrupto_no_revienta(self):
        for malo in ("", "basura", "pbkdf2_sha256$x$y$z", "md5$1$aa$bb"):
            self.assertFalse(autenticacion.verificar_clave("secreta", malo))


class TestUsuarios(BaseAutenticacion):
    def test_crear_y_autenticar(self):
        autenticacion.crear_usuario(self.conn, "ana", "secreta")
        fila = autenticacion.autenticar(self.conn, "ana", "secreta")
        self.assertIsNotNone(fila)
        self.assertEqual(fila["rol"], "admin")

    def test_nombre_sin_distinguir_mayusculas(self):
        autenticacion.crear_usuario(self.conn, "Ana", "secreta")
        self.assertIsNotNone(autenticacion.autenticar(self.conn, "ana", "secreta"))

    def test_clave_incorrecta_o_usuario_inexistente(self):
        autenticacion.crear_usuario(self.conn, "ana", "secreta")
        self.assertIsNone(autenticacion.autenticar(self.conn, "ana", "mala"))
        self.assertIsNone(autenticacion.autenticar(self.conn, "nadie", "secreta"))

    def test_recrear_cambia_la_clave(self):
        autenticacion.crear_usuario(self.conn, "ana", "secreta")
        autenticacion.crear_usuario(self.conn, "ana", "nueva")
        self.assertIsNone(autenticacion.autenticar(self.conn, "ana", "secreta"))
        self.assertIsNotNone(autenticacion.autenticar(self.conn, "ana", "nueva"))

    def test_clave_demasiado_corta(self):
        with self.assertRaises(ValueError):
            autenticacion.crear_usuario(self.conn, "ana", "x")

    def test_inactivo_no_entra(self):
        autenticacion.crear_usuario(self.conn, "ana", "secreta", activo=False)
        self.assertIsNone(autenticacion.autenticar(self.conn, "ana", "secreta"))

    def test_sembrar_solo_la_primera_vez(self):
        os.environ.pop(autenticacion.VARIABLE_CLAVE_INICIAL, None)
        creado = autenticacion.sembrar_usuario_inicial(self.conn)
        self.assertEqual(creado, ("admin", "admin"))
        self.assertIsNone(autenticacion.sembrar_usuario_inicial(self.conn))

    def test_variable_de_clave_vacia_usa_el_default(self):
        """`docker-compose` deja `${TEAMS_ADMIN_CLAVE:-}` como cadena vacia."""
        previa = os.environ.get(autenticacion.VARIABLE_CLAVE_INICIAL)
        os.environ[autenticacion.VARIABLE_CLAVE_INICIAL] = ""
        self.addCleanup(
            lambda: os.environ.pop(autenticacion.VARIABLE_CLAVE_INICIAL, None)
            if previa is None
            else os.environ.__setitem__(autenticacion.VARIABLE_CLAVE_INICIAL, previa)
        )
        creado = autenticacion.sembrar_usuario_inicial(self.conn)
        self.assertEqual(creado, ("admin", "admin"))


class TestSesiones(BaseAutenticacion):
    def setUp(self):
        super().setUp()
        self.usuario_id = autenticacion.crear_usuario(self.conn, "ana", "secreta")

    def test_crear_y_resolver(self):
        token, _ = autenticacion.crear_sesion(self.conn, self.usuario_id)
        fila = autenticacion.resolver_sesion(self.conn, token)
        self.assertIsNotNone(fila)
        self.assertEqual(fila["nombre"], "ana")

    def test_token_inexistente_o_vacio(self):
        self.assertIsNone(autenticacion.resolver_sesion(self.conn, "inventado"))
        self.assertIsNone(autenticacion.resolver_sesion(self.conn, ""))

    def test_cerrar_la_invalida(self):
        token, _ = autenticacion.crear_sesion(self.conn, self.usuario_id)
        autenticacion.cerrar_sesion(self.conn, token)
        self.assertIsNone(autenticacion.resolver_sesion(self.conn, token))

    def test_caducada_no_vale_y_se_borra(self):
        token, _ = autenticacion.crear_sesion(self.conn, self.usuario_id, horas=-1)
        self.assertIsNone(autenticacion.resolver_sesion(self.conn, token))
        quedan = self.conn.execute("SELECT COUNT(*) FROM sesiones").fetchone()[0]
        self.assertEqual(quedan, 0)

    def test_dos_sesiones_del_mismo_usuario(self):
        a, _ = autenticacion.crear_sesion(self.conn, self.usuario_id)
        b, _ = autenticacion.crear_sesion(self.conn, self.usuario_id)
        self.assertNotEqual(a, b)
        self.assertIsNotNone(autenticacion.resolver_sesion(self.conn, b))

    def test_limpiar_sesiones(self):
        autenticacion.crear_sesion(self.conn, self.usuario_id, horas=-1)
        autenticacion.limpiar_sesiones(self.conn)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM sesiones").fetchone()[0], 0
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
