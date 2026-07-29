FROM python:3.12-alpine

LABEL org.opencontainers.image.title="mirrorwatch" \
      org.opencontainers.image.description="Watch HTTP endpoints for new and changed files, mirror them, get notified." \
      org.opencontainers.image.source="https://github.com/W0rkingChr1s/mirrorwatch" \
      org.opencontainers.image.licenses="MIT"

RUN adduser -D -u 10001 mirrorwatch

WORKDIR /app
COPY mirrorwatch/ ./mirrorwatch/
COPY pyproject.toml README.md ./

# No dependencies to install: mirrorwatch is standard library only.
RUN mkdir -p /data && chown -R mirrorwatch:mirrorwatch /data /app

USER mirrorwatch
VOLUME ["/data"]

ENV MIRRORWATCH_CONFIG=/config/config.json \
    MIRRORWATCH_STATE_FILE=/data/state.json \
    MIRRORWATCH_MIRROR_DIR=/data/mirror \
    MIRRORWATCH_ARCHIVE_DIR=/data/archive \
    MIRRORWATCH_HEALTH_MAX_AGE=45000 \
    PYTHONUNBUFFERED=1

HEALTHCHECK --interval=5m --timeout=15s --start-period=3m --retries=3 \
  CMD python -m mirrorwatch status --max-age ${MIRRORWATCH_HEALTH_MAX_AGE:-45000} || exit 1

ENTRYPOINT ["python", "-m", "mirrorwatch"]
CMD ["run"]
