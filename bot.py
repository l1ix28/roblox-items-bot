import os
import json
import time
import asyncio
import itertools
from datetime import datetime, timedelta, timezone
from threading import Thread
from http.server import HTTPServer, BaseHTTPRequestHandler

import aiohttp


# --- Mini serveur HTTP pour satisfaire Render (Web Service) ---
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is alive!")

    def log_message(self, format, *args):
        return


def run_server():
    port = int(os.environ.get("PORT", 8080))
    HTTPServer(("0.0.0.0", port), HealthCheckHandler).serve_forever()


Thread(target=run_server, daemon=True).start()
print("Mini serveur HTTP démarré pour Render.")

WEBHOOK_URL = os.environ.get("WEBHOOK_URL")
if not WEBHOOK_URL:
    print("Critical Error: WEBHOOK_URL environment variable is not defined!")
    raise SystemExit(1)

# ---------------- Réglages ----------------
# Délai minimum entre deux requêtes vers Roblox (secondes).
# Commence à 2. Si tu vois des "429" dans les logs, monte à 3 ou 4.
MIN_REQUEST_GAP = float(os.environ.get("MIN_REQUEST_GAP", 2))
# Proxies optionnels (séparés par des virgules), ex : "http://user:pass@ip:port,http://..."
# Avec N proxies, le délai effectif entre requêtes est divisé par N (donc détection plus rapide).
PROXIES = [p.strip() for p in os.environ.get("PROXIES", "").split(",") if p.strip()]
# ID du rôle Discord à ping pour les limiteds (clic droit sur le rôle > Copier l'identifiant)
LIMITED_ROLE_ID = os.environ.get("LIMITED_ROLE_ID")

WATCHLIST_EVERY = 5            # la watchlist est vérifiée 1 tour sur 5
SEED_PAGES = 4                 # pages mémorisées au démarrage (30 items par page)
NEW_DAYS = 60                  # un item créé il y a moins de X jours compte comme "nouveau"
ALERT_NEW_NON_LIMITED = True   # alerter aussi sur les nouveaux items Roblox non limited
ALERT_BACK_ON_SALE = True      # alerte quand un item hors vente repasse en vente
INCLUDE_OFFSALE = True         # mets False si l'API renvoie une erreur 400
TEST_ON_START = os.environ.get("TEST_ON_START") == "1"
RETRIES = 3

WATCHLIST = [
    ("Bundle", 319226),  # Korblox Deathwalker
]

API_URL = "https://catalog.roblox.com/v1/search/items/details"
DETAILS_URL = "https://catalog.roblox.com/v1/catalog/items/details"
BASE_PARAMS = {
    "Category": 1,
    "CreatorTargetId": 1,
    "CreatorType": "User",
    "SortType": 3,
    "Limit": 30,
}
if INCLUDE_OFFSALE:
    BASE_PARAMS["IncludeNotForSale"] = "true"

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; RobloxCatalogNotifier/2.0)"}
LIMITED_TAGS = {"Collectible", "Limited", "LimitedUnique"}

tracked_items = {}
csrf_token = None
limiter = None
background_tasks = set()
proxy_cycle = itertools.cycle(PROXIES) if PROXIES else None


# ---------------- Limiteur de requêtes ----------------
class Limiter:
    """Espace les requêtes d'au moins `gap` secondes, et gère les pauses après un 429."""

    def __init__(self, gap):
        self.gap = gap
        self.next_time = 0.0
        self.cooldown_until = 0.0
        self.lock = asyncio.Lock()

    async def wait(self):
        async with self.lock:
            now = time.monotonic()
            start = max(now, self.next_time, self.cooldown_until)
            self.next_time = start + self.gap
        delay = start - now
        if delay > 0:
            await asyncio.sleep(delay)

    def penalize(self, seconds):
        self.cooldown_until = max(self.cooldown_until, time.monotonic() + seconds)


def next_proxy():
    return next(proxy_cycle) if proxy_cycle else None


# ---------------- Outils ----------------
def item_key(item):
    return f"{item.get('itemType', 'Asset')}:{item.get('id')}"


def normalize(value):
    return str(value or "").replace(" ", "").replace("_", "").lower()


def is_limited(item):
    return bool(LIMITED_TAGS.intersection(item.get("itemRestrictions") or []))


def is_on_sale(item):
    status = normalize(item.get("priceStatus"))
    if status == "offsale":
        return False
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


