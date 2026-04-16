FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    tzdata \
 && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=UTC

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY config.py ibkr_helpers.py global_rsi_bot.py backtest.py dashboard.py ./

# Dashboard port (FastAPI). Railway sets $PORT automatically; we listen on
# 8000 by default if it isn't set.
EXPOSE 8000

# bot_state.json is written here at runtime
VOLUME ["/app/state"]
ENV BOT_STATE_FILE=/app/state/bot_state.json

CMD ["python", "-u", "global_rsi_bot.py"]
