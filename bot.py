import os
import json
import time
import asyncio
import itertools
import functools
from datetime import datetime, timedelta, timezone
from threading import Thread
from http.server import HTTPServer, BaseHTTPRequestHandler

import aiohttp

# Affiche les logs tout de suite (sinon Render les regroupe et ils ont tous la même heure)
print = functools.partial(print, flush=True)


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
# La route qui liste les derniers items autorise 12 requêtes par minute et par IP
# (en-tête x-ratelimit-limit: 12;w=60), donc 1 requête toutes les 5 s par IP.
# Ce délai est divisé par le nombre de proxies actifs : 10 proxies = 1 requête toutes les ~0,55 s.
# Monte cette valeur si tu veux consommer moins de bande passante (ex : 20 = 1 requête / 2 s avec 10 proxies).
MIN_REQUEST_GAP = float(os.environ.get("MIN_REQUEST_GAP", 5.5))
# Proxies (séparés par des virgules), ex : "http://user:pass@ip:port,http://user:pass@ip:port"
# Tu peux mettre "direct" dans la liste : ça veut dire "sans proxy" (IP de Render, gratuit en bande passante).
# Exemple sans aucun proxy payant : PROXIES=direct
PROXIES = [p.strip() for p in os.environ.get("PROXIES", "").split(",") if p.strip()]
# ID du rôle Discord à ping pour les limiteds (clic droit sur le rôle > Copier l'identifiant)
LIMITED_ROLE_ID = os.environ.get("LIMITED_ROLE_ID")

REFRESH_EVERY_SECONDS = 8      # on revérifie les détails des tout derniers items (pour voir un item passer limited)
REFRESH_TOP = 5                # nombre d'items du haut de la liste revérifiés
NEW_DAYS = 60                  # un item créé il y a moins de X jours compte comme "nouveau"
ALERT_NEW_NON_LIMITED = True   # alerter aussi sur les nouveaux items Roblox non limited
ALERT_BACK_ON_SALE = True      # alerte quand un item hors vente repasse en vente
INCLUDE_OFFSALE = True         # inclut les items hors vente (désactivé tout seul si Roblox répond 400)
TEST_ON_START = os.environ.get("TEST_ON_START") == "1"
RETRIES = 3
DETAILS_ATTEMPTS = 6

# Items surveillés en plus, même s'ils ne sont pas modifiés récemment.
# ("Bundle", id) pour roblox.com/bundles/ID/...    ("Asset", id) pour roblox.com/catalog/ID/...
WATCHLIST = [
    ("Bundle", 319226),  # Korblox Deathwalker
]

LIST_URL = "https://catalog.roblox.com/v1/search/items"                # liste d'IDs (rapide)
DETAILS_URL = "https://catalog.roblox.com/v1/catalog/items/details"    # détails (POST)
LIST_PARAMS = {
    "Category": 1,
    "CreatorTargetId": 1,
    "CreatorType": "User",
    "SortType": 3,        # 3 = Updated (les plus récemment modifiés d'abord)
    "Limit": 30,
}
if INCLUDE_OFFSALE:
    LIST_PARAMS["IncludeNotForSale"] = "true"

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; RobloxCatalogNotifier/3.0)"}
LIMITED_TAGS = {"Collectible", "Limited", "LimitedUnique"}

tracked_items = {}      # "Type:id" -> {"limited": bool, "on_sale": bool}
known_order = None      # derniers IDs vus dans la liste, du plus récent au plus ancien
csrf_token = None
limiter = None
background_tasks = set()
proxy_cycles = {}
proxy_cooldown = {}     # (usage, proxy) -> instant jusqu'auquel on ne l'utilise plus
poll_count = 0


# ---------------- Limiteur de requêtes ----------------
class Limiter:
    """Espace les requêtes de la liste, et gère les pauses quand on n'utilise pas de proxy."""

    def __init__(self, gap_func):
        self.gap_func = gap_func
        self.next_time = 0.0
        self.cooldown_until = 0.0
        self.lock = asyncio.Lock()

    async def wait(self):
        async with self.lock:
            now = time.monotonic()
            start = max(now, self.next_time, self.cooldown_until)
            self.next_time = start + self.gap_func()
        delay = start - now
        if delay > 0:
            await asyncio.sleep(delay)

    def penalize(self, seconds):
        self.cooldown_until = max(self.cooldown_until, time.monotonic() + seconds)


