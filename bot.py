"""
Roblox Official Asset ID Scanner & Notifier -> Discord webhook.
Contourne le délai d'indexation de la recherche publique en scannant les ID bruts.
Cible strictement les items officiels Roblox (CREATOR_ID = 1).
"""

import asyncio
import functools
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
except ImportError:
    import json
    _loads = json.loads

try:
    import uvloop
    uvloop.install()
except ImportError:
    pass

load_dotenv()
print = functools.partial(print, flush=True)

# ---------------------------------------------------------------- config ----
WEBHOOK_URL = os.environ["WEBHOOK_URL"].split("?")[0].rstrip("/")
POLL_INTERVAL = float(os.getenv("POLL_INTERVAL", "0.4"))
WORKERS = max(1, int(os.getenv("WORKERS", "3")))
HEARTBEAT = float(os.getenv("HEARTBEAT_SECONDS", "30"))
LOG_TOP = int(os.getenv("LOG_TOP", "5"))
PROXY_MIN_GAP = float(os.getenv("PROXY_MIN_GAP", "5"))
CREATOR_ID = int(os.getenv("CREATOR_ID", "1"))
TEST_ON_START = os.getenv("TEST_ON_START", "0") == "1"

def parse_proxy(raw: str) -> str:
    raw = raw.strip()
    if "://" in raw:
        return raw
    parts = raw.split(":")
    if len(parts) == 4:
        ip, port, user, pwd = parts
        return f"http://{user}:{pwd}@{ip}:{port}"
    return f"http://{raw}"

PROXIES = [
    parse_proxy(p) for p in os.getenv("PROXIES", "").replace("\n", ",").split(",") if p.strip()
]

HEADERS = {"User-Agent": "Mozilla/5.0 (asset-scanner)"}
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2.1 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:123.0) Gecko/20100101 Firefox/123.0",
]

def random_headers() -> dict:
    return {"User-Agent": random.choice(USER_AGENTS)}

DETAILS_URL = "https://catalog.roblox.com/v1/catalog/items/details"
SEARCH_BOOT_URL = f"https://catalog.roblox.com/v1/search/items/details?CreatorType=User&CreatorTargetId={CREATOR_ID}&SortType=3&Limit=30&IncludeNotForSale=true"

SALE_LOCATIONS = {
    "shoponly": "Website only",
    "experiencesdevapionly": "Experiences only",
    "shopandallexperiences": "Website and experiences",
    "shopandmyexperiences": "Website and creator's experiences",
    "shopandexperiencesbyid": "Website and specific experiences",
}

# ----------------------------------------------------------------- state ----
state: dict[int, bool] = {}
latest: dict = {"items": []}
last_status = 0
_tasks: set[asyncio.Task] = set()

def spawn(coro) -> None:
    task = asyncio.create_task(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)

class ProxyPool:
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
        self.streak = 0
        self.last_ok = 0.0

    async def acquire(self) -> dict:
        slot = min(self.slots, key=lambda x: x["at"])
        now = time.monotonic()
        start = max(now, slot["at"])
        slot["at"] = start + self.gap
        if start > now:
            await asyncio.sleep(start - now)
        return slot

    def limited(self, slot: dict, retry_after: float) -> float:
        slot["fails"] += 1
        self.streak += 1
        delay = min(60.0, (retry_after + 1) * (1.5 ** (slot["fails"] - 1)))
        slot["at"] = max(slot["at"], time.monotonic() + delay)
        self.limited_count += 1
        return delay

    def failed(self, slot: dict) -> None:
        self.err_count += 1
        self.streak += 1
        slot["at"] = max(slot["at"], time.monotonic() + 3)

    def ok(self, slot: dict) -> None:
        slot["fails"] = 0
        self.ok_count += 1
        self.streak = 0
        self.last_ok = time.monotonic()

pool = ProxyPool(PROXIES)

# --------------------------------------------------------------- helpers ----
def normalize(value) -> str:
    return str(value or "").replace(" ", "").replace("_", "").lower()

def check_item_status(item: dict) -> tuple[bool, bool]:
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

def format_price(price) -> str:
    if price is None:
        return "?"
    if price == 0:
        return "Free"
    return f"R${price:,}"

def fmt_item(it: dict) -> str:
    _, limited = check_item_status(it)
    price = it.get("price") if it.get("price") is not None else it.get("lowestPrice")
    tag = "LIMITED" if limited else "normal"
    return f"  - {it.get('name', '?')} ({it.get('id')}) | {tag} | {format_price(price)}"

def build_embed(item: dict, is_limited: bool) -> tuple[dict, str | None]:
    asset_id = item["id"]
    price = item.get("price") if item.get("price") is not None else item.get("lowestPrice")
    quantity = item.get("totalQuantity")
    left = item.get("unitsAvailableForConsumption")

    qty_txt = f"{left}/{quantity}" if quantity and quantity > 0 and left is not None else (str(quantity) if quantity and quantity > 0 else "Unlimited")
    description = (item.get("description") or "").strip() or "—"
    if len(description) > 300:
        description = description[:297] + "..."

    location_raw = item.get("saleLocationType") or "Unknown"
    location = SALE_LOCATIONS.get(normalize(location_raw), location_raw)

    PING_ROLE_ID = os.getenv("PING_ROLE_ID", "")
    if is_limited:
        author, color = "New Limited", 0xE0457B
        content = f"<@&{PING_ROLE_ID}>" if PING_ROLE_ID else "@limited"
    else:
        author, color, content = "New Roblox Item", 0x3498DB, None

    width_pad = "\u2800" * 45
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
                    return None
                return _loads(await r.read()).get("id")
        except (aiohttp.ClientError, asyncio.TimeoutError):
            await asyncio.sleep(0.5)
    return None

