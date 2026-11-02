# Build from the _work/ folder:  podman build -f ba_portal/Dockerfile -t ba-portal .
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends poppler-utils libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*
RUN useradd -m -u 1000 user
WORKDIR /app
COPY ba_portal/requirements.txt /app/ba_portal/requirements.txt
RUN pip install --no-cache-dir -r ba_portal/requirements.txt
COPY ba_tool/ba_core.py ba_tool/ba_validate.py /app/ba_tool/
COPY --chown=user ba_portal/ /app/ba_portal/
RUN mkdir -p /app/ba_portal/data /app/ba_portal/logs && chown -R user /app/ba_portal/data /app/ba_portal/logs
USER user
WORKDIR /app/ba_portal
ENV HOST=0.0.0.0 PORT=7860 TRUST_PROXY=1 FORWARDED_ALLOW_IPS=* DATA_DIR=/app/ba_portal/data
EXPOSE 7860
CMD ["sh", "-c", "exec python -m uvicorn server:app --host $HOST --port $PORT --workers 1 --proxy-headers --forwarded-allow-ips \"$FORWARDED_ALLOW_IPS\" --timeout-keep-alive 15"]