# ---------------- Requêtes Roblox ----------------
async def fetch_page(session, cursor=None):
    """Retourne (items, curseur suivant) ou (None, None) en cas d'échec."""
    params = dict(BASE_PARAMS)
    if cursor:
        params["Cursor"] = cursor

    for attempt in range(1, RETRIES + 1):
        await limiter.wait()
        try:
            async with session.get(
                API_URL, params=params, headers=HEADERS, proxy=next_proxy(),
                timeout=aiohttp.ClientTimeout(total=8),
            ) as r:
                if r.status == 429:
                    try:
                        wait = int(r.headers.get("Retry-After", 5))
                    except ValueError:
                        wait = 5
                    print(f"429 Roblox, pause de {wait}s ({attempt}/{RETRIES})")
                    limiter.penalize(wait)
                    continue
                if r.status != 200:
                    print(f"Erreur API Roblox {r.status} : {(await r.text())[:200]}")
                    return None, None
                body = await r.json()
        except Exception as e:
            print(f"Erreur réseau : {e}")
            continue

        items = [
            i for i in body.get("data", [])
            if i.get("creatorTargetId") == 1 and i.get("creatorType", "User") == "User"
        ]
        return items, body.get("nextPageCursor")

    return None, None


async def fetch_watchlist(session):
    global csrf_token
    if not WATCHLIST:
        return []

    payload = {"items": [{"itemType": t, "id": i} for t, i in WATCHLIST]}

    for attempt in range(1, RETRIES + 1):
        headers = dict(HEADERS)
        if csrf_token:
            headers["x-csrf-token"] = csrf_token

        await limiter.wait()
        try:
            async with session.post(
                DETAILS_URL, json=payload, headers=headers, proxy=next_proxy(),
                timeout=aiohttp.ClientTimeout(total=8),
            ) as r:
                if r.status == 403 and r.headers.get("x-csrf-token"):
                    csrf_token = r.headers["x-csrf-token"]
                    continue
                if r.status == 429:
                    try:
                        wait = int(r.headers.get("Retry-After", 5))
                    except ValueError:
                        wait = 5
                    print(f"429 Roblox (watchlist), pause de {wait}s")
                    limiter.penalize(wait)
                    continue
                if r.status != 200:
                    print(f"Erreur watchlist {r.status} : {(await r.text())[:200]}")
                    return []
                return (await r.json()).get("data", [])
        except Exception as e:
            print(f"Erreur réseau (watchlist) : {e}")
            continue

    return []


# ---------------- Détection ----------------
def evaluate(item):
    key = item_key(item)
    limited = is_limited(item)
    on_sale = is_on_sale(item)
    previous = tracked_items.get(key)
    reason = None

    if previous is None:
        if limited:
            reason = "new_limited" if is_recent(item) else "now_limited"
        elif ALERT_NEW_NON_LIMITED and is_recent(item):
            reason = "new_item"
    else:
        if ALERT_BACK_ON_SALE and not previous["on_sale"] and on_sale:
            reason = "back_on_sale"
        elif not previous["limited"] and limited:
            reason = "now_limited"

    tracked_items[key] = {"limited": limited, "on_sale": on_sale}
    return reason


async def seed_tracked_items(session):
    cursor = None
    count = 0
    pages = 0
    for _ in range(SEED_PAGES):
        items, next_cursor = await fetch_page(session, cursor)
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

    print(f"Démarrage : {count} items mémorisés ({pages}/{SEED_PAGES} pages), sans alerte.")

    for item in await fetch_watchlist(session):
        evaluate(item)


# ---------------- Discord ----------------
SALE_LOCATIONS = {
    "shoponly": "Website only",
    "experiencesdevapionly": "Experiences only",
    "shopandallexperiences": "Website and experiences",
    "shopandmyexperiences": "Website and creator's experiences",
}


async def get_thumbnail(session, item):
    if item.get("itemType") == "Bundle":
        url = "https://thumbnails.roblox.com/v1/bundles/thumbnails"
        params = {"bundleIds": item["id"], "size": "420x420", "format": "Png"}
    else:
        url = "https://thumbnails.roblox.com/v1/assets"
        params = {"assetIds": item["id"], "size": "420x420", "format": "Png"}
    try:
        async with session.get(
            url, params=params, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=2.5)
        ) as r:
            if r.status == 200:
                data = (await r.json()).get("data", [])
                if data and data[0].get("imageUrl"):
                    return data[0]["imageUrl"]
    except Exception:
        pass
    return None


