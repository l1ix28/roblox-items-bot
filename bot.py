import asyncio
import functools
import itertools
import os
import random
import time
from datetime import datetime, timezone

import aiohttp
from aiohttp import web
from dotenv import load_dotenv

load_dotenv()

# Force immediate log printing on Render
print = functools.partial(print, flush=True)

WEBHOOK_URL = os.environ["WEBHOOK_URL"]
POLL_INTERVAL = float(os.getenv("POLL_INTERVAL", "0.6"))  # Légèrement ajusté pour préserver les proxies
PING_ROLE_ID = os.getenv("PING_ROLE_ID", "")
PROXIES = [p.strip() for p in os.getenv("PROXIES", "").split(",") if p.strip()]
TEST_ON_START = os.getenv("TEST_ON_START", "0") == "1"

CATALOG_URL = (
    "https://catalog.roblox.com/v1/search/items/details"
    "?CreatorType=User&CreatorTargetId=1&SortType=3&Limit=30"
)

# Liste de vrais User-Agents pour simuler des navigateurs différents à chaque requête
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2.1 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:123.0) Gecko/20100101 Firefox/123.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
]

SALE_LOCATIONS = {
    "shoponly": "Website only",
    "experiencesdevapionly": "Experiences only",
    "shopandallexperiences": "Website and experiences",
    "shopandmyexperiences": "Website and creator's experiences",
    "shopandexperiencesbyid": "Website and specific experiences",
}

state: dict[int, dict] = {}
proxy_cycle = itertools.cycle(PROXIES) if PROXIES else None


def get_random_headers() -> dict:
    """Génère des en-têtes avec un User-Agent aléatoire pour chaque requête."""
    return {"User-Agent": random.choice(USER_AGENTS)}


def normalize(value):
    return str(value or "").replace(" ", "").replace("_", "").lower()


def check_item_status(item: dict) -> tuple[bool, bool]:
    creator_id = item.get("creatorTargetId")
    if creator_id != 1:
        return False, False

    restrictions = item.get("itemRestrictions") or []
    total_qty = item.get("totalQuantity") or 0
    
    is_lim = bool(
        "Limited" in restrictions
        or "LimitedUnique" in restrictions
        or "Collectible" in restrictions
        or (total_qty > 0 and item.get("collectibleItemId"))
    )
    return True, is_lim


async def get_thumbnail(session: aiohttp.ClientSession, asset_id: int) -> str | None:
    url = f"https://thumbnails.roblox.com/v1/assets?assetIds={asset_id}&size=420x420&format=Png"
    proxy = next(proxy_cycle) if proxy_cycle else None
    headers = get_random_headers()
    try:
        async with session.get(url, headers=headers, proxy=proxy, timeout=aiohttp.ClientTimeout(total=5)) as r:
            if r.status == 200:
                data = await r.json()
                results = data.get("data", [])
                if results and results[0].get("imageUrl"):
                    return results[0]["imageUrl"]
    except Exception:
        pass
    return None


