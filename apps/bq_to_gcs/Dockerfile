FROM python:3.14-slim

ARG APP_ENV=dev
ENV PYTHONUNBUFFERED=1 \
    APP_ENV=${APP_ENV} \
    PORT=8080

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py ./

EXPOSE 8080

CMD ["sh", "-c", "functions-framework --source=main.py --target=export_table_to_gcs --host=0.0.0.0 --port=${PORT}"]