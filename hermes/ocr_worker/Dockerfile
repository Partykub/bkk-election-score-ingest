FROM python:3.11-slim

WORKDIR /app

COPY ./hermes/ocr_worker/requirements.txt /tmp/ocr-worker-requirements.txt
RUN pip install --no-cache-dir -r /tmp/ocr-worker-requirements.txt

COPY ./hermes /app/hermes

ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1

CMD ["python", "-m", "hermes.ocr_worker"]