# syntax=docker/dockerfile:1
FROM python:3.12-slim

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1

# rasterio's wheel links against libexpat, which python:*-slim does not ship.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libexpat1 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY configs ./configs
COPY src ./src
COPY service ./service
COPY scripts ./scripts

# Маска постоянных тепловых источников (факелы, промышленность) нужна для отсева техногенного огня.
# Без неё образ работал бы с ложными «пожарами» на факелах, пока маска не построится в фоне.
RUN python - <<'CHECK'
import sys, yaml
from pathlib import Path
files = yaml.safe_load(open("configs/service.yaml", encoding="utf-8"))["fire_first"]["persistent_sources"]["files"]
missing = [f for f in files if not Path(f).is_file()]
if missing:
    sys.exit(f"Нет маски постоянных источников: {missing}. Постройте: python scripts/build_persistent_sources.py "
             "--region configs/regions/krasnodar_adygea.geojson --output <путь из configs/service.yaml>")
print("маска постоянных источников:", files)
CHECK

# Ключи (CDSE_CLIENT_ID, CDSE_CLIENT_SECRET, FIRMS_MAP_KEY) передаются переменными окружения
# при запуске контейнера; в образ они не попадают.
EXPOSE 8080
CMD ["python", "-m", "service", "--host", "0.0.0.0", "--port", "8080"]
