"""
Roblox Official Notifier (Limiteds + Normal Items) -> Discord webhook.
Lecture seule de l'API publique du catalogue : aucune action sur ton compte.

Optimisations vitesse :
  - plusieurs workers décalés dans le temps (polling effectif = POLL_INTERVAL)
  - envoi du webhook IMMEDIAT, la miniature est ajoutée ensuite par édition
  - session HTTP persistante (keep-alive), orjson/uvloop si installés
  - cache de démarrage large (Limit=120) pour éviter les faux "nouveaux items"
"""

import asyncio
import functools
import itertools
import os
import time
from datetime import datetime, timezone

import aiohttp
from aiohttp import web
from dotenv import load_dotenv

try:
    import orjson

    _loads = orjson.loads
except ImportError:  # fallback
    import json

    _loads = json.loads

try:
    import uvloop

    uvloop.install()
except ImportError:
    pass

load_dotenv()
print = functools.partial(print, flush=True)  # logs immédiats sur Render

# ---------------------------------------------------------------- config ----
WEBHOOK_URL = os.environ["WEBHOOK_URL"].split("?")[0].rstrip("/")
POLL_INTERVAL = float(os.getenv("POLL_INTERVAL", "1.0"))  # intervalle effectif global
WORKERS = max(1, int(os.getenv("WORKERS", "1")))  # requêtes concurrentes décalées
POLL_LIMIT = os.getenv("POLL_LIMIT", "28")  # valeurs acceptées : 10, 28, 30, 60, 120
PING_ROLE_ID = os.getenv("PING_ROLE_ID", "")


def parse_proxy(raw: str) -> str:
    """Accepte http://user:pass@ip:port ou ip:port:user:pass ou ip:port."""
    raw = raw.strip()
    if "://" in raw:
        return raw
    parts = raw.split(":")
    if len(parts) == 4:  # ip:port:user:pass
        ip, port, user, pwd = parts
        return f"http://{user}:{pwd}@{ip}:{port}"
    return f"http://{raw}"  # ip:port


PROXIES = [
    parse_proxy(p) for p in os.getenv("PROXIES", "").replace("\n", ",").split(",") if p.strip()
]
TEST_ON_START = os.getenv("TEST_ON_START", "0") == "1"
CATEGORY = os.getenv("CATEGORY", "11")  # 11 = accessoires
CREATOR_ID = int(os.getenv("CREATOR_ID", "1"))  # 1 = Roblox officiel
MAX_AGE_HOURS = float(os.getenv("MAX_AGE_HOURS", "48"))  # ignore les "nouveaux" trop vieux

HEADERS = {"User-Agent": "Mozilla/5.0 (limited-notifier)"}

SALE_LOCATIONS = {
    "shoponly": "Website only",
    "experiencesdevapionly": "Experiences only",
    "shopandallexperiences": "Website and experiences",
    "shopandmyexperiences": "Website and creator's experiences",
    "shopandexperiencesbyid": "Website and specific experiences",
}


def catalog_url(limit: str | int) -> str:
    return (
        "https://catalog.roblox.com/v1/search/items/details"
        f"?Category={CATEGORY}&CreatorType=User&CreatorTargetId={CREATOR_ID}"
        f"&SortType=3&Limit={limit}"
    )


POLL_URL = catalog_url(POLL_LIMIT)
BOOT_URL = catalog_url(120)

# ----------------------------------------------------------------- state ----
state: dict[int, bool] = {}  # asset id -> est limité
proxy_cycle = itertools.cycle(PROXIES) if PROXIES else None
cooldown_until = 0.0  # pause globale après un 429 (sans proxies)
last_status = 0  # dernier statut HTTP du catalogue (0 = erreur réseau)
_tasks: set[asyncio.Task] = set()


