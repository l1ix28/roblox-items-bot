"""
Notifier Roblox Officiel (Limiteds + Items normaux) -> webhook Discord.
Aucune action sur ton compte : lecture seule de l'API publique du catalogue.
"""

import asyncio
import itertools
import os
import time
from datetime import datetime, timezone

import aiohttp
from aiohttp import web
from dotenv import load_dotenv

load_dotenv()

WEBHOOK_URL = os.environ["WEBHOOK_URL"]
POLL_INTERVAL = float(os.getenv("POLL_INTERVAL", "1.5"))  # secondes entre 2 requêtes
PING_ROLE_ID = os.getenv("PING_ROLE_ID", "")  # ID du rôle @limited (optionnel)
CATEGORY = os.getenv("CATALOG_CATEGORY", "1")  # à ajuster si besoin
PROXIES = [p.strip() for p in os.getenv("PROXIES", "").split(",") if p.strip()]
TEST_ON_START = os.getenv("TEST_ON_START", "0") == "1"

# URL filtrée spécifiquement sur le créateur officiel Roblox (CreatorTargetId=1)
CATALOG_URL = (
    "https://catalog.roblox.com/v1/search/items/details"
    f"?Category={CATEGORY}&CreatorType=User&CreatorTargetId=1&SortType=3&Limit=30"
)
HEADERS = {"User-Agent": "Mozilla/5.0 (limited-notifier)"}

# id -> {"limited": bool} pour stocker l'état
state: dict[int, dict] = {}
proxy_cycle = itertools.cycle(PROXIES) if PROXIES else None


def check_item_status(item: dict) -> tuple[bool, bool]:
    """
    Vérifie si c'est un item officiel Roblox et s'il est limited.
    Retourne (is_roblox_official, is_limited)
    """
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
    """Récupère l'image officielle de l'item via l'API Thumbnail de Roblox."""
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
    price = item.get("price") or item.get("lowestPrice")
    quantity = item.get("totalQuantity")
    left = item.get("unitsAvailableForConsumption")

    price_txt = f"R$ {price:,}" if isinstance(price, int) and price > 0 else "Free"
    
    if quantity and quantity > 0:
        qty_txt = f"{left}/{quantity}" if left is not None else str(quantity)
    else:
        qty_txt = "Unlimited"

    description = (item.get("description") or "").strip() or "—"
    image_url = await get_thumbnail(session, asset_id)

    # Configuration selon si c'est un Limited ou un item normal Roblox
    if is_limited:
        author_name = "Went Limited"
        color = 0xE6A8F0  # Rose
        content = f"<@&{PING_ROLE_ID}>" if PING_ROLE_ID else "@limited"
    else:
        author_name = "New Roblox Item"
        color = 0x3498DB  # Bleu (pas rose, pas de ping)
        content = None

    embed = {
        "title": item.get("name", "Unknown"),
        "url": f"https://www.roblox.com/catalog/{asset_id}",
        "color": color,
        "author": {"name": author_name},
        "description": f"**Description:**\n{description[:900]}",
        "fields": [
            {"name": "Asset ID", "value": f"`{asset_id}`", "inline": True},
            {"name": "Prix", "value": price_txt, "inline": True},
            {"name": "Quantité", "value": qty_txt, "inline": True},
        ],
        "footer": {"text": "Detected at"},
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
                print(f"[webhook] erreur {r.status}: {await r.text()}")
            return


async def fetch_items(session: aiohttp.ClientSession) -> list[dict] | None:
    proxy = next(proxy_cycle) if proxy_cycle else None
    try:
        async with session.get(CATALOG_URL, proxy=proxy) as r:
            if r.status == 429:
                retry = float(r.headers.get("retry-after", 5))
                print(f"[catalog] rate limit, pause {retry}s")
                await asyncio.sleep(retry)
                return None
            if r.status != 200:
                print(f"[catalog] statut {r.status}")
                return None
            return (await r.json()).get("data", [])
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        print(f"[catalog] erreur réseau: {e}")
        return None


async def start_health_server() -> None:
    """Mini serveur HTTP pour Render (Web Service) et pour les pings keep-alive."""

    async def health(_request: web.Request) -> web.Response:
        return web.Response(text="ok")

    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", "10000"))
    await web.TCPSite(runner, "0.0.0.0", port).start()
    print(f"[http] serveur de santé sur le port {port}")


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
        # Amorçage : on mémorise l'existant sans notifier
        items = None
        while items is None:
            items = await fetch_items(session)
            if items is None:
                await asyncio.sleep(2)
        
        for it in items:
            is_roblox, is_lim = check_item_status(it)
            if is_roblox:
                state[it["id"]] = {"limited": is_lim}
                
        print(f"[init] {len(state)} items officiels Roblox en cache. Surveillance lancée.")

        if TEST_ON_START and items:
            print("[test] Envoi d'une alerte de test au démarrage...")
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
                        # Nouvel item officiel Roblox découvert
                        state[item_id] = {"limited": is_lim}
                        print(f"[nouveau Roblox] {it.get('name')} ({item_id}) | Limited: {is_lim}")
                        asyncio.create_task(process_item_notification(session, it, is_lim))
                    else:
                        # Si l'item devient limited alors qu'il ne l'était pas
                        if is_lim and not previous["limited"]:
                            state[item_id]["limited"] = True
                            print(f"[Went Limited] {it.get('name')} ({item_id})")
                            asyncio.create_task(process_item_notification(session, it, True))

            elapsed = time.monotonic() - started
            await asyncio.sleep(max(0.0, POLL_INTERVAL - elapsed))


if __name__ == "__main__":
    asyncio.run(main())
