import os
import time
import requests

# Retrieve webhook URL from environment variables
WEBHOOK_URL = os.environ.get("WEBHOOK_URL")

if not WEBHOOK_URL:
    print("Critical Error: WEBHOOK_URL environment variable is not defined!")
    exit(1)

# Store seen item IDs to prevent duplicate alerts
seen_item_ids = set()

def get_latest_roblox_items():
    # Roblox Catalog API endpoint
    url = "https://catalog.roblox.com/v1/search/items/details?category=11&salesTypeFilter=1&sortType=3&limit=10"
    
    try:
        response = requests.get(url, timeout=10)
        if response.status_code == 200:
            data = response.json()
            items = data.get("data", [])
            
            roblox_items = []
            for item in items:
                # Check if the creator is officially Roblox
                creator_id = item.get("creatorId")
                creator_name = item.get("creatorName", "")
                
                # Keep all items published by Roblox (limited or not)
                if creator_name == "Roblox" or creator_id == 1:
                    roblox_items.append(item)
                        
            return roblox_items
    except Exception as e:
        print(f"Error during API request: {e}")
    return []

def send_discord_alert(item):
    item_id = item.get("id")
    item_name = item.get("name", "Unknown item")
    item_price = item.get("price", "Free / Unknown")
    item_type = item.get("itemType", "Asset")
    
    # Check if the item is a limited to adapt the alert title
    restrictions = item.get("itemRestrictions", [])
    is_collectible = "Collectible" in restrictions or "Limited" in restrictions or item.get("isLimited") == True
    
    title_text = "🚨 NEW ROBLOX LIMITED !" if is_collectible else "📦 NEW ROBLOX ITEM !"
    color_code = 16711680 if is_collectible else 3447003 # Red for limited, Blue for normal

    # Direct link to the item on the Roblox catalog
    item_url = f"https://www.roblox.com/catalog/{item_id}"
    
    # Generate thumbnail via Roblox API
    thumb_url = f"https://www.roblox.com/asset-thumbnail/image?assetId={item_id}&width=420&height=420&format=png"

    # Discord embed structure
    embed = {
        "title": title_text,
        "description": f"**[{item_name}]({item_url})**",
        "color": color_code,
        "fields": [
            {"name": "💰 Price", "value": f"{item_price} Robux", "inline": True},
            {"name": "📦 Type", "value": f"{item_type}", "inline": True}
        ],
        "image": {"url": thumb_url},
        "footer": {"text": "Roblox Official Notifier Bot"}
    }

    payload = {"embeds": [embed]}
    
    try:
        response = requests.post(WEBHOOK_URL, json=payload, timeout=10)
        if response.status_code == 204 or response.status_code == 200:
            print(f"Alert sent for Roblox item: {item_name}")
        else:
            print(f"Discord Error (Code {response.status_code}): {response.text}")
    except Exception as e:
        print(f"Error sending webhook: {e}")

print("Official Roblox item monitoring bot started...")

# Main bot loop
while True:
    items = get_latest_roblox_items()
    for item in items:
        item_id = item.get("id")
        if item_id and item_id not in seen_item_ids:
            # Mark as seen to prevent spam
            seen_item_ids.add(item_id)
            
            # Keep a maximum limit of 500 IDs in memory
            if len(seen_item_ids) > 500:
                seen_item_ids.pop()
                
            # Send alert to Discord
            send_discord_alert(item)

    # 20-second pause to avoid rate limits
    time.sleep(20)
