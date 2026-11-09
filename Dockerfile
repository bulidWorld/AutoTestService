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

EXPOSE 31010
CMD ["python", "-m", "app.main"]
