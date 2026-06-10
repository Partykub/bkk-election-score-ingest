FROM python:3.11-slim

WORKDIR /app

COPY ./hermes/update_worker/requirements.txt /tmp/update-worker-requirements.txt
RUN pip install --no-cache-dir -r /tmp/update-worker-requirements.txt

COPY ./hermes /app/hermes

ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1

CMD ["python", "-m", "hermes.update_worker"]