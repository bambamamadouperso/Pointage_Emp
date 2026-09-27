FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

# unixODBC : nécessaire au module pyodbc (source HFSQL via son pilote ODBC).
RUN apt-get update && apt-get install -y --no-install-recommends unixodbc && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
RUN mkdir -p /app/data
VOLUME ["/app/data"]

EXPOSE 8000
# Un seul worker : le planificateur tourne dans le processus de l'application.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
