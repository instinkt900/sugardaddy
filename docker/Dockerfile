FROM python:3.12-slim-bookworm

# tzdata so zoneinfo can resolve the configured display timezone.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml ./
COPY sugardaddy ./sugardaddy
RUN pip install --no-cache-dir . && rm -rf /root/.cache

# /data holds the SQLite DB (mounted as a volume in compose).
VOLUME ["/data"]
EXPOSE 8080

ENTRYPOINT ["sugardaddy"]
CMD ["serve", "-c", "/app/config.toml"]