def spawn(coro) -> None:
    task = asyncio.create_task(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


# --------------------------------------------------------------- helpers ----
def normalize(value) -> str:
    return str(value or "").replace(" ", "").replace("_", "").lower()


def check_item_status(item: dict) -> tuple[bool, bool]:
    """(est un item du créateur surveillé, est limité)."""
    if item.get("creatorTargetId") != CREATOR_ID:
        return False, False
    restrictions = item.get("itemRestrictions") or []
    total_qty = item.get("totalQuantity") or 0
    limited = bool(
        "Limited" in restrictions
        or "LimitedUnique" in restrictions
        or "Collectible" in restrictions
        or (total_qty > 0 and item.get("collectibleItemId"))
    )
    return True, limited


def is_stale(item: dict) -> bool:
    """True si l'item est trop ancien pour être annoncé comme nouveau."""
    raw = item.get("itemCreatedUtc") or item.get("createdUtc")
    if not raw:
        return False
    try:
        dt = datetime.fromisoformat(str(raw).rstrip("Z").split(".")[0])
        dt = dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return False
    return (datetime.now(timezone.utc) - dt).total_seconds() > MAX_AGE_HOURS * 3600


def format_price(price) -> str:
    if price is None:
        return "?"
    if price == 0:
        return "Free"
    return f"R${price:,}"


def build_embed(item: dict, is_limited: bool) -> tuple[dict, str | None]:
    asset_id = item["id"]
    price = item.get("price") if item.get("price") is not None else item.get("lowestPrice")
    quantity = item.get("totalQuantity")
    left = item.get("unitsAvailableForConsumption")

    if quantity and quantity > 0:
        qty_txt = f"{left}/{quantity}" if left is not None else str(quantity)
    else:
        qty_txt = "Unlimited"

    description = (item.get("description") or "").strip() or "—"
    if len(description) > 300:
        description = description[:297] + "..."

    location_raw = item.get("saleLocationType") or "Unknown"
    location = SALE_LOCATIONS.get(normalize(location_raw), location_raw)

    if is_limited:
        author, color = "New Limited", 0xE0457B
        content = f"<@&{PING_ROLE_ID}>" if PING_ROLE_ID else "@limited"
    else:
        author, color, content = "New Roblox Item", 0x3498DB, None

    width_pad = "\u2800" * 45  # force un embed large
    embed = {
        "author": {"name": author},
        "title": item.get("name", "Unknown"),
        "url": f"https://www.roblox.com/catalog/{asset_id}",
        "color": color,
        "description": (
            f"**Asset ID:** {asset_id}\n\n"
            f"**Description:**\n{description}\n\n"
            f"**Purchase location:**\n{location}\n\n"
            f"{width_pad}"
        ),
        "fields": [
            {"name": "Price", "value": format_price(price), "inline": True},
            {"name": "Quantity", "value": qty_txt, "inline": True},
        ],
        "footer": {"text": "Detected at:"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    return embed, content


# --------------------------------------------------------------- discord ----
async def post_webhook(session: aiohttp.ClientSession, payload: dict) -> str | None:
    """Envoie le message, retourne son ID (pour l'éditer ensuite)."""
    for _ in range(4):
        try:
            async with session.post(f"{WEBHOOK_URL}?wait=true", json=payload) as r:
                if r.status == 429:
                    data = _loads(await r.read())
                    await asyncio.sleep(float(data.get("retry_after", 1)))
                    continue
                if r.status >= 500:
                    await asyncio.sleep(0.5)
                    continue
                if r.status >= 400:
                    print(f"[webhook] error {r.status}: {await r.text()}")
                    return None
                return _loads(await r.read()).get("id")
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            print(f"[webhook] network error: {e}")
            await asyncio.sleep(0.5)
    return None


async def fetch_thumbnail(session: aiohttp.ClientSession, asset_id: int) -> str | None:
    url = (
        "https://thumbnails.roblox.com/v1/assets"
        f"?assetIds={asset_id}&size=420x420&format=Png"
    )
    for _ in range(6):  # les miniatures d'items neufs mettent parfois quelques secondes
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=4)) as r:
                if r.status == 200:
                    data = _loads(await r.read()).get("data", [])
                    if data and data[0].get("state") == "Completed" and data[0].get("imageUrl"):
                        return data[0]["imageUrl"]
        except (aiohttp.ClientError, asyncio.TimeoutError):
            pass
        await asyncio.sleep(1.5)
    return None


async def edit_message(session: aiohttp.ClientSession, message_id: str, embed: dict) -> None:
    try:
        async with session.patch(
            f"{WEBHOOK_URL}/messages/{message_id}", json={"embeds": [embed]}
        ) as r:
            if r.status >= 400:
                print(f"[webhook] edit error {r.status}: {await r.text()}")
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        print(f"[webhook] edit network error: {e}")


async def notify(session: aiohttp.ClientSession, item: dict, is_limited: bool) -> None:
    embed, content = build_embed(item, is_limited)
    payload = {"embeds": [embed], "allowed_mentions": {"parse": ["roles", "everyone"]}}
    if content:
        payload["content"] = content

    # 1) envoi immédiat, sans attendre la miniature
    message_id = await post_webhook(session, payload)
    if not message_id:
        return

    # 2) miniature ajoutée par édition
    image = await fetch_thumbnail(session, item["id"])
    if image:
        embed["thumbnail"] = {"url": image}
        await edit_message(session, message_id, embed)


# ---------------------------------------------------------------- catalog ---
async def fetch_items(session: aiohttp.ClientSession, url: str) -> list[dict] | None:
    global cooldown_until, last_status
    last_status = 0
    wait = cooldown_until - time.monotonic()
    if wait > 0:
        await asyncio.sleep(wait)

    proxy = next(proxy_cycle) if proxy_cycle else None
    try:
        async with session.get(url, proxy=proxy) as r:
            last_status = r.status
            if r.status == 429:
                retry = float(r.headers.get("retry-after", 3))
                if not proxy_cycle:  # une seule IP : tout le monde se met en pause
                    cooldown_until = time.monotonic() + retry
                print(f"[catalog] rate limit ({retry}s)")
                return None
            if r.status != 200:
                body = (await r.text())[:150].replace("\n", " ")
                print(f"[catalog] status {r.status} | {body}")
                return None
            return _loads(await r.read()).get("data", [])
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        print(f"[catalog] network error: {e!r}")
        return None


async def pick_working_url(session: aiohttp.ClientSession, limits: list[int]):
    """Essaie plusieurs valeurs de Limit, garde la première acceptée par l'API."""
    for lim in limits:
        url = catalog_url(lim)
        while True:
            items = await fetch_items(session, url)
            if items is not None:
                print(f"[init] Limit={lim} accepté")
                return url, items
            if last_status == 400:
                print(f"[init] Limit={lim} refusé (400), essai de la valeur suivante")
                break
            await asyncio.sleep(2)  # 429 / réseau : on réessaie la même valeur
    raise SystemExit("Aucune valeur de Limit acceptée (400) : vérifie CATEGORY / CREATOR_ID / proxies")


def handle_item(session: aiohttp.ClientSession, item: dict) -> None:
    ok, limited = check_item_status(item)
    if not ok:
        return
    item_id = item["id"]
    previous = state.get(item_id)

    if previous is None:
        state[item_id] = limited  # marqué AVANT tout await : pas de doublon entre workers
        if is_stale(item):
            return
        print(f"[new] {item.get('name')} ({item_id}) | limited={limited}")
        spawn(notify(session, item, limited))
    elif limited and not previous:
        state[item_id] = True
        print(f"[went limited] {item.get('name')} ({item_id})")
        spawn(notify(session, item, True))


async def worker(session: aiohttp.ClientSession, index: int) -> None:
    period = POLL_INTERVAL * WORKERS
    await asyncio.sleep(index * POLL_INTERVAL)  # décalage entre workers
    while True:
        started = time.monotonic()
        try:
            items = await fetch_items(session, POLL_URL)
            if items:
                for it in items:
                    handle_item(session, it)
        except Exception as e:  # un worker ne doit jamais mourir
            print(f"[worker {index}] error: {e!r}")
        await asyncio.sleep(max(0.0, period - (time.monotonic() - started)))


# ----------------------------------------------------------------- server ---
async def start_health_server() -> None:
    async def health(_request: web.Request) -> web.Response:
        return web.Response(text="ok")

    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", "10000"))
    await web.TCPSite(runner, "0.0.0.0", port).start()
    print(f"[http] health check server on port {port}")


async def main() -> None:
    await start_health_server()
    connector = aiohttp.TCPConnector(limit=50, ttl_dns_cache=300, keepalive_timeout=60)
    async with aiohttp.ClientSession(
        headers=HEADERS, timeout=aiohttp.ClientTimeout(total=6), connector=connector
    ) as session:
        global POLL_URL
        # Cache de démarrage large : on mémorise sans notifier
        _, items = await pick_working_url(session, [120, 60, 30, 10])
        POLL_URL, _ = await pick_working_url(
            session, list(dict.fromkeys([int(POLL_LIMIT), 30, 10]))
        )

        first_match = None
        for it in items:
            ok, limited = check_item_status(it)
            if ok:
                state[it["id"]] = limited
                first_match = first_match or (it, limited)
        print(f"[init] {len(state)} items cached | workers={WORKERS} | interval={POLL_INTERVAL}s")

        if TEST_ON_START and first_match:
            print("[test] sending startup test alert")
            spawn(notify(session, *first_match))

        await asyncio.gather(*(worker(session, i) for i in range(WORKERS)))


if __name__ == "__main__":
    asyncio.run(main())
