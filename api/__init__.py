"""Interfaz web sobre el historico de reuniones (`datos/meetings.db`).

Fase I0 de `PLAN_INTERFAZ.md`: el esqueleto. Sirve la pagina estatica de
`web/` y una API de solo lectura.

Regla del proyecto: **aqui no se escribe SQL**. Toda consulta vive en
`memoria.py`, que es la unica capa de acceso a la base y la comparten el
pipeline, esta API y los futuros `ask_teams.py` / `report_teams.py`.
"""

VERSION = "0.2.0"
