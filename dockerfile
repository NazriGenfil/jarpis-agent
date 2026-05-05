# Gunakan image Python yang ringan
FROM python:3.11-slim

# Set working directory di dalam container
WORKDIR /app

# Install dependencies sistem yang dibutuhkan (untuk websockets/aiohttp)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements dulu biar caching layer Docker efisien
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy seluruh script dan file project
COPY . .

# Jalankan script utama
CMD ["python", "main.py"]