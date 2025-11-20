# syntax=docker/dockerfile:1.7
FROM python:3.11-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install build deps for pip packages (removed after layer)
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /app/.streamlit && \
    if [ -d .streamlit ]; then cp -r .streamlit /app/; fi

RUN useradd --create-home trader \
    && chown -R trader:trader /app
USER trader

# Make entrypoint executable in case git clone preserved mode
RUN chmod +x /app/docker-entrypoint.sh

ENTRYPOINT ["/app/docker-entrypoint.sh"]
#CMD ["sh", "-c", "streamlit run ui/ui_dashboard.py --server.port $PORT --server.address 0.0.0.0"]
#CMD ["python", "auto_future_trader.py"]
