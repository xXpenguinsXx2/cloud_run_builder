FROM python:3.14-slim

ENV PYTHONUNBUFFERED=1 \
    PORT=8080

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py ./

EXPOSE 8080

CMD ["sh", "-c", "functions-framework --source=main.py --target=import_gcs_to_postgresql --host=0.0.0.0 --port=${PORT}"]
