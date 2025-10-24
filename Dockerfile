# --- Dockerfile for Fly.io deployment ---
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1         PYTHONUNBUFFERED=1

# System deps (add others as needed)
RUN apt-get update && apt-get install -y --no-install-recommends         build-essential         curl         && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Keep layers small
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Copy the app
COPY . /app

# Default port for Fly
ENV PORT=8080
# Persist uploads/db if you mount a volume at /data
ENV UPLOAD_FOLDER=/data/uploads

# Gunicorn (threads worker plays nice with Flask & I/O)
CMD ["gunicorn", "-w", "4", "-k", "gthread", "-b", "0.0.0.0:8080", "app:app"]