def real_proxy(entry):
    """"direct" dans PROXIES = pas de proxy (on utilise l'IP de Render, sans consommer de bande passante Webshare)."""
    return None if entry == "direct" else entry


def next_proxy(kind="list"):
    """Prochain proxy pour cet usage ("list", "details" ou "thumb") qui n'est pas en pause."""
    if not PROXIES:
        return None
    if kind not in proxy_cycles:
        proxy_cycles[kind] = itertools.cycle(PROXIES)
    now = time.monotonic()
    for _ in range(len(PROXIES)):
        p = next(proxy_cycles[kind])
        if proxy_cooldown.get((kind, p), 0) <= now:
            return p
    return min(PROXIES, key=lambda p: proxy_cooldown.get((kind, p), 0))


def current_gap():
    """Délai entre deux requêtes de liste : divisé par le nombre de proxies actifs."""
    if not PROXIES:
        return MIN_REQUEST_GAP
    now = time.monotonic()
    active = sum(1 for p in PROXIES if proxy_cooldown.get(("list", p), 0) <= now)
    return MIN_REQUEST_GAP / max(1, active)


def retry_after(response):
    try:
        return int(response.headers.get("Retry-After", 5))
    except ValueError:
        return 5


def handle_429(proxy, wait, kind, label=""):
    """429 : on met ce proxy en pause pour cet usage (les autres continuent)."""
    if proxy:
        pause = max(wait, 61)
        proxy_cooldown[(kind, proxy)] = time.monotonic() + pause
        print(f"429 Roblox{label} via {proxy.split('@')[-1]} : ce proxy est mis en pause {pause}s")
    else:
        limiter.penalize(wait)
        print(f"429 Roblox{label} (sans proxy) : pause de {wait}s")


def spawn(coro):
    task = asyncio.create_task(coro)
    background_tasks.add(task)
    task.add_done_callback(background_tasks.discard)


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
async def fetch_list(session):
    """Liste ordonnée [(type, id), ...] des derniers items modifiés, ou None en cas d'échec."""
    for attempt in range(1, RETRIES + 1):
        await limiter.wait()
        proxy = next_proxy("list")
        try:
            async with session.get(
                LIST_URL, params=LIST_PARAMS, headers=HEADERS, proxy=real_proxy(proxy),
                timeout=aiohttp.ClientTimeout(total=8),
            ) as r:
                if r.status == 429:
                    handle_429(proxy, retry_after(r), "list")
                    continue
                if r.status == 400 and "IncludeNotForSale" in LIST_PARAMS:
                    print("Erreur 400 : on retire IncludeNotForSale de la recherche et on réessaie.")
                    LIST_PARAMS.pop("IncludeNotForSale", None)
                    continue
                if r.status != 200:
                    print(f"Erreur API Roblox (liste) {r.status} : {(await r.text())[:200]}")
                    return None
                body = await r.json()
        except Exception as e:
            print(f"Erreur réseau (liste) : {e!r}")
            continue

        return [(i.get("itemType", "Asset"), i["id"]) for i in body.get("data", []) if i.get("id")]

    return None


