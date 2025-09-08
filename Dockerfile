# Dockerfile
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# OS deps for Chromium
RUN apt-get update && apt-get install -y --no-install-recommends \
    chromium chromium-driver fonts-liberation \
    ca-certificates curl unzip \
    && rm -rf /var/lib/apt/lists/*

# Let Selenium know where Chromium is
ENV CHROME_BIN=/usr/bin/chromium
ENV CHROMEDRIVER_BIN=/usr/bin/chromedriver
ENV HEADLESS=1

WORKDIR /app
COPY requirements.txt /app/
RUN pip install -r requirements.txt

# Copy sources
COPY . /app/

# Railway sets $PORT; default 8000 locally
EXPOSE 8000
CMD ["bash", "-lc", "uvicorn api:app --host 0.0.0.0 --port ${PORT:-8000}"]
