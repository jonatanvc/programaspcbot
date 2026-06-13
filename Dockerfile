# Usar imagen oficial de Python ligera
FROM python:3.11-slim

# Evitar que Python escriba archivos .pyc y forzar salida sin buffer para los logs
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Instalar dependencias del sistema operativo (7zip y aria2) necesarias para el bot
RUN apt-get update && apt-get install -y \
    curl \
    p7zip-full \
    aria2 \
    && rm -rf /var/lib/apt/lists/*

# Crear y establecer el directorio de trabajo
WORKDIR /app

# Copiar el archivo de dependencias primero (aprovecha la caché de Docker)
COPY requirements.txt .

# Instalar dependencias de Python
RUN pip install --no-cache-dir -r requirements.txt

# Instalar navegadores de Playwright y sus dependencias del sistema
RUN playwright install chromium
RUN playwright install-deps chromium

# Copiar el resto del código del bot
COPY . .

# Comando principal para iniciar el bot
CMD ["python", "bot.py"]