async def fetch_details(session, pairs):
    """Détails de plusieurs items [(type, id), ...] (requête POST, avec jeton CSRF)."""
    global csrf_token
    if not pairs:
        return []

    payload = {"items": [{"itemType": t, "id": i} for t, i in pairs]}

    for attempt in range(1, DETAILS_ATTEMPTS + 1):
        headers = dict(HEADERS)
        if csrf_token:
            headers["x-csrf-token"] = csrf_token

        proxy = next_proxy("details")
        try:
            async with session.post(
                DETAILS_URL, json=payload, headers=headers, proxy=real_proxy(proxy),
                timeout=aiohttp.ClientTimeout(total=8),
            ) as r:
                if r.status == 403 and r.headers.get("x-csrf-token"):
                    csrf_token = r.headers["x-csrf-token"]
                    continue
                if r.status == 429:
                    handle_429(proxy, retry_after(r), "details", " (détails)")
                    await asyncio.sleep(0.3)
                    continue
                if r.status != 200:
                    print(f"Erreur détails {r.status} : {(await r.text())[:200]}")
                    await asyncio.sleep(0.3)
                    continue
                return (await r.json()).get("data", [])
        except Exception as e:
            print(f"Erreur réseau (détails) : {e!r}")
            await asyncio.sleep(0.3)
            continue

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


def find_changes(pairs):
    """Items nouveaux dans la liste, ou remontés vers le haut (= modifiés) depuis le dernier passage."""
    global known_order
    new_order = [i for _, i in pairs]
    if known_order is None:
        known_order = new_order
        return []
    old_pos = {i: p for p, i in enumerate(known_order)}
    changed = [(t, i) for p, (t, i) in enumerate(pairs) if i not in old_pos or p < old_pos[i]]
    known_order = new_order
    return changed


async def seed(session):
    """Mémorise l'état actuel sans envoyer d'alerte."""
    global known_order
    pairs = None
    for attempt in range(1, 6):
        pairs = await fetch_list(session)
        if pairs:
            break
        print(f"Mémorisation : liste indisponible, nouvel essai ({attempt}/5)")
        await asyncio.sleep(3)

    if not pairs:
        print("Mémorisation impossible, la première liste reçue servira de référence.")
        return

    known_order = [i for _, i in pairs]
    details = await fetch_details(session, pairs[:REFRESH_TOP * 2] + WATCHLIST)
    print("Exemples d'items reçus :")
    for item in details[:5]:
        debug_item(item)
    for item in details:
        evaluate(item)
    print(f"Démarrage : {len(pairs)} items dans la liste, {len(details)} mémorisés, sans alerte.")


async def process_changes(session, pairs):
    """Récupère les détails, évalue, et envoie les alertes."""
    details = []
    for _ in range(3):
        details = await fetch_details(session, pairs)
        if details:
            break
        await asyncio.sleep(1)

    if not details:
        print(f"Détails indisponibles pour {len(pairs)} item(s).")
        return

    alerts = []
    for item in details:
        if not item.get("id"):
            continue
        reason = evaluate(item)
        if reason:
            alerts.append((item, reason))

    for item, reason in reversed(alerts):
        fire_alert(session, item, reason)

    if len(tracked_items) > 2000:
        for key in list(tracked_items)[:500]:
            del tracked_items[key]


async def send_test_alert(session, pairs):
    details = await fetch_details(session, pairs[:1])
    if details:
        fire_alert(session, details[0], "new_item")
        print("Alerte test envoyée.")


# ---------------- Discord ----------------
SALE_LOCATIONS = {
    "shoponly": "Website only",
    "experiencesdevapionly": "Experiences only",
    "shopandallexperiences": "Website and experiences",
    "shopandmyexperiences": "Website and creator's experiences",
}


