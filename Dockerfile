FROM python:3.11-slim

WORKDIR /app

# System deps for numpy/pandas wheels and timezone data
RUN apt-get update && apt-get install -y --no-install-recommends \
    tzdata \
    && rm -rf /var/lib/apt/lists/*

# Python deps first for better layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App code
COPY config.py ibkr_helpers.py dashboard.py global_rsi_bot.py ./
COPY backtest.py ./

# Persistent state dir (bot_state.json lives here in container)
VOLUME ["/app/state"]
ENV BOT_STATE_FILE=/app/state/bot_state.json

# Dashboard port — Railway will override via PORT env
EXPOSE 8000

# Unbuffered stdout so logs stream live
ENV PYTHONUNBUFFERED=1

CMD ["python", "-u", "global_rsi_bot.py"]
