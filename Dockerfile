FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .
# Ohne diesen Ordner laeuft zwar die API, die Startseite aber nicht.
COPY templates/ ./templates/
ENV PORT=8000
EXPOSE 8000
CMD gunicorn app:app --bind 0.0.0.0:$PORT --workers 1 --threads 4
