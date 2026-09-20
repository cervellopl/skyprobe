FROM python:3.12-slim

# astrometry.net solver + Tycho-2 index files (fields from ~20' to several degrees).
# Add wider/narrower index files with scripts/install_indexes.sh, or leave them out
# entirely and use nova.astrometry.net by setting ASTROMETRY_API_KEY.
ARG INDEXES="astrometry-data-tycho2-08-littleendian astrometry-data-tycho2-09-littleendian astrometry-data-tycho2-10-19-littleendian"
RUN apt-get update && apt-get install -y --no-install-recommends \
        astrometry.net $INDEXES \
        libraw-dev libde265-0 libheif1 curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY static ./static

ENV DATA_DIR=/data WORKERS=1 PYTHONUNBUFFERED=1
VOLUME ["/data"]
EXPOSE 8765
HEALTHCHECK --interval=60s --timeout=10s CMD curl -fsS http://localhost:8765/api/health || exit 1
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8765"]
