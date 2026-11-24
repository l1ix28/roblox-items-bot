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
import random
import time
from datetime import datetime, timezone

from urllib.parse import quote

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
POLL_LIMIT = os.getenv("POLL_LIMIT", "30")  # valeurs acceptées par l'API : 10, 28, 30
BOOT_PAGES = max(1, int(os.getenv("BOOT_PAGES", "3")))  # pages chargées au démarrage
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
CATEGORY = os.getenv("CATEGORY", "1")  # 1 = toutes catégories (nécessaire pour le tri par date)
CREATOR_ID = int(os.getenv("CREATOR_ID", "1"))  # 1 = Roblox officiel
HEARTBEAT = float(os.getenv("HEARTBEAT_SECONDS", "30"))  # 0 = désactivé
LOG_TOP = int(os.getenv("LOG_TOP", "5"))  # nb d'items affichés dans les logs
PROXY_MIN_GAP = float(os.getenv("PROXY_MIN_GAP", "5"))  # secondes min entre 2 requêtes sur un même proxy
MAX_AGE_HOURS = float(os.getenv("MAX_AGE_HOURS", "1440"))  # ignore les "nouveaux" trop vieux

HEADERS = {"User-Agent": "Mozilla/5.0 (limited-notifier)"}

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2.1 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:123.0) Gecko/20100101 Firefox/123.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
]


def random_headers() -> dict:
    return {"User-Agent": random.choice(USER_AGENTS)}


SALE_LOCATIONS = {
    "shoponly": "Website only",
    "experiencesdevapionly": "Experiences only",
    "shopandallexperiences": "Website and experiences",
    "shopandmyexperiences": "Website and creator's experiences",
    "shopandexperiencesbyid": "Website and specific experiences",
}

LIMITS_CYCLE = itertools.cycle([10, 28, 30])


def catalog_url(limit: str | int | None = None) -> str:
    lim = limit if limit is not None else next(LIMITS_CYCLE)
    cat = f"Category={CATEGORY}&" if CATEGORY else ""
    return (
        "https://catalog.roblox.com/v1/search/items/details"
        f"?{cat}CreatorType=1&CreatorTargetId={CREATOR_ID}&SortType=3&Limit={lim}&IncludeNotForSale=true"
    )


POLL_URL = catalog_url(POLL_LIMIT)

# ----------------------------------------------------------------- state ----
state: dict[int, bool] = {}  # asset id -> est limité


class ProxyPool:
    """Répartit les requêtes sur les proxies et met de côté ceux qui prennent un 429."""

    def __init__(self, proxies: list[str]) -> None:
        self.gap = PROXY_MIN_GAP if proxies else 0.0
        urls = proxies or [None]
        self.slots = [
            {"url": u, "name": f"proxy#{i + 1}" if u else "direct", "at": 0.0, "fails": 0}
            for i, u in enumerate(urls)
        ]
        self.last_log = 0.0
        self.limited_count = 0
        self.ok_count = 0
        self.err_count = 0
        self.streak = 0  # échecs d'affilée
        self.last_ok = 0.0

    async def acquire(self) -> dict:
        slot = min(self.slots, key=lambda x: x["at"])  # le proxy dispo le plus tôt
        now = time.monotonic()
        start = max(now, slot["at"])
        slot["at"] = start + self.gap  # réservé : un autre worker prendra un autre proxy
        if start > now:
            await asyncio.sleep(start - now)
        return slot

    def limited(self, slot: dict, retry_after: float) -> float:
        slot["fails"] += 1
        self.streak += 1
        delay = min(60.0, (retry_after + 1) * (1.5 ** (slot["fails"] - 1)))
        slot["at"] = max(slot["at"], time.monotonic() + delay)
        self.limited_count += 1
        now = time.monotonic()
        if now - self.last_log > 5:  # un seul log toutes les 5 s
            self.last_log = now
            paused = sum(1 for x in self.slots if x["at"] > now)
            print(
                f"[catalog] 429 ({self.limited_count} depuis le début) | "
                f"{paused}/{len(self.slots)} proxies en pause, dernier: {slot['name']} {delay:.0f}s"
            )
        return delay

    def failed(self, slot: dict) -> None:
        self.err_count += 1
        self.streak += 1
        slot["at"] = max(slot["at"], time.monotonic() + 3)

    def ok(self, slot: dict) -> None:
        slot["fails"] = 0
        self.ok_count += 1
        if self.streak >= 3:
            print(f"[recovered] requêtes OK de nouveau après {self.streak} échecs d'affilée")
        self.streak = 0
        self.last_ok = time.monotonic()


pool = ProxyPool(PROXIES)
latest: dict = {"items": []}  # dernière réponse valide du catalogue (pour les logs)


def fmt_item(it: dict) -> str:
    _, limited = check_item_status(it)
    price = it.get("price") if it.get("price") is not None else it.get("lowestPrice")
    tag = "LIMITED" if limited else "normal"
    return f"  - {it.get('name', '?')} ({it.get('id')}) | {tag} | {format_price(price)}"


