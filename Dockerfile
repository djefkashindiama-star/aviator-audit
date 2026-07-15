FROM node:22-bookworm-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates python3 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY dashboard/package.json dashboard/package-lock.json ./dashboard/
RUN cd dashboard && npm ci

COPY dashboard ./dashboard
RUN cd dashboard && npm run build

COPY aviator_audit.py render_start.py ./

ENV PYTHONUNBUFFERED=1 \
    AVIATOR_DATA_DIR=/var/data \
    AVIATOR_DURATION_DAYS=20

EXPOSE 10000
CMD ["python3", "render_start.py"]
