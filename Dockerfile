FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    APP_CONFIG=config/application-prod.yaml

WORKDIR /service
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY config ./config
COPY docs ./docs
COPY db ./db
COPY scripts/sync_schema.py ./scripts/sync_schema.py

EXPOSE 32010
CMD ["sh", "-c", "python scripts/sync_schema.py && exec python -m app.main"]
