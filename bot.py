import os
import json
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
ALERT_BACK_ON_SALE = True      # alerte quand un item hors vente repasse en vente
INCLUDE_OFFSALE = True         # inclut les items hors vente dans la recherche (mets False si l'API renvoie une erreur 400)
TEST_ON_START = os.environ.get("TEST_ON_START") == "1"  # TEST_ON_START=1 envoie une alerte test au démarrage
INTERVAL = 60                  # secondes entre deux vérifications
RETRIES = 4                    # essais par requête en cas de 429

# Items surveillés en plus, même s'ils ne sont pas modifiés récemment.
# ("Bundle", id) pour un lien roblox.com/bundles/ID/...   ("Asset", id) pour un lien roblox.com/catalog/ID/...
WATCHLIST = [
    ("Bundle", 319226),  # Korblox Deathwalker
]

API_URL = "https://catalog.roblox.com/v1/search/items/details"
DETAILS_URL = "https://catalog.roblox.com/v1/catalog/items/details"
BASE_PARAMS = {
    "Category": 1,             # 1 = tout
    "CreatorTargetId": 1,      # 1 = le compte Roblox
    "CreatorType": "User",     # obligatoire avec CreatorTargetId
    "SortType": 3,             # 3 = Updated (les plus récemment modifiés d'abord)
    "Limit": 30,
}
if INCLUDE_OFFSALE:
    BASE_PARAMS["IncludeNotForSale"] = "true"

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; RobloxCatalogNotifier/1.0)"}
LIMITED_TAGS = {"Collectible", "Limited", "LimitedUnique"}

tracked_items = {}   # "Type:id" -> {"limited": bool, "on_sale": bool}
csrf_token = None


# ---------------- Outils ----------------
def item_key(item):
    return f"{item.get('itemType', 'Asset')}:{item.get('id')}"


def normalize(value):
    return str(value or "").replace(" ", "").replace("_", "").lower()


def is_limited(item):
    return bool(LIMITED_TAGS.intersection(item.get("itemRestrictions") or []))


def is_on_sale(item):
    """Vrai si l'item peut être acheté directement chez Roblox."""
    status = normalize(item.get("priceStatus"))
    if status == "offsale":
        return False
    # limited avec un stock : en vente tant qu'il reste des exemplaires chez Roblox
    if is_limited(item) and (item.get("totalQuantity") or 0) > 0:
        return (item.get("unitsAvailableForConsumption") or 0) > 0
    if status == "free":
        return True
    return item.get("price") is not None


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


def debug_item(item):
    print(
        f"  - {item.get('name')} | priceStatus={item.get('priceStatus')!r} | price={item.get('price')!r} | "
        f"stock={item.get('unitsAvailableForConsumption')!r}/{item.get('totalQuantity')!r} | "
        f"on_sale={is_on_sale(item)} | limited={is_limited(item)}"
    )


def retry_delay(response, attempt):
    try:
        base = int(response.headers.get("Retry-After", 5))
    except ValueError:
        base = 5
    return base + attempt * 3


# ---------------- Requêtes Roblox ----------------
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


def fetch_watchlist():
    """Détails des items de la WATCHLIST (requête POST, avec jeton CSRF)."""
    global csrf_token
    if not WATCHLIST:
        return []

    payload = {"items": [{"itemType": t, "id": i} for t, i in WATCHLIST]}

    for attempt in range(1, RETRIES + 1):
        headers = dict(HEADERS)
        if csrf_token:
            headers["x-csrf-token"] = csrf_token
        try:
            response = requests.post(DETAILS_URL, json=payload, headers=headers, timeout=10)
        except Exception as e:
            print(f"Erreur réseau (watchlist) : {e}")
            time.sleep(5)
            continue

        if response.status_code == 403 and response.headers.get("x-csrf-token"):
            csrf_token = response.headers["x-csrf-token"]  # Roblox demande un jeton, on réessaie avec
            continue

        if response.status_code == 429:
            wait = retry_delay(response, attempt)
            print(f"Limite de requêtes Roblox (429) sur la watchlist, nouvel essai dans {wait}s")
            time.sleep(wait)
            continue

        if response.status_code != 200:
            print(f"Erreur watchlist {response.status_code} : {response.text[:200]}")
            return []

        return response.json().get("data", [])

    return []


