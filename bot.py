import os
import time
import requests

# Récupération sécurisée du Webhook depuis l'environnement (Render / Système)
WEBHOOK_URL = os.environ.get("WEBHOOK_URL")

if not WEBHOOK_URL:
    print("Erreur critique : La variable d'environnement WEBHOOK_URL n'est pas définie !")
    exit(1)

# Ensemble pour stocker les IDs des items déjà détectés (pour éviter les doublons)
seen_item_ids = set()

def get_latest_limiteds():
    # Endpoint de l'API Catalogue Roblox pour chercher des objets récents
    url = "https://catalog.roblox.com/v1/search/items/details?category=11&salesTypeFilter=1&sortType=3&limit=10"
    
    try:
        response = requests.get(url, timeout=10)
        if response.status_code == 200:
            data = response.json()
            return data.get("data", [])
    except Exception as e:
        print(f"Erreur lors de la requête API : {e}")
    return []

def send_discord_alert(item):
    item_id = item.get("id")
    item_name = item.get("name", "Objet inconnu")
    item_price = item.get("price", "Gratuit / Inconnu")
    item_type = item.get("itemType", "Asset")
    
    # Construction du lien direct vers l'item sur le catalogue Roblox
    item_url = f"https://www.roblox.com/catalog/{item_id}"
    
    # Génération de la miniature via l'API Roblox
    thumb_url = f"https://www.roblox.com/asset-thumbnail/image?assetId={item_id}&width=420&height=420&format=png"

    # Format de l'embed Discord
    embed = {
        "title": "🚨 NOUVEAU LIMITED / UGC DÉTECTÉ !",
        "description": f"**[{item_name}]({item_url})**",
        "color": 16711680, # Couleur rouge/orange
        "fields": [
            {"name": "💰 Prix", "value": f"{item_price} Robux", "inline": True},
            {"name": "📦 Type", "value": f"{item_type}", "inline": True}
        ],
        "image": {"url": thumb_url},
        "footer": {"text": "Roblox Limited Notifier Bot"}
    }

    payload = {"embeds": [embed]}
    
    try:
        response = requests.post(WEBHOOK_URL, json=payload, timeout=10)
        if response.status_code == 204 or response.status_code == 200:
            print(f"Alerte envoyée avec succès pour : {item_name}")
        else:
            print(f"Erreur Discord (Code {response.status_code}): {response.text}")
    except Exception as e:
        print(f"Erreur d'envoi du webhook : {e}")

print("Bot de surveillance des Limiteds démarré...")

# Boucle principale du bot
while True:
    items = get_latest_limiteds()
    for item in items:
        item_id = item.get("id")
        if item_id and item_id not in seen_item_ids:
            # Marquer comme vu pour éviter le spam au prochain tour
            seen_item_ids.add(item_id)
            
            # Empêcher le set de grandir indéfiniment en mémoire (garder les 500 derniers)
            if len(seen_item_ids) > 500:
                seen_item_ids.pop()
                
            # Envoyer l'alerte
            send_discord_alert(item)

    # Pause de 60 secondes pour éviter le rate limit de l'API Roblox
    time.sleep(60)