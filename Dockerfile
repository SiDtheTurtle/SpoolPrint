# niimbluelib talks to the printer from the browser. It's fetched from npm at a
# pinned version, and the build fails if the download doesn't match the hash.
# To update: change both values, using the new package's SHA-256.
FROM python:3.12-slim AS niimbluelib
ARG NIIMBLUELIB_VERSION=0.49.1
ARG NIIMBLUELIB_SHA256=612b2498a7d803a5b4ab5416207e047d9666322e462d4b31ccb315ac0b024b05
ADD --checksum=sha256:${NIIMBLUELIB_SHA256} \
    https://registry.npmjs.org/@mmote/niimbluelib/-/niimbluelib-${NIIMBLUELIB_VERSION}.tgz /niimbluelib.tgz
RUN tar -xzf /niimbluelib.tgz -C / && mkdir /out \
 && cp /package/dist/umd/niimbluelib.min.js /out/ \
 && cp /package/LICENSE /out/niimbluelib.LICENSE

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .
COPY static/ static/
COPY --from=niimbluelib /out/ static/vendor/

RUN useradd --system --uid 1000 spoolprint && mkdir -p /data && chown spoolprint /data
USER spoolprint

EXPOSE 6464

HEALTHCHECK --interval=1m --timeout=5s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:6464/healthz')"

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "6464"]
