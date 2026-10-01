"""Autenticacion de la interfaz (login, logout y quien soy).

La logica de usuarios y sesiones vive en `autenticacion.py`, en la raiz: esta
capa solo traduce HTTP. El hash de la clave nunca sale de ahi.
"""

import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

import autenticacion
from api.deps import COOKIE_SESION, cookie_segura, conexion_usuarios, usuario_actual

router = APIRouter(tags=["autenticacion"])


class Credenciales(BaseModel):
    usuario: str = Field(min_length=1, max_length=200)
    clave: str = Field(min_length=1, max_length=500)


class Sesion(BaseModel):
    """Lo que el front necesita saber tras entrar (y para saludar en el pie)."""

    usuario: str
    rol: str


@router.post("/login", response_model=Sesion, summary="Iniciar sesion")
def login(
    datos: Credenciales,
    respuesta: Response,
    conn: sqlite3.Connection = Depends(conexion_usuarios),
) -> Sesion:
    """Valida las credenciales y deja una cookie de sesion HttpOnly.

    Si es la primera vez que arranca (no hay ningun usuario), se siembra el
    usuario inicial con `TEAMS_ADMIN_USUARIO`/`TEAMS_ADMIN_CLAVE`. Se hace aqui
    y no solo al arrancar para que una base borrada se pueda recuperar sin
    reiniciar el servicio.
    """
    autenticacion.sembrar_usuario_inicial(conn)
    fila = autenticacion.autenticar(conn, datos.usuario, datos.clave)
    if fila is None:
        raise HTTPException(status_code=401, detail="Usuario o clave incorrectos.")
    token, expira = autenticacion.crear_sesion(conn, fila["id"])
    respuesta.set_cookie(
        key=COOKIE_SESION,
        value=token,
        httponly=True,
        samesite="lax",
        secure=cookie_segura(),
        path="/",
        expires=_segundos_hasta(expira),
    )
    return Sesion(usuario=fila["nombre"], rol=fila["rol"])


@router.post("/logout", response_model=Sesion, summary="Cerrar sesion")
def logout(
    request: Request,
    respuesta: Response,
    conn: sqlite3.Connection = Depends(conexion_usuarios),
    usuario: dict = Depends(usuario_actual),
) -> Sesion:
    """Borra la sesion de la base y limpia la cookie.

    No se limita a quitarla del navegador: el token se elimina de la tabla, que
    es lo que hace que cerrar sesion invalide de verdad lo que hubiera abierto.
    """
    autenticacion.cerrar_sesion(conn, request.cookies.get(COOKIE_SESION, ""))
    respuesta.delete_cookie(COOKIE_SESION, path="/")
    return Sesion(usuario=usuario["nombre"], rol=usuario["rol"])


@router.get("/yo", response_model=Sesion, summary="Usuario de la sesion")
def yo(usuario: dict = Depends(usuario_actual)) -> Sesion:
    return Sesion(usuario=usuario["nombre"], rol=usuario["rol"])


def _segundos_hasta(expira: str) -> int:
    """`max_age` de la cookie a partir de su caducidad ISO.

    Se calcula en vez de fijarlo aparte para que la cookie y la fila de la
    sesion no puedan tener vidas distintas: manda siempre la fecha guardada.
    """
    from datetime import datetime, timezone

    try:
        cuando = datetime.fromisoformat(expira)
    except ValueError:  # pragma: no cover - la fecha la acabamos de generar
        return 0
    segundos = int((cuando - datetime.now(timezone.utc)).total_seconds())
    return max(segundos, 0)
