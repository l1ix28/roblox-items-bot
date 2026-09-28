import os
import time
import requests

# Récupération sécurisée du Webhook depuis Railway
WEBHOOK_URL = os.environ.get("WEBHOOK_URL")

if not WEBHOOK_URL:
    print("Erreur critique : La variable d'environnement WEBHOOK_URL n'est pas définie !")
    exit(1)

# Ensemble pour stocker les IDs des items déjà détectés (pour éviter les doublons)
seen_item_ids = set()

def get_latest_limiteds():
    # URL de l'API Catalogue Roblox
    url = "https://catalog.roblox.com/v1/search/items/details?category=11&salesTypeFilter=1&sortType=3&limit=10"
    
    try:
        response = requests.get(url, timeout=10)
        if response.status_code == 200:
            data = response.json()
            items = data.get("data", [])
            
            official_limiteds = []
            for item in items:
                # 1. Vérifier si c'est un Limited / Collectible
                restrictions = item.get("itemRestrictions", [])
                is_collectible = "Collectible" in restrictions or "Limited" in restrictions or item.get("isLimited") == True
                
                if is_collectible:
                    # 2. Vérifier si le créateur est officiellement Roblox
                    creator_id = item.get("creatorId")
                    creator_name = item.get("creatorName", "")
                    
                    # On filtre pour ne garder que les objets publiés par Roblox (ID 1 ou nom "Roblox")
                    if creator_name == "Roblox" or creator_id == 1:
                        official_limiteds.append(item)
                        
            return official_limiteds
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
        "title": "🚨 OFFICIEL ROBLOX LIMITED DÉTECTÉ !",
        "description": f"**[{item_name}]({item_url})**",
        "color": 16711680, # Rouge
        "fields": [
            {"name": "💰 Prix", "value": f"{item_price} Robux", "inline": True},
            {"name": "📦 Type", "value": f"{item_type}", "inline": True}
        ],
        "image": {"url": thumb_url},
        "footer": {"text": "Roblox Official Limited Notifier"}
    }

    payload = {"embeds": [embed]}
    
    try:
        response = requests.post(WEBHOOK_URL, json=payload, timeout=10)
        if response.status_code == 204 or response.status_code == 200:
            print(f"Alerte envoyée avec succès pour l'item officiel : {item_name}")
        else:
            print(f"Erreur Discord (Code {response.status_code}): {response.text}")
    except Exception as e:
        print(f"Erreur d'envoi du webhook : {e}")

print("Bot de surveillance des Limiteds Officiels Roblox démarré...")

# Boucle principale du bot
while True:
    items = get_latest_limiteds()
    for item in items:
        item_id = item.get("id")
        if item_id and item_id not in seen_item_ids:
            # Marquer comme vu pour éviter le spam
            seen_item_ids.add(item_id)
            
            # Garder une limite de 500 IDs en mémoire
            if len(seen_item_ids) > 500:
                seen_item_ids.pop()
                
            # Envoyer l'alerte sur Discord
            send_discord_alert(item)

    # Pause de 60 secondes pour éviter le rate limit de l'API Roblox
    time.sleep(60)
