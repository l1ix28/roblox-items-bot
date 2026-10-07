import os
import time
import requests
from datetime import datetime, timedelta, timezone

WEBHOOK_URL = os.environ.get("WEBHOOK_URL")
if not WEBHOOK_URL:
    print("Critical Error: WEBHOOK_URL environment variable is not defined!")
    raise SystemExit(1)

# ---------------- Réglages ----------------
SEED_PAGES = 4                 # pages d'items mémorisées au démarrage (30 items par page)
NEW_DAYS = 7                   # un item créé il y a moins de X jours compte comme "nouveau"
ALERT_NEW_NON_LIMITED = False  # True = alerter aussi sur les nouveaux items Roblox non limited
TEST_ON_START = os.environ.get("TEST_ON_START") == "1"  # TEST_ON_START=1 envoie une alerte test au démarrage
INTERVAL = 30                  # secondes entre deux vérifications (monte à 60 si les 429 restent fréquents)
RETRIES = 4                    # essais par requête en cas de 429

API_URL = "https://catalog.roblox.com/v1/search/items/details"
BASE_PARAMS = {
    "Category": 1,             # 1 = tout
    "CreatorTargetId": 1,      # 1 = le compte Roblox
    "CreatorType": "User",     # obligatoire avec CreatorTargetId
    "SortType": 3,             # 3 = Updated (les plus récemment modifiés d'abord)
    "Limit": 30,
}
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; RobloxCatalogNotifier/1.0)"}
LIMITED_TAGS = {"Collectible", "Limited", "LimitedUnique"}

tracked_items = {}  # id -> True si l'item est limited


def is_limited(item):
    return bool(LIMITED_TAGS.intersection(item.get("itemRestrictions") or []))