async def get_thumbnail(session, item, timeout=2.0):
    if item.get("itemType") == "Bundle":
        url = "https://thumbnails.roblox.com/v1/bundles/thumbnails"
        params = {"bundleIds": item["id"], "size": "420x420", "format": "Png"}
    else:
        url = "https://thumbnails.roblox.com/v1/assets"
        params = {"assetIds": item["id"], "size": "420x420", "format": "Png"}
    try:
        async with session.get(
            url, params=params, headers=HEADERS, proxy=real_proxy(next_proxy("thumb")),
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as r:
            if r.status == 200:
                data = (await r.json()).get("data", [])
                if data and data[0].get("imageUrl"):
                    return data[0]["imageUrl"]
                state = data[0].get("state") if data else "aucune donnée"
                print(f"Miniature pas prête pour {item['id']} (état : {state})")
            else:
                print(f"Miniature : erreur HTTP {r.status} pour {item['id']}")
    except Exception as e:
        print(f"Miniature : erreur pour {item['id']} : {e!r}")
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

    message_id = None
    for attempt in range(3):
        try:
            async with session.post(
                f"{WEBHOOK_URL}?wait=true", json=body, timeout=aiohttp.ClientTimeout(total=10)
            ) as r:
                if r.status in (200, 204):
                    print(f"Alerte envoyée ({reason}) : {name}")
                    try:
                        message_id = (await r.json()).get("id")
                    except Exception:
                        message_id = None
                    break
                if r.status == 429:
                    data = await r.json()
                    await asyncio.sleep(float(data.get("retry_after", 1)))
                    continue
                print(f"Erreur Discord ({r.status}) : {(await r.text())[:200]}")
                return
        except Exception as e:
            print(f"Erreur webhook : {e}")
            await asyncio.sleep(1)
    else:
        return

    # Si l'image n'était pas prête à l'envoi, on réessaie quelques secondes
    # puis on modifie le message pour l'ajouter.
    if thumb or not message_id:
        return
    for delay in (2, 3, 5, 8):
        await asyncio.sleep(delay)
        thumb = await get_thumbnail(session, item, timeout=5)
        if thumb:
            embed["thumbnail"] = {"url": thumb}
            try:
                async with session.patch(
                    f"{WEBHOOK_URL}/messages/{message_id}",
                    json={"embeds": [embed]},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as r:
                    if r.status != 200:
                        print(f"Erreur ajout image ({r.status}) : {(await r.text())[:200]}")
            except Exception as e:
                print(f"Erreur ajout image : {e}")
            return

    # Dernier recours : lien direct d'image Roblox (redirige vers l'image quand elle existe)
    if is_bundle:
        fallback = f"https://www.roblox.com/bundle-thumbnail/image?bundleId={item_id}&width=420&height=420&format=png"
    else:
        fallback = f"https://www.roblox.com/asset-thumbnail/image?assetId={item_id}&width=420&height=420&format=png"
    embed["thumbnail"] = {"url": fallback}
    try:
        async with session.patch(
            f"{WEBHOOK_URL}/messages/{message_id}",
            json={"embeds": [embed]},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as r:
            print(f"Image de secours ajoutée pour {name} (HTTP {r.status})")
    except Exception as e:
        print(f"Erreur image de secours : {e}")


def fire_alert(session, item, reason):
    """Envoie l'alerte en tâche de fond pour ne pas ralentir la détection."""
    spawn(send_discord_alert(session, item, reason))


# ---------------- Boucle principale ----------------
async def main():
    global limiter, poll_count
    limiter = Limiter(current_gap)
    print(
        f"Bot démarré (1 requête de liste toutes les {current_gap():.2f}s, "
        f"{len(PROXIES)} proxy(s))."
    )

    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(limit=40)) as session:
        await seed(session)
        first_check = True
        next_refresh = time.monotonic() + REFRESH_EVERY_SECONDS
        next_heartbeat = time.monotonic() + 60

        while True:
            pairs = await fetch_list(session)
            if pairs is None:
                await asyncio.sleep(1)
                continue
            poll_count += 1

            if first_check:
                first_check = False
                if TEST_ON_START and pairs:
                    spawn(send_test_alert(session, pairs))

            changed = find_changes(pairs)
            if changed:
                print(f"{len(changed)} item(s) nouveau(x) ou modifié(s) : {[i for _, i in changed]}")
                spawn(process_changes(session, changed))

            now = time.monotonic()
            if now >= next_refresh:
                next_refresh = now + REFRESH_EVERY_SECONDS
                spawn(process_changes(session, pairs[:REFRESH_TOP] + WATCHLIST))

            if now >= next_heartbeat:
                next_heartbeat = now + 60
                print(f"[{time.strftime('%H:%M:%S')} UTC] actif : {poll_count} listes reçues, {len(tracked_items)} items suivis")
                poll_count = 0


if __name__ == "__main__":
    asyncio.run(main())
