FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Directorio de uploads por defecto (se sobreescribe con Railway Volume en /data/uploads)
RUN mkdir -p app/static/uploads

ENV PYTHONPATH=/app

EXPOSE 8000

CMD uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}