last_status = 0  # dernier statut HTTP du catalogue (0 = erreur réseau)
last_cursor = None  # curseur de la page suivante
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
    limited = "Limited" in restrictions or "LimitedUnique" in restrictions
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

    if is_limited:
        author, color = "New Limited", 0xE0457B
        content = f"<@&{PING_ROLE_ID}>" if PING_ROLE_ID else "@limited"
    else:
        author, color, content = "New Roblox Item", 0x3498DB, None

    embed = {
        "author": {"name": author},
        "title": item.get("name", "Unknown"),
        "url": f"https://www.roblox.com/catalog/{asset_id}",
        "color": color,
        "description": f"**Asset ID:** {asset_id}",
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
            async with session.get(
                url, headers=random_headers(), timeout=aiohttp.ClientTimeout(total=4)
            ) as r:
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
    global last_status, last_cursor
    last_status = 0
    last_cursor = None
    slot = await pool.acquire()
    try:
        async with session.get(url, headers=random_headers(), proxy=slot["url"]) as r:
            last_status = r.status
            if r.status == 429:
                pool.limited(slot, float(r.headers.get("retry-after", 5)))
                return None
            if r.status != 200:
                body = (await r.text())[:150].replace("\n", " ")
                print(f"[catalog] status {r.status} on {slot['name']} | {body}")
                pool.failed(slot)
                return None
            pool.ok(slot)
            body = _loads(await r.read())
            last_cursor = body.get("nextPageCursor")
            return body.get("data", [])
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        print(f"[catalog] network error on {slot['name']}: {e!r}")
        pool.failed(slot)
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

    if item_id not in state:
        state[item_id] = limited  # marqué AVANT tout await : pas de doublon entre workers
        if is_stale(item):
            return
        print(f"[new] {item.get('name')} ({item_id}) | limited={limited}")
        spawn(notify(session, item, limited))


async def worker(session: aiohttp.ClientSession, index: int) -> None:
    period = POLL_INTERVAL * WORKERS
    await asyncio.sleep(index * POLL_INTERVAL)  # décalage entre workers
    while True:
        started = time.monotonic()
        try:
            items = await fetch_items(session, catalog_url())
            if items:
                latest["items"] = items
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


async def heartbeat() -> None:
    """Log périodique : prouve que le bot tourne et montre les derniers items vus."""
    prev_ok = prev_rl = prev_err = 0
    while True:
        await asyncio.sleep(HEARTBEAT)
        now = time.monotonic()
        ok, rl, err = pool.ok_count, pool.limited_count, pool.err_count
        active = sum(1 for x in pool.slots if x["at"] <= now)
        since = f"{now - pool.last_ok:.0f}s" if pool.last_ok else "jamais"
        print(
            f"[status] sur {HEARTBEAT:.0f}s : {ok - prev_ok} OK, {rl - prev_rl} x 429, "
            f"{err - prev_err} erreurs | dernier succès il y a {since} | "
            f"proxies dispo {active}/{len(pool.slots)} | {len(state)} items suivis"
        )
        prev_ok, prev_rl, prev_err = ok, rl, err
        for it in latest["items"][:LOG_TOP]:
            print(fmt_item(it))


async def main() -> None:
    await start_health_server()
    connector = aiohttp.TCPConnector(limit=50, ttl_dns_cache=300, keepalive_timeout=60)
    async with aiohttp.ClientSession(
        headers=HEADERS, timeout=aiohttp.ClientTimeout(total=6), connector=connector
    ) as session:
        global POLL_URL
        # Cache de démarrage sur plusieurs pages : on mémorise sans notifier
        boot_url, first_page = await pick_working_url(session, [30, 28, 10])
        items = list(first_page)
        cursor = last_cursor
        for _ in range(BOOT_PAGES - 1):
            if not cursor:
                break
            page = await fetch_items(session, boot_url + "&Cursor=" + quote(cursor, safe=""))
            if not page:
                break
            items += page
            cursor = last_cursor
        POLL_URL, _ = await pick_working_url(
            session, list(dict.fromkeys([int(POLL_LIMIT), 30, 10]))
        )

        first_match = None
        for it in items:
            ok, limited = check_item_status(it)
            if ok:
                state[it["id"]] = limited
                first_match = first_match or (it, limited)
        print(
            f"[init] {len(state)} items cached | workers={WORKERS} | interval={POLL_INTERVAL}s"
            f" | proxies={len(PROXIES)} (gap {PROXY_MIN_GAP}s)"
        )

        latest["items"] = items
        print(f"[init] {min(LOG_TOP, len(items))} derniers items vus :")
        for it in items[:LOG_TOP]:
            print(fmt_item(it))

        if HEARTBEAT > 0:
            spawn(heartbeat())

        if TEST_ON_START and first_match:
            print("[test] sending startup test alert")
            spawn(notify(session, *first_match))

        await asyncio.gather(*(worker(session, i) for i in range(WORKERS)))


if __name__ == "__main__":
    asyncio.run(main())
