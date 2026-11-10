"""Serializacion de Server-Sent Events.

Vive aparte porque lo usan tres sitios con el mismo formato: el chat (I5), el
progreso de las subidas (I10b) y la narrativa del informe (I8). Tres copias de
la misma linea son tres sitios donde equivocarse al escapar el JSON.

El `data:` de SSE no admite saltos de linea, asi que el JSON va sin `indent`:
un `ensure_ascii=False` deja los acentos legibles y `json.dumps` ya escapa los
saltos como `\\n`.
"""

import json


def evento(nombre: str, dato) -> str:
    """Un evento SSE completo, con su linea en blanco de cierre."""
    return f"event: {nombre}\ndata: {json.dumps(dato, ensure_ascii=False)}\n\n"
