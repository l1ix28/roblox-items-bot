import os
import time
import requests

# Récupération du Webhook depuis Railway
WEBHOOK_URL = os.environ.get("WEBHOOK_URL")

if not WEBHOOK_URL:
    print("Erreur critique : La variable d'environnement WEBHOOK_URL n'est pas définie !")
    exit(1)

# On stock les IDs des items déjà détectés (pas de doublons)
seen_item_ids = set()

def get_latest_roblox_items():
    # URL de l'API Catalogue Roblox
    url = "https://catalog.roblox.com/v1/search/items/details?category=11&salesTypeFilter=1&sortType=3&limit=10"
    
    try:
        response = requests.get(url, timeout=10)
        if response.status_code == 200:
            data = response.json()
            items = data.get("data", [])
            
            roblox_items = []
            for item in items:
                # Vérifier si le créateur est officiellement Roblox
                creator_id = item.get("creatorId")
                creator_name = item.get("creatorName", "")
                
                # On garde tous les objets publiés par Roblox (limited ou non)
                if creator_name == "Roblox" or creator_id == 1:
                    roblox_items.append(item)
                        
            return roblox_items
    except Exception as e:
        print(f"Erreur lors de la requête API : {e}")
    return []

def send_discord_alert(item):
    item_id = item.get("id")
    item_name = item.get("name", "Objet inconnu")
    item_price = item.get("price", "Gratuit / Inconnu")
    item_type = item.get("itemType", "Asset")
    
    # Vérifier si l'objet est un limited pour adapter le titre de l'alerte
    restrictions = item.get("itemRestrictions", [])
    is_collectible = "Collectible" in restrictions or "Limited" in restrictions or item.get("isLimited") == True
    
    title_text = "🚨 NEW ROBLOX LIMITED !" if is_collectible else "📦 NEW ROBLOX ITEM !"
    color_code = 16711680 if is_collectible else 3447003 # Rouge pour limited, Bleu pour normal

    # Lien direct vers l'item sur le catalogue Roblox
    item_url = f"https://www.roblox.com/catalog/{item_id}"
    
    # Génération de la minia via l'API Roblox
    thumb_url = f"https://www.roblox.com/asset-thumbnail/image?assetId={item_id}&width=420&height=420&format=png"

    # Format de l'embed Discord
    embed = {
        "title": title_text,
        "description": f"**[{item_name}]({item_url})**",
        "color": color_code,
        "fields": [
            {"name": "💰 Prix", "value": f"{item_price} Robux", "inline": True},
            {"name": "📦 Type", "value": f"{item_type}", "inline": True}
        ],
        "image": {"url": thumb_url},
        "footer": {"text": "Roblox Official Notifier Bot"}
    }

    payload = {"embeds": [embed]}
    
    try:
        response = requests.post(WEBHOOK_URL, json=payload, timeout=10)
        if response.status_code == 204 or response.status_code == 200:
            print(f"Alerte envoyée pour l'item Roblox : {item_name}")
        else:
            print(f"Erreur Discord (Code {response.status_code}): {response.text}")
    except Exception as e:
        print(f"Erreur d'envoi du webhook : {e}")

print("Bot de surveillance des items officiels Roblox démarré...")

# Boucle principale du bot
while True:
    items = get_latest_roblox_items()
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

    # Pause de 20 secondes pour éviter le rate limit 
    time.sleep(20)
