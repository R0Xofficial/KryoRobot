FROM python:3.11-slim

WORKDIR /app

# Natychmiastowe wyświetlanie logów w Portainerze
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Zależności systemowe
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Instalacja pakietów Pythona
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Utworzenie katalogu na trwałą bazę danych SQLite
RUN mkdir -p /app/data

# Skopiowanie reszty kodu aplikacji
COPY . .

CMD ["python", "main.py"]