async def fetch_thumbnail(session: aiohttp.ClientSession, asset_id: int) -> str | None:
    url = f"https://thumbnails.roblox.com/v1/assets?assetIds={asset_id}&size=420x420&format=Png"
    for _ in range(6):
        try:
            async with session.get(url, headers=random_headers(), timeout=aiohttp.ClientTimeout(total=4)) as r:
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
        async with session.patch(f"{WEBHOOK_URL}/messages/{message_id}", json={"embeds": [embed]}) as r:
            pass
    except (aiohttp.ClientError, asyncio.TimeoutError):
        pass

async def notify(session: aiohttp.ClientSession, item: dict, is_limited: bool) -> None:
    embed, content = build_embed(item, is_limited)
    payload = {"embeds": [embed], "allowed_mentions": {"parse": ["roles", "everyone"]}}
    if content:
        payload["content"] = content

    message_id = await post_webhook(session, payload)
    if not message_id:
        return

    image = await fetch_thumbnail(session, item["id"])
    if image:
        embed["thumbnail"] = {"url": image}
        await edit_message(session, message_id, embed)

# ---------------------------------------------------------- asset scanner ---
async def fetch_item_batch(session: aiohttp.ClientSession, asset_ids: list[int]) -> list[dict] | None:
    global last_status
    last_status = 0
    slot = await pool.acquire()
    payload = {"items": [{"itemType": "Asset", "id": aid} for aid in asset_ids]}
    try:
        proxy_url = slot["url"] if slot["url"] else None
        async with session.post(DETAILS_URL, json=payload, headers=random_headers(), proxy=proxy_url) as r:
            last_status = r.status
            if r.status == 429:
                pool.limited(slot, float(r.headers.get("retry-after", 5)))
                return None
            if r.status != 200:
                body_preview = (await r.text())[:150].replace("\n", " ")
                print(f"[catalog POST] proxy={slot['name']} status={r.status} | {body_preview}")
                pool.failed(slot)
                return None
            pool.ok(slot)
            body = _loads(await r.read())
            return body.get("data", [])
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        print(f"[catalog POST] proxy={slot['name']} error: {e!r}")
        pool.failed(slot)
        return None

def handle_item(session: aiohttp.ClientSession, item: dict) -> None:
    ok, limited = check_item_status(item)
    if not ok:
        return
    item_id = item["id"]
    previous = state.get(item_id)

    if previous is None:
        state[item_id] = limited
        print(f"[new official] {item.get('name')} ({item_id}) | limited={limited}")
        spawn(notify(session, item, limited))
    elif limited and not previous:
        state[item_id] = True
        print(f"[went limited] {item.get('name')} ({item_id})")
        spawn(notify(session, item, True))

async def worker(session: aiohttp.ClientSession, index: int, current_id_holder: dict) -> None:
    period = POLL_INTERVAL * WORKERS
    await asyncio.sleep(index * POLL_INTERVAL)
    while True:
        started = time.monotonic()
        try:
            batch_size = 30
            start_id = current_id_holder["val"]
            current_id_holder["val"] += batch_size
            batch_ids = list(range(start_id, start_id + batch_size))

            items = await fetch_item_batch(session, batch_ids)
            if items:
                latest["items"] = items
                for it in items:
                    handle_item(session, it)
            else:
                await asyncio.sleep(0.5)
        except Exception as e:
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
        print("[init] Recherche du dernier ID Roblox de référence...")
        slot = await pool.acquire()
        highest_id = 0
        first_match = None
        try:
            proxy_url = slot["url"] if slot["url"] else None
            async with session.get(SEARCH_BOOT_URL, headers=random_headers(), proxy=proxy_url) as r:
                if r.status == 200:
                    data = _loads(await r.read()).get("data", [])
                    if data:
                        highest_id = max(it["id"] for it in data)
                        for it in data:
                            ok, limited = check_item_status(it)
                            if ok:
                                state[it["id"]] = limited
                                first_match = first_match or (it, limited)
        except Exception as e:
            print(f"[init] Erreur boot search: {e}")

        if highest_id == 0:
            highest_id = 11000000000

        current_id_holder = {"val": highest_id + 1}
        print(f"[init] Scanner d'ID démarré à partir de : {current_id_holder['val']}")
        print(f"[init] workers={WORKERS} | interval={POLL_INTERVAL}s | proxies={len(PROXIES)}")

        if HEARTBEAT > 0:
            spawn(heartbeat())

        if TEST_ON_START and first_match:
            print("[test] sending startup test alert")
            spawn(notify(session, *first_match))

        await asyncio.gather(*(worker(session, i, current_id_holder) for i in range(WORKERS)))

if __name__ == "__main__":
    asyncio.run(main())
