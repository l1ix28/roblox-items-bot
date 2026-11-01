"""
Roblox Official Notifier (Limiteds + Normal Items) -> Formatted Discord Webhook.
No action on your account: read-only public catalog API.
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

load_dotenv()

# Force immediate log printing on Render
print = functools.partial(print, flush=True)

WEBHOOK_URL = os.environ["WEBHOOK_URL"]
POLL_INTERVAL = float(os.getenv("POLL_INTERVAL", "1.5"))  # seconds between requests
PING_ROLE_ID = os.getenv("PING_ROLE_ID", "")  # @limited role ID (optional)
PROXIES = [p.strip() for p in os.getenv("PROXIES", "").split(",") if p.strip()]
TEST_ON_START = os.getenv("TEST_ON_START", "0") == "1"

# URL filtered specifically for official Roblox creator (CreatorTargetId=1) 
# and strictly on Accessories (Category=11) to avoid faces, heads, and makeup.
CATALOG_URL = (
    "https://catalog.roblox.com/v1/search/items/details"
    "?Category=11&CreatorType=User&CreatorTargetId=1&SortType=3&Limit=30"
)
HEADERS = {"User-Agent": "Mozilla/5.0 (limited-notifier)"}

# id -> {"limited": bool} to store state
state: dict[int, dict] = {}
proxy_cycle = itertools.cycle(PROXIES) if PROXIES else None


def check_item_status(item: dict) -> tuple[bool, bool]:
    """Check if it's an official Roblox item and whether it is limited."""
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
    """Fetch official item thumbnail via Roblox Thumbnail API."""
    url = f"https://thumbnails.roblox.com/v1/assets?assetIds={asset_id}&size=420x420&format=Png"
    proxy = next(proxy_cycle) if proxy_cycle else None
    try:
        async with session.get(url, proxy=proxy, timeout=aiohttp.ClientTimeout(total=5)) as r:
            if r.status == 200:
                data = await r.json()
                results = data.get("data", [])
                if results and results[0].get("imageUrl"):
                    return results[0]["imageUrl"]
    except Exception:
        pass
    return None


async def build_payload(session: aiohttp.ClientSession, item: dict, is_limited: bool) -> dict:
    asset_id = item["id"]
    name = item.get("name", "Unknown")
    price = item.get("price") or item.get("lowestPrice")
    quantity = item.get("totalQuantity")
    left = item.get("unitsAvailableForConsumption")

    price_txt = f"{price:,} Robux" if isinstance(price, int) and price > 0 else "Free"
    
    if is_limited:
        stock_txt = f"📦 Stock: {left}/{quantity}" if left is not None and quantity else (f"📦 Stock: {quantity}" if quantity else "📦 Stock: Limited")
        status_line = f"New Roblox Limited Item {stock_txt}"
        color = 0xE0457B  # Vibrant pink
        content = f"<@&{PING_ROLE_ID}>" if PING_ROLE_ID else "@limited"
    else:
        stock_txt = f"📦 Stock: {quantity}" if quantity and quantity > 0 else "📦 Stock: Unlimited"
        status_line = f"New Roblox Item {stock_txt}"
        color = 0x3498DB  # Blue
        content = None

    image_url = await get_thumbnail(session, asset_id)

    embed = {
        "title": name,
        "url": f"https://www.roblox.com/catalog/{asset_id}",
        "color": color,
        "description": (
            f"{status_line}\n"
            f"🌐 [Roblox Page](https://www.roblox.com/catalog/{asset_id})"
        ),
        "fields": [
            {
                "name": "Creator",
                "value": "Roblox",
                "inline": False
            },
            {
                "name": "💰 Price",
                "value": price_txt,
                "inline": False
            }
        ],
        "footer": {"text": "Roblox Catalog Notifier"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    if image_url:
        embed["thumbnail"] = {"url": image_url}

    payload = {
        "embeds": [embed],
        "allowed_mentions": {"parse": ["roles", "everyone"]},
    }
    if content:
        payload["content"] = content

    return payload


async def send_webhook(session: aiohttp.ClientSession, payload: dict) -> None:
    for _ in range(3):
        async with session.post(WEBHOOK_URL, json=payload) as r:
            if r.status == 429:
                data = await r.json()
                await asyncio.sleep(float(data.get("retry_after", 1)))
                continue
            if r.status >= 400:
                print(f"[webhook] error {r.status}: {await r.text()}")
            return


async def fetch_items(session: aiohttp.ClientSession) -> list[dict] | None:
    proxy = next(proxy_cycle) if proxy_cycle else None
    try:
        async with session.get(CATALOG_URL, proxy=proxy) as r:
            if r.status == 429:
                retry = float(r.headers.get("retry-after", 5))
                print(f"[catalog] rate limit, sleeping {retry}s")
                await asyncio.sleep(retry)
                return None
            if r.status != 200:
                print(f"[catalog] status {r.status}")
                return None
            return (await r.json()).get("data", [])
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        print(f"[catalog] network error: {e}")
        return None


async def start_health_server() -> None:
    """Mini HTTP server for Render (Web Service)."""
    async def health(_request: web.Request) -> web.Response:
        return web.Response(text="ok")

    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", "10000"))
    await web.TCPSite(runner, "0.0.0.0", port).start()
    print(f"[http] health check server running on port {port}")


async def process_item_notification(session, item, is_limited):
    payload = await build_payload(session, item, is_limited)
    await send_webhook(session, payload)


async def main() -> None:
    await start_health_server()
    timeout = aiohttp.ClientTimeout(total=8)
    connector = aiohttp.TCPConnector(limit=10, ttl_dns_cache=300)
    async with aiohttp.ClientSession(
        headers=HEADERS, timeout=timeout, connector=connector
    ) as session:
        # Bootstrapping: caching existing items without notifying
        items = None
        while items is None:
            items = await fetch_items(session)
            if items is None:
                await asyncio.sleep(2)
        
        for it in items:
            is_roblox, is_lim = check_item_status(it)
            if is_roblox:
                state[it["id"]] = {"limited": is_lim}
                
        print(f"[init] {len(state)} official Roblox accessories cached. Monitoring started.")

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
                        print(f"[new accessory] {it.get('name')} ({item_id}) | Limited: {is_lim}")
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
