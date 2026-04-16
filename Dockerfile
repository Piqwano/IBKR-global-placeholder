FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && \
    apt-get install -y --no-install-recommends tzdata && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY config.py ibkr_helpers.py global_rsi_bot.py backtest.py dashboard.py ./

EXPOSE 8000

VOLUME ["/app/state"]

CMD ["python", "-u", "global_rsi_bot.py"]
