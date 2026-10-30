# Utilise une image Python officielle légère
FROM python:3.11-slim

# Définit le dossier de travail dans le conteneur
WORKDIR /app

# Copie et installe les dépendances s'il y a un requirements.txt
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copie tout le reste de ton code dans le conteneur
COPY . .

# Commande pour lancer ton bot (remplace main.py par le nom de ton fichier principal)
CMD ["python", "bot.py"]
