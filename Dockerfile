FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .
COPY static/ static/

RUN useradd --system --uid 1000 spoolprint && mkdir -p /data && chown spoolprint /data
USER spoolprint

EXPOSE 6464

HEALTHCHECK --interval=1m --timeout=5s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:6464/healthz')"

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "6464"]