# ---------------- Détection ----------------
def evaluate(item):
    """Compare l'item à l'état mémorisé, met à jour la mémoire, et retourne une raison d'alerte (ou None)."""
    key = item_key(item)
    limited = is_limited(item)
    on_sale = is_on_sale(item)
    previous = tracked_items.get(key)
    reason = None

    if previous is None:
        # item jamais vu : nouveau, ou ancien item qui remonte dans les modifiés
        if limited:
            reason = "new_limited" if is_recent(item) else "now_limited"
        elif ALERT_NEW_NON_LIMITED and is_recent(item):
            reason = "new_item"
    else:
        if ALERT_BACK_ON_SALE and not previous["on_sale"] and on_sale:
            reason = "back_on_sale"      # il était hors vente, il est de nouveau en vente
        elif not previous["limited"] and limited:
            reason = "now_limited"       # il n'était pas limited, il l'est devenu

    tracked_items[key] = {"limited": limited, "on_sale": on_sale}
    return reason


def seed_tracked_items():
    """Mémorise l'état actuel des items sans envoyer d'alerte (patient face aux erreurs 429)."""
    cursor = None
    count = 0
    pages = 0
    for _ in range(SEED_PAGES):
        items, next_cursor = None, None
        for attempt in range(1, 4):
            items, next_cursor = fetch_page(cursor)
            if items is not None:
                break
            print(f"Mémorisation : page {pages + 1} indisponible, nouvel essai dans 20s ({attempt}/3)")
            time.sleep(20)

        if items is None:
            print("Mémorisation interrompue, on continue avec ce qu'on a déjà.")
            break

        if pages == 0:
            print("Exemples d'items reçus :")
            for item in items[:5]:
                debug_item(item)

        for item in items:
            if item.get("id"):
                evaluate(item)
        count += len(items)
        pages += 1
        cursor = next_cursor
        if not cursor:
            break
        time.sleep(10)

    limited_count = sum(1 for v in tracked_items.values() if v["limited"])
    on_sale_count = sum(1 for v in tracked_items.values() if v["on_sale"])
    print(
        f"Démarrage : {count} items Roblox mémorisés ({limited_count} limited, {on_sale_count} en vente, "
        f"{pages}/{SEED_PAGES} pages), sans alerte."
    )

    watch_items = fetch_watchlist()
    for item in watch_items:
        evaluate(item)
    print(f"Watchlist : {len(watch_items)}/{len(WATCHLIST)} items mémorisés.")
    for item in watch_items:
        debug_item(item)
    if watch_items:
        print("Réponse brute du premier item surveillé : " + json.dumps(watch_items[0])[:1500])


# ---------------- Discord ----------------
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
    """reason : "new_limited", "now_limited", "back_on_sale" ou "new_item"."""
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
    elif reason == "back_on_sale":
        label, color = f"{kind} is back on sale!", 3066993
    else:
        label, color = f"New Roblox {kind}", 3447003

    stock_text = ""
    if is_limited(item):
        stock = item.get("totalQuantity") or 0
        stock_text = f" 📦 Stock: {stock}" if stock > 0 else " 📦 Limited"

    creator_id = item.get("creatorTargetId")
    creator_name = item.get("creatorName", "Roblox")
    if item.get("creatorType") == "Group":
        creator_link = f"https://www.roblox.com/communities/{creator_id}"
    else:
        creator_link = f"https://www.roblox.com/users/{creator_id or 1}/profile"

    price_text = f"{price} Robux" if price > 0 else (item.get("priceStatus") or "Free")

    embed = {
        "description": f"**[{name}]({item_url})**\n{label}{stock_text}\n🌐 [Roblox Page]({item_url})",
        "color": color,
        "fields": [
            {"name": "Creator", "value": f"[{creator_name}]({creator_link})", "inline": False},
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


# ---------------- Boucle principale ----------------
print("Bot de surveillance Roblox démarré...")
seed_tracked_items()
first_check = True

while True:
    items, _ = fetch_page()
    watch_items = fetch_watchlist()

    if items is None and not watch_items:
        time.sleep(INTERVAL)
        continue

    items = (items or []) + watch_items

    top = ", ".join(i.get("name", "?") for i in items[:3])
    print(f"[{time.strftime('%H:%M:%S')} UTC] {len(items)} items vérifiés (dont {len(watch_items)} surveillés). Début : {top}")

    if first_check:
        first_check = False
        if TEST_ON_START and items:
            send_discord_alert(items[0], "new_item")
            print("Alerte test envoyée pour vérifier le webhook.")

    alerts = []
    for item in items:
        if not item.get("id"):
            continue
        reason = evaluate(item)
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
