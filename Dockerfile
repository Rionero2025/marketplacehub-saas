FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    tesseract-ocr \
    tesseract-ocr-eng \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./requirements.txt
RUN python -m pip install --upgrade pip && \
    python -m pip install -r requirements.txt

COPY . .
RUN mkdir -p /app/data /app/logs

EXPOSE 10000

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD sh -c 'curl --fail "http://127.0.0.1:${PORT:-10000}/_stcore/health" || exit 1'

CMD ["sh", "-c", "python tools/online_preflight.py && exec streamlit run app.py --server.address=0.0.0.0 --server.port=${PORT:-10000} --server.headless=true --browser.gatherUsageStats=false"]
