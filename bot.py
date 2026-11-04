async def start_health_server() -> web.AppRunner:
    """Mini HTTP server pour Render & UptimeRobot."""
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
    # 1. Lancer le serveur HTTP en tâche de fond indépendante
    await start_health_server()
    
    # 2. Continuer la logique de monitoring
    timeout = aiohttp.ClientTimeout(total=8)
    connector = aiohttp.TCPConnector(limit=10, ttl_dns_cache=300)
    
    async with aiohttp.ClientSession(
        headers=HEADERS, timeout=timeout, connector=connector
    ) as session:
        # Bootstrapping: caching existing items
        items = None
        while items is None:
            items = await fetch_items(session)
            if items is None:
                await asyncio.sleep(2)
        
        for it in items:
            is_roblox, is_lim = check_item_status(it)
            if is_roblox:
                state[it["id"]] = {"limited": is_lim}
                print(f"[cached] {it.get('name')} (ID: {it['id']}) | Limited: {is_lim}")
                
        print(f"[init] {len(state)} official Roblox accessories cached. Monitoring started.")

        if TEST_ON_START and items:
            print("[test] Sending startup test alert...")
            is_roblox, is_lim = check_item_status(items[0])
            if is_roblox:
                asyncio.create_task(process_item_notification(session, items[0], is_lim))

        # Boucle de polling
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
