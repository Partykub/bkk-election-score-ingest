FROM python:3.11-slim

WORKDIR /app

COPY ./hermes/results_api/requirements.txt /tmp/results-api-requirements.txt
RUN pip install --no-cache-dir -r /tmp/results-api-requirements.txt

COPY ./hermes /app/hermes

ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1

CMD ["uvicorn", "hermes.results_api.app:app", "--host", "0.0.0.0", "--port", "8080"]
