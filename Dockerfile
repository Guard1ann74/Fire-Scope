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

# Ключи (CDSE_CLIENT_ID, CDSE_CLIENT_SECRET, FIRMS_MAP_KEY) передаются переменными окружения
# при запуске контейнера; в образ они не попадают.
EXPOSE 8080
CMD ["python", "-m", "service", "--host", "0.0.0.0", "--port", "8080"]
