FROM python:3.11-slim

# Install system dependencies needed for Python packages
# libzbar0: required by pyzbar (barcode scanning)
# libjpeg62-turbo, zlib1g: required by Pillow (image processing)
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        libzbar0 \
        libjpeg62-turbo \
        zlib1g \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY inventory-system/ .

# Ensure data & uploads directories exist (for persistent volume mount)
RUN mkdir -p /app/inventory-system/data /app/inventory-system/uploads

EXPOSE 5000

CMD ["sh", "-c", "gunicorn web:app --bind 0.0.0.0:${PORT:-5000}"]