def build_base_payload(item: dict, is_limited: bool) -> tuple[dict, dict]:
    asset_id = item["id"]
    name = item.get("name", "Unknown")
    price = item.get("price") or item.get("lowestPrice")
    quantity = item.get("totalQuantity")
    left = item.get("unitsAvailableForConsumption")

    price_txt = f"R${price:,}" if isinstance(price, int) and price > 0 else "Free"
    
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
        author_name = "New Limited"
        color = 0xE0457B
        content = f"<@&{PING_ROLE_ID}>" if PING_ROLE_ID else "@limited"
    else:
        author_name = "New Roblox Item"
        color = 0x3498DB
        content = None

    width_pad = "\u2800" * 45

    embed = {
        "author": {"name": author_name},
        "title": name,
        "url": f"https://www.roblox.com/catalog/{asset_id}",
        "color": color,
        "description": (
            f"**Asset ID:** {asset_id}\n\n"
            f"**Description:**\n{description}\n\n"
            f"**Purchase location:**\n{location}\n\n"
            f"{width_pad}"
        ),
        "fields": [
            {"name": "Price", "value": price_txt, "inline": True},
            {"name": "Quantity", "value": qty_txt, "inline": True},
        ],
        "footer": {"text": "Detected at:"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    payload = {
        "embeds": [embed],
        "allowed_mentions": {"parse": ["roles", "everyone"]},
    }
    if content:
        payload["content"] = content

    return payload, embed


async def send_instant_webhook(session: aiohttp.ClientSession, payload: dict) -> dict | None:
    url = f"{WEBHOOK_URL}?wait=true"
    for _ in range(3):
        async with session.post(url, json=payload) as r:
            if r.status == 429:
                data = await r.json()
                await asyncio.sleep(float(data.get("retry_after", 1)))
                continue
            if r.status in (200, 201):
                return await r.json()
            if r.status >= 400:
                print(f"[webhook] error {r.status}: {await r.text()}")
            return None


async def update_message_thumbnail(session: aiohttp.ClientSession, message_id: str, embed: dict, asset_id: int) -> None:
    image_url = await get_thumbnail(session, asset_id)
    if not image_url:
        return

    embed["thumbnail"] = {"url": image_url}
    patch_url = f"{WEBHOOK_URL}/messages/{message_id}"
    patch_payload = {"embeds": [embed]}
    
    try:
        async with session.patch(patch_url, json=patch_payload) as r:
            if r.status >= 400:
                print(f"[webhook patch] error {r.status}: {await r.text()}")
    except Exception as e:
        print(f"[webhook patch] exception: {e}")


async def process_item_notification(session, item, is_limited):
    asset_id = item["id"]
    payload, embed = build_base_payload(item, is_limited)
    
    res = await send_instant_webhook(session, payload)
    if res and "id" in res:
        asyncio.create_task(update_message_thumbnail(session, res["id"], embed, asset_id))


async def fetch_items(session: aiohttp.ClientSession) -> list[dict] | None:
    proxy = next(proxy_cycle) if proxy_cycle else None
    headers = get_random_headers()  # Changement de User-Agent à chaque requête catalogue
    try:
        async with session.get(CATALOG_URL, headers=headers, proxy=proxy) as r:
            if r.status == 429:
                retry = float(r.headers.get("retry-after", 5))
                print(f"[catalog] rate limit on proxy, sleeping {retry}s")
                await asyncio.sleep(retry)
                return None
            if r.status != 200:
                print(f"[catalog] status {r.status}")
                return None
            return (await r.json()).get("data", [])
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        print(f"[catalog] network error: {e}")
        return None


async def start_health_server():
    async def health(_request: web.Request) -> web.Response:
        return web.Response(text="ok", status=200)

    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", "10000"))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    print(f"[http] health check server running on port {port}")
    return runner


async def main() -> None:
    await start_health_server()
    
    timeout = aiohttp.ClientTimeout(total=8)
    connector = aiohttp.TCPConnector(limit=10, ttl_dns_cache=300)
    async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
        items = None
        while items is None:
            items = await fetch_items(session)
            if items is None:
                await asyncio.sleep(2)
        
        for it in items:
            is_roblox, is_lim = check_item_status(it)
            if is_roblox:
                state[it["id"]] = {"limited": is_lim}
                
        print(f"[init] {len(state)} Roblox items cached. Monitoring started.")

        if TEST_ON_START and items:
            print("[test] Sending startup test alert...")
            is_roblox, is_lim = check_item_status(items[0])
            if is_roblox:
                asyncio.create_task(process_item_notification(session, items[0], is_lim))

        while True:
            started = time.monotonic()
            items = await fetch_items(session)
            if items:
                for it in items:
                    is_roblox, is_lim = check_item_status(it)
                    if not is_roblox:
                        continue
                    
                    item_id = it["id"]
                    previous = state.get(item_id)
                    
                    if previous is None:
                        state[item_id] = {"limited": is_lim}
                        print(f"[new item] {it.get('name')} ({item_id}) | Limited: {is_lim}")
                        asyncio.create_task(process_item_notification(session, it, is_lim))
                    else:
                        if is_lim and not previous["limited"]:
                            state[item_id]["limited"] = True
                            print(f"[went limited] {it.get('name')} ({item_id})")
                            asyncio.create_task(process_item_notification(session, it, True))

            elapsed = time.monotonic() - started
            await asyncio.sleep(max(0.0, POLL_INTERVAL - elapsed))


if __name__ == "__main__":
    asyncio.run(main())
