# ---------- base ----------
FROM python:3.11-slim

# System deps for Chrome & Selenium (headless)
RUN apt-get update && apt-get install -y \
    wget unzip gnupg curl fonts-liberation \
    libnss3 libxss1 libasound2 libatk-bridge2.0-0 libgtk-3-0 \
    libu2f-udev xvfb \
    && rm -rf /var/lib/apt/lists/*

# Install Chrome (stable)
RUN wget -qO - https://dl-ssl.google.com/linux/linux_signing_key.pub | gpg --dearmor -o /usr/share/keyrings/google-linux.gpg && \
    echo "deb [arch=amd64 signed-by=/usr/share/keyrings/google-linux.gpg] http://dl.google.com/linux/chrome/deb/ stable main" \
      > /etc/apt/sources.list.d/google-chrome.list && \
    apt-get update && apt-get install -y google-chrome-stable && \
    rm -rf /var/lib/apt/lists/*

# Install chromedriver matching chrome
RUN CHROME_VERSION=$(google-chrome --version | awk '{print $3}') && \
    MAJOR=${CHROME_VERSION%%.*} && \
    URL=$(curl -s https://googlechromelabs.github.io/chrome-for-testing/known-good-versions-with-downloads.json \
      | python - <<'PY'\nimport sys, json; d=json.load(sys.stdin); \nprint(next(v['downloads']['chromedriver'][0]['url'] \nfor v in d['versions'] if v['version'].split('.')[0]==sys.argv[1]))\nPY $MAJOR) && \
    curl -L "$URL" -o /tmp/chromedriver.zip && \
    unzip /tmp/chromedriver.zip -d /usr/local/bin && \
    mv /usr/local/bin/chromedriver* /usr/local/bin/chromedriver && \
    chmod +x /usr/local/bin/chromedriver && \
    rm -rf /tmp/chromedriver.zip

# ---------- python deps ----------
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy app package
COPY app/ app/

# Make sure imports resolve
ENV PYTHONPATH=/app

# Railway provides $PORT
EXPOSE 8000
CMD ["uvicorn", "app.api:app", "--host", "0.0.0.0", "--port", "8000"]
