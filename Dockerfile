# Global RSI Bot — Dockerfile v2.3
# ══════════════════════════════════════════════════════════════════════════
# v2.3 fixes:
#   L2: Pinned Python patch version for deterministic builds
#   M6: backtest.py and yfinance dependency NOT in production image
#       (use a separate docker-compose override or local venv for backtesting)
#
# Build:   docker build -t rsi-bot:2.3 .
# Run:     docker run --env-file .env rsi-bot:2.3

FROM python:3.11.9-slim-bookworm

# System deps kept minimal. No build tools — all wheels are pure or
# prebuilt. tini for proper signal handling (SIGTERM forwarded).
RUN apt-get update \
 && apt-get install -y --no-install-recommends tini ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# Non-root user (defense in depth — if the bot is ever compromised,
# attacker doesn't get root in the container).
RUN useradd --create-home --shell /bin/bash --uid 1001 rsi
WORKDIR /app

# Install deps first for layer caching.
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir -r /app/requirements.txt

# Copy production code only. backtest.py explicitly EXCLUDED (M6).
# .dockerignore should also list backtest.py, .env, __pycache__, .git.
COPY config.py /app/config.py
COPY ibkr_helpers.py /app/ibkr_helpers.py
COPY dashboard.py /app/dashboard.py
COPY global_rsi_bot.py /app/global_rsi_bot.py

# State directory owned by the bot user (compose mounts /state).
RUN mkdir -p /state && chown -R rsi:rsi /app /state
USER rsi

# FastAPI dashboard port. Override in compose/env as needed.
EXPOSE 8000

# tini as PID 1 ensures SIGTERM from Railway/compose reaches Python,
# which flips the _shutdown flag and triggers graceful save_state().
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-u", "global_rsi_bot.py"]
