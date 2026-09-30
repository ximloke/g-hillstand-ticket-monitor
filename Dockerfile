FROM python:3.12-slim-bookworm
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt \
    && python -m playwright install --with-deps --only-shell chromium \
    && rm -rf /var/lib/apt/lists/*
COPY monitor.py .
ENV STATE_FILE=/data/state.json
VOLUME /data
# state.json is rewritten after every check, so a stale mtime means the loop is stuck.
HEALTHCHECK --interval=5m --timeout=10s --start-period=10m --retries=1 \
    CMD python -c "import os,sys,time; sys.exit(time.time()-os.path.getmtime('/data/state.json')>1800)"
CMD ["python", "monitor.py", "--daemon"]
