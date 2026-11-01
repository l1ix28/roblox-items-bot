"""
Notifier Roblox "Went Limited" (Créations officielles Roblox uniquement) -> webhook Discord.
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

# id -> True si l'item était déjà limité à la dernière vérification
state: dict[int, bool] = {}
proxy_cycle = itertools.cycle(PROXIES) if PROXIES else None


def is_limited(item: dict) -> bool:
    # Double vérification : créateur officiel Roblox obligatoire (ID 1)
    creator_id = item.get("creatorTargetId")
    if creator_id != 1:
        return False

    restrictions = item.get("itemRestrictions") or []
    return bool(
        "Limited" in restrictions
        or "LimitedUnique" in restrictions
        or "Collectible" in restrictions
    )


def build_payload(item: dict) -> dict:
    asset_id = item["id"]
    price = item.get("price") or item.get("lowestPrice")
    quantity = item.get("totalQuantity")
    left = item.get("unitsAvailableForConsumption")

    price_txt = f"R$ {price:,}" if isinstance(price, int) else "?"
    if quantity and left is not None:
        qty_txt = f"{left}/{quantity}"
    else:
        qty_txt = str(quantity) if quantity else "?"

    description = (item.get("description") or "").strip() or "—"

    embed = {
        "title": item.get("name", "Unknown"),
        "url": f"https://www.roblox.com/catalog/{asset_id}",
        "color": 0xE6A8F0,
        "author": {"name": "Went Limited"},
        "description": f"**Description:**\n{description[:900]}",
        "fields": [
            {"name": "Asset ID", "value": f"`{asset_id}`", "inline": True},
            {"name": "Prix", "value": price_txt, "inline": True},
            {"name": "Quantité", "value": qty_txt, "inline": True},
        ],
        "thumbnail": {
            "url": (
                "https://www.roblox.com/asset-thumbnail/image"
                f"?assetId={asset_id}&width=420&height=420&format=png"
            )
        },
        "footer": {"text": "Detected at"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    content = f"<@&{PING_ROLE_ID}>" if PING_ROLE_ID else "@limited"
    return {
        "content": content,
        "embeds": [embed],
        "allowed_mentions": {"parse": ["roles", "everyone"]},
    }


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
            state[it["id"]] = is_limited(it)
        print(f"[init] {len(state)} items officiels Roblox en cache. Surveillance lancée.")

        if TEST_ON_START and items:
            print("[test] Envoi d'une alerte de test au démarrage...")
            asyncio.create_task(send_webhook(session, build_payload(items[0])))

        while True:
            started = time.monotonic()
            items = await fetch_items(session)
            if items:
                for it in items:
                    now_limited = is_limited(it)
                    was_limited = state.get(it["id"])  # None = jamais vu
                    state[it["id"]] = now_limited
                    if now_limited and not was_limited:
                        print(f"[limited officiel] {it.get('name')} ({it['id']})")
                        asyncio.create_task(send_webhook(session, build_payload(it)))
            elapsed = time.monotonic() - started
            await asyncio.sleep(max(0.0, POLL_INTERVAL - elapsed))


if __name__ == "__main__":
    asyncio.run(main())