async def send_discord_alert(session, item, reason):
    item_id = item["id"]
    name = item.get("name", "Unknown item")
    is_bundle = item.get("itemType") == "Bundle"
    item_url = f"https://www.roblox.com/{'bundles' if is_bundle else 'catalog'}/{item_id}"

    titles = {
        "new_limited": ("New Limited", 0xE0457B),
        "now_limited": ("Went Limited", 0xE0457B),
        "back_on_sale": ("Back On Sale", 0x2ECC71),
        "new_item": ("New Item", 0x3498DB),
    }
    author, color = titles.get(reason, ("Update", 0x3498DB))

    price = item.get("price")
    price_text = f"R${price:,}" if isinstance(price, int) and price > 0 else (item.get("priceStatus") or "Free")

    total = item.get("totalQuantity") or 0
    available = item.get("unitsAvailableForConsumption") or 0
    quantity_text = f"{available}/{total}" if total > 0 else "Unlimited"

    description = (item.get("description") or "No description").strip()
    if len(description) > 300:
        description = description[:297] + "..."

    location = SALE_LOCATIONS.get(
        normalize(item.get("saleLocationType")), item.get("saleLocationType") or "Unknown"
    )

    thumb = await get_thumbnail(session, item)

    embed = {
        "author": {"name": author},
        "title": name,
        "url": item_url,
        "color": color,
        "description": (
            f"**Asset ID:** {item_id}\n\n"
            f"**Description:**\n{description}\n\n"
            f"**Purchase location:**\n{location}"
        ),
        "fields": [
            {"name": "Price", "value": price_text, "inline": True},
            {"name": "Quantity", "value": quantity_text, "inline": True},
        ],
        "footer": {"text": "Detected at:"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if thumb:
        embed["thumbnail"] = {"url": thumb}

    body = {"embeds": [embed]}
    if LIMITED_ROLE_ID and reason in ("new_limited", "now_limited"):
        body["content"] = f"<@&{LIMITED_ROLE_ID}>"
        body["allowed_mentions"] = {"roles": [LIMITED_ROLE_ID]}

    for attempt in range(3):
        try:
            async with session.post(WEBHOOK_URL, json=body, timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status in (200, 204):
                    print(f"Alerte envoyée ({reason}) : {name}")
                    return
                if r.status == 429:
                    data = await r.json()
                    await asyncio.sleep(float(data.get("retry_after", 1)))
                    continue
                print(f"Erreur Discord ({r.status}) : {(await r.text())[:200]}")
                return
        except Exception as e:
            print(f"Erreur webhook : {e}")
            await asyncio.sleep(1)


def fire_alert(session, item, reason):
    """Envoie l'alerte en tâche de fond pour ne pas ralentir la détection."""
    task = asyncio.create_task(send_discord_alert(session, item, reason))
    background_tasks.add(task)
    task.add_done_callback(background_tasks.discard)


# ---------------- Boucle principale ----------------
async def main():
    global limiter
    limiter = Limiter(MIN_REQUEST_GAP / max(1, len(PROXIES)))
    print(f"Bot démarré (délai {limiter.gap:.2f}s entre requêtes, {len(PROXIES)} proxy(s)).")

    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(limit=30)) as session:
        await seed_tracked_items(session)
        first_check = True
        loop_count = 0

        while True:
            tasks = [fetch_page(session)]
            check_watch = bool(WATCHLIST) and loop_count % WATCHLIST_EVERY == 0
            if check_watch:
                tasks.append(fetch_watchlist(session))
            loop_count += 1

            results = await asyncio.gather(*tasks)
            items = results[0][0]
            watch_items = results[1] if check_watch else []

            if items is None and not watch_items:
                await asyncio.sleep(1)
                continue

            items = (items or []) + watch_items

            if first_check:
                first_check = False
                if TEST_ON_START and items:
                    fire_alert(session, items[0], "new_item")
                    print("Alerte test envoyée.")

            alerts = []
            for item in items:
                if item.get("id"):
                    reason = evaluate(item)
                    if reason:
                        alerts.append((item, reason))

            for item, reason in reversed(alerts):
                fire_alert(session, item, reason)

            if len(tracked_items) > 2000:
                for key in list(tracked_items)[:500]:
                    del tracked_items[key]


if __name__ == "__main__":
    asyncio.run(main())
