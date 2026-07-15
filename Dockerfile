FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY auditor ./auditor
COPY webapp ./webapp
RUN pip install --no-cache-dir .
# Bind to loopback by default: this app holds live admin credentials and has
# NO auth layer yet. Exposing it on 0.0.0.0 (to publish the port outside the
# container) REQUIRES that auth layer first — only then override MA_BIND.
ENV MA_DATA_DIR=/data MA_BIND=127.0.0.1:8484
VOLUME /data
EXPOSE 8484
CMD ["migration-auditor", "serve"]
