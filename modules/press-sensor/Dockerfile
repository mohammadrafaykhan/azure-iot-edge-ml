# Build context: the repository root (infra/package.py ships edgeml/, ml/, and modules/).
FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN useradd --uid 1000 --no-create-home --shell /usr/sbin/nologin module
WORKDIR /app

COPY modules/press-sensor/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY edgeml edgeml
COPY modules/press-sensor/sensor.py modules/press-sensor/press_sensor.py ./

USER module
CMD ["python", "press_sensor.py"]
