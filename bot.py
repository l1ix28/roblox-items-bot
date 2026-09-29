import os
import time
import requests

# Retrieve webhook URL from environment variables
WEBHOOK_URL = os.environ.get("WEBHOOK_URL")

if not WEBHOOK_URL:
    print("Critical Error: WEBHOOK_URL environment variable is not defined!")
    exit(1)

# Store known items with their previous limited status to detect when an old item turns limited
tracked_items = {}

def get_latest_roblox_items():
    # Roblox Catalog API endpoint
    url = "https://catalog.roblox.com/v1/search/items/details?category=11&salesTypeFilter=1&sortType=3&limit=30"
    
    try:
        response = requests.get(url, timeout=10)
        if response.status_code == 200:
            data = response.json()
            items = data.get("data", [])
            
            roblox_items = []
            for item in items:
                creator_id = item.get("creatorId")
                creator_name = item.get("creatorName", "")
                
                # Keep items published officially by Roblox
                if creator_name == "Roblox" or creator_id == 1:
                    roblox_items.append(item)
                        
            return roblox_items
    except Exception as e:
        print(f"Error during API request: {e}")
    return []

def send_discord_alert(item, is_new=True, became_limited=False):
    item_id = item.get("id")
    item_name = item.get("name", "Unknown item")
    item_price = item.get("price", 0)
    
    # Determine item type description (e.g., "New Roblox Hat")
    bundle_type = item.get("itemType", "Asset")
    description_text = f"New Roblox {bundle_type}"
    
    # Check restrictions and stock limit (if available)
    restrictions = item.get("itemRestrictions", [])
    is_collectible = "Collectible" in restrictions or "Limited" in restrictions or item.get("isLimited") == True
    
    # Look for stock limit if it's a limited item
    stock_text = ""
    # Roblox sometimes provides quantity limit in restriction or specific fields
    quantity = item.get("unitsAvailable") or item.get("remaining")
    if is_collectible and quantity:
        stock_text = f" 🇦🇹 Limit {quantity}"
    elif is_collectible:
        stock_text = " 🇦🇹 Limited"

    # Links
    item_url = f"https://www.roblox.com/catalog/{item_id}"
    # Try-on link redirecting to the catalog item page or avatar shop view
    try_on_url = f"https://www.roblox.com/catalog/{item_id}" 
    
    # Thumbnail image
    thumb_url = f"https://www.roblox.com/asset-thumbnail/image?assetId={item_id}&width=420&height=420&format=png"

    # Color code (Blue bar on the left like Rolimon's)
    color_code = 3447003 

    # Build description mimicking the screenshot layout
    desc_markdown = f"**[{item_name}]({item_url})**\n{description_text}{stock_text}\n🌐 [Roblox Page]({item_url})  👕 [Try On]({try_on_url})"

    embed = {
        "description": desc_markdown,
        "color": color_code,
        "thumbnail": {"url": thumb_url},
        "fields": [
            {
                "name": "Creator",
                "value": "[Roblox](https://www.roblox.com/users/1/profile)",
                "inline": False
            },
            {
                "name": "💰 Price",
                "value": f"{item_price} Robux" if item_price > 0 else "Free",
                "inline": True
            }
        ],
        "footer": {"text": "Roblox Catalog Notifier"}
    }

    payload = {"embeds": [embed]}
    
    try:
        response = requests.post(WEBHOOK_URL, json=payload, timeout=10)
        if response.status_code in [200, 204]:
            print(f"Alert sent for: {item_name}")
        else:
            print(f"Discord Error ({response.status_code}): {response.text}")
    except Exception as e:
        print(f"Webhook error: {e}")

print("Roblox style-matched monitoring bot started...")

# Main loop with tracking mechanism for old items turning limited
while True:
    items = get_latest_roblox_items()
    
    for item in items:
        item_id = item.get("id")
        if not item_id:
            continue
            
        restrictions = item.get("itemRestrictions", [])
        is_collectible = "Collectible" in restrictions or "Limited" in restrictions or item.get("isLimited") == True
        
        # If the item is completely new to the bot
        if item_id not in tracked_items:
            tracked_items[item_id] = is_collectible
            send_discord_alert(item, is_new=True)
            
        # If an existing item suddenly switches to limited/collectible
        elif tracked_items[item_id] == False and is_collectible == True:
            tracked_items[item_id] = True
            send_discord_alert(item, is_new=False, became_limited=True)

    # Clean up memory if dictionary gets too large
    if len(tracked_items) > 1000:
        keys_to_remove = list(tracked_items.keys())[:200]
        for k in keys_to_remove:
            del tracked_items[k]

    # 20-second interval to stay safe from rate limits
    time.sleep(20)
