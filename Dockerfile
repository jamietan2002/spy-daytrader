FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app/src \
    PORT=8501 \
    STATE_DB_PATH=/app/data/bot_state.db

WORKDIR /app

COPY requirements.txt pytest.ini README.md main.py ./
COPY src ./src
COPY dashboard ./dashboard
COPY .streamlit ./.streamlit

RUN pip install --upgrade pip && \
    pip install -r requirements.txt && \
    mkdir -p /app/data

EXPOSE 8501

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.getenv(\"PORT\", \"8501\")}/_stcore/health', timeout=4)" || exit 1

CMD ["python", "main.py", "serve"]

