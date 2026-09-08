# Imagen de la API. Se construye en el portatil (con internet) y se exporta al
# servidor Ubuntu (sin internet) con `docker save`. Ver PLAN_INTERFAZ.md #5.
#
# La imagen lleva SOLO Python y las dependencias: el codigo de la aplicacion
# (api/, web/, memoria.py) se monta como volumen, para que actualizar el front
# sea copiar ficheros de texto y no reexportar 200 MB.
#
# Fijada por digest y no por etiqueta: `python:3.12-slim` apunta a una imagen
# distinta cada mes, y en un entorno cerrado la reproducibilidad es lo unico
# que permite reconstruir con garantias dentro de seis meses.
FROM python:3.12-slim@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea

# PYTHONDONTWRITEBYTECODE: el codigo se monta en solo lectura, escribir .pyc
# junto a el solo produciria ruido y errores.
# PYTHONUNBUFFERED: sin esto los logs se quedan en el buffer y `docker logs`
# no muestra nada, que es lo peor que puede pasar al depurar a ciegas.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

WORKDIR /app

# Las dependencias, en su propia capa: es lo unico que se reconstruye cuando
# cambia algo, y lo unico que justifica reexportar la imagen.
COPY requirements-api.txt /tmp/requirements-api.txt
RUN pip install --no-cache-dir -r /tmp/requirements-api.txt \
 && rm /tmp/requirements-api.txt

# Usuario sin privilegios. El volumen de datos/ se monta con escritura, asi que
# el contenedor no deberia correr como root mas de lo imprescindible.
RUN useradd --create-home --uid 1000 memoria
USER memoria

EXPOSE 8000

# Sin --reload: en produccion vigilar ficheros solo gasta CPU. Para desarrollar
# en el portatil, el compose lo sobrescribe.
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]
