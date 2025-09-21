# Image Playwright Python avec Chrome/Firefox/WebKit et deps
FROM mcr.microsoft.com/playwright/python:v1.47.0-noble

# Répertoire de travail
WORKDIR /app

# Dépendances Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Ton code
COPY app.py .

# (Optionnel) Si tu utilises des certificats/CA internes, etc., copie-les ici.

# Commande lancée par le cron Railway (one-shot puis exit)
CMD ["python", "app.py"]