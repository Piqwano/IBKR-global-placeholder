FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    tzdata \
 && rm -rf /var/lib/apt/lists/*

# Python runtime
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=UTC

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY config.py ibkr_helpers.py global_rsi_bot.py backtest.py ./

# bot_state.json is written here at runtime
VOLUME ["/app/state"]
ENV BOT_STATE_FILE=/app/state/bot_state.json

CMD ["python", "-u", "global_rsi_bot.py"]