def is_recent(item):
    created = item.get("itemCreatedUtc")
    if not created:
        return False
    try:
        dt = datetime.strptime(created.split(".")[0].rstrip("Z"), "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return False
    dt = dt.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - dt < timedelta(days=NEW_DAYS)


def retry_delay(response, attempt):
    try:
        base = int(response.headers.get("Retry-After", 5))
    except ValueError:
        base = 5
    return base + attempt * 3


def fetch_page(cursor=None):
    """Retourne (items, curseur de la page suivante), ou (None, None) si la requête a échoué."""
    params = dict(BASE_PARAMS)
    if cursor:
        params["Cursor"] = cursor

    for attempt in range(1, RETRIES + 1):
        try:
            response = requests.get(API_URL, params=params, headers=HEADERS, timeout=10)
        except Exception as e:
            print(f"Erreur réseau : {e}")
            time.sleep(5)
            continue

        if response.status_code == 429:
            wait = retry_delay(response, attempt)
            print(f"Limite de requêtes Roblox (429), nouvel essai dans {wait}s ({attempt}/{RETRIES})")
            time.sleep(wait)
            continue

        if response.status_code != 200:
            print(f"Erreur API Roblox {response.status_code} : {response.text[:200]}")
            return None, None

        body = response.json()
        items = [
            i for i in body.get("data", [])
            if i.get("creatorTargetId") == 1 and i.get("creatorType", "User") == "User"
        ]
        return items, body.get("nextPageCursor")

    print("Requête abandonnée après plusieurs essais, on réessaiera au prochain tour.")
    return None, None


def seed_tracked_items():
    """Mémorise l'état actuel des items sans envoyer d'alerte."""
    cursor = None
    count = 0
    pages = 0
    for _ in range(SEED_PAGES):
        items, cursor = fetch_page(cursor)
        if items is None:
            print("Mémorisation interrompue, on continue avec ce qu'on a déjà.")
            break
        for item in items:
            if item.get("id"):
                tracked_items[item["id"]] = is_limited(item)
        count += len(items)
        pages += 1
        if not cursor:
            break
        time.sleep(3)
    limited_count = sum(1 for v in tracked_items.values() if v)
    print(f"Démarrage : {count} items Roblox mémorisés ({limited_count} limited, {pages}/{SEED_PAGES} pages), sans alerte.")


def get_thumbnail(item):
    item_id = item["id"]
    if item.get("itemType") == "Bundle":
        url = "https://thumbnails.roblox.com/v1/bundles/thumbnails"
        params = {"bundleIds": item_id, "size": "420x420", "format": "Png"}
    else:
        url = "https://thumbnails.roblox.com/v1/assets"
        params = {"assetIds": item_id, "size": "420x420", "format": "Png"}
    try:
        r = requests.get(url, params=params, headers=HEADERS, timeout=10)
        if r.status_code == 200:
            data = r.json().get("data", [])
            if data and data[0].get("imageUrl"):
                return data[0]["imageUrl"]
    except Exception as e:
        print(f"Erreur miniature : {e}")
    return None


def send_discord_alert(item, reason):
    """reason : "new_limited", "now_limited" ou "new_item"."""
    item_id = item["id"]
    name = item.get("name", "Unknown item")
    price = item.get("price") or 0
    is_bundle = item.get("itemType") == "Bundle"
    kind = "Bundle" if is_bundle else "Item"
    item_url = f"https://www.roblox.com/{'bundles' if is_bundle else 'catalog'}/{item_id}"

    if reason == "new_limited":
        label, color = f"New Roblox Limited {kind}", 3447003
    elif reason == "now_limited":
        label, color = f"Roblox {kind} just became Limited", 15105570
    else:
        label, color = f"New Roblox {kind}", 3447003

    stock_text = ""
    if is_limited(item):
        stock = item.get("totalQuantity") or 0
        stock_text = f" 📦 Stock: {stock}" if stock > 0 else " 📦 Limited"

    price_text = f"{price} Robux" if price > 0 else (item.get("priceStatus") or "Free")

    embed = {
        "description": f"**[{name}]({item_url})**\n{label}{stock_text}\n🌐 [Roblox Page]({item_url})",
        "color": color,
        "fields": [
            {"name": "Creator", "value": "[Roblox](https://www.roblox.com/users/1/profile)", "inline": False},
            {"name": "💰 Price", "value": price_text, "inline": True},
        ],
        "footer": {"text": "Roblox Catalog Notifier"},
    }
    thumb = get_thumbnail(item)
    if thumb:
        embed["thumbnail"] = {"url": thumb}

    try:
        response = requests.post(WEBHOOK_URL, json={"embeds": [embed]}, timeout=10)
        if response.status_code in (200, 204):
            print(f"Alerte envoyée ({reason}) : {name}")
        else:
            print(f"Erreur Discord ({response.status_code}) : {response.text[:200]}")
    except Exception as e:
        print(f"Erreur webhook : {e}")


print("Bot de surveillance Roblox démarré...")
seed_tracked_items()
first_check = True

while True:
    items, _ = fetch_page()

    if items is None:
        time.sleep(INTERVAL)
        continue

    top = ", ".join(i.get("name", "?") for i in items[:3])
    print(f"[{time.strftime('%H:%M:%S')} UTC] {len(items)} items Roblox reçus. Début : {top}")

    if first_check:
        first_check = False
        if TEST_ON_START and items:
            send_discord_alert(items[0], "new_limited")
            print("Alerte test envoyée pour vérifier le webhook.")

    alerts = []
    for item in items:
        item_id = item.get("id")
        if not item_id:
            continue

        limited = is_limited(item)
        was_limited = tracked_items.get(item_id)  # None = jamais vu
        reason = None

        if was_limited is None:
            # item jamais vu : nouveau, ou ancien item qui remonte dans les modifiés
            if limited:
                reason = "new_limited" if is_recent(item) else "now_limited"
            elif ALERT_NEW_NON_LIMITED and is_recent(item):
                reason = "new_item"
        elif not was_limited and limited:
            reason = "now_limited"  # il n'était pas limited, il l'est devenu

        tracked_items[item_id] = limited
        if reason:
            alerts.append((item, reason))

    for item, reason in reversed(alerts):  # du plus ancien au plus récent
        send_discord_alert(item, reason)
        time.sleep(1)

    # on évite que la mémoire grossisse indéfiniment
    if len(tracked_items) > 2000:
        for key in list(tracked_items)[:500]:
            del tracked_items[key]

    time.sleep(INTERVAL)
