FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends webp && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY build-cache.py .

ENTRYPOINT ["python3", "build-cache.py"]
