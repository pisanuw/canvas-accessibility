FROM python:3.11-slim

# Install system dependencies (tesseract for OCR, poppler for pdf2image)
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr \
    poppler-utils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY webapp/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY fixes/ ./fixes/
COPY webapp/ ./webapp/
COPY ally_api.py .

EXPOSE 10000

CMD ["gunicorn", \
     "--worker-class", "gthread", \
     "--workers", "1", \
     "--threads", "4", \
     "--timeout", "600", \
     "--keep-alive", "65", \
     "--bind", "0.0.0.0:10000", \
     "webapp.app:app"]
