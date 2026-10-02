"""Interactive login in an ephemeral browser; reuse the gateway's OAuth capture.

Only observes O2's own SAPI traffic in the browser launched by this application.
No stored browser profile, storage dump, passwords or OTP collection.
"""

from __future__ import annotations

import asyncio
from urllib.parse import parse_qs, urlparse

from o2gateway.o2.login import (
    O2BrowserSessionState,
    _capture_oauth_request,
    _capture_oauth_response,
)
from o2gateway.o2.session import O2Cookie, O2Session

from .security import BridgeError


async def browser_session(api, timeout=300):
    from playwright.async_api import async_playwright

    network = O2BrowserSessionState()
    user_agent = ""
    tasks = set()

    def sapi(url):
        parsed = urlparse(url)
        return parsed.hostname == "cloud.o2online.es" and parsed.path.startswith("/sapi/")

    async def request_seen(request):
        nonlocal user_agent
        if not sapi(request.url):
            return
        parsed = urlparse(request.url)
        value = parse_qs(parsed.query).get("validationkey", [""])[0]
        if value:
            network.validation_key = value
        headers = await request.all_headers()
        user_agent = headers.get("user-agent", user_agent)
        # Existing helper handles the observed OAuth and device headers.
        await _capture_oauth_request(request, network)

    async def response_seen(response):
        if sapi(response.url) and urlparse(response.url).path.rstrip("/").endswith("/login/oauth"):
            await _capture_oauth_response(response, network)

    def schedule(coro):
        task = asyncio.create_task(coro)
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context(
            viewport={"width": 1100, "height": 780}, color_scheme="dark"
        )
        try:
            page = await context.new_page()
            page.on("request", lambda request: schedule(request_seen(request)))
            page.on("response", lambda response: schedule(response_seen(response)))
            await page.goto("https://cloud.o2online.es/login", wait_until="domcontentloaded")
            deadline = asyncio.get_running_loop().time() + timeout
            while asyncio.get_running_loop().time() < deadline:
                if tasks:
                    await asyncio.gather(*list(tasks), return_exceptions=True)
                cookies = [
                    O2Cookie(c["name"], c["value"], c["domain"], c["path"])
                    for c in await context.cookies("https://cloud.o2online.es/")
                    if c["name"] in ("JSESSIONID", "validationKey")
                ]
                if network.validation_key and any(c.name == "JSESSIONID" for c in cookies):
                    session = O2Session(
                        validation_key=network.validation_key,
                        cookies=cookies,
                        user_agent=user_agent,
                        oauth_bundle=network.oauth_bundle,
                        device_id=network.device_id,
                        device_name=network.device_name,
                        encryption_token=network.encryption_token,
                    )
                    if await api.validate_session(session):
                        return session
                await asyncio.sleep(2)
            raise BridgeError(
                "LOGIN_TIMEOUT", "O2 no ha confirmado una sesión válida. Vuelve a conectar."
            )
        finally:
            await asyncio.gather(*list(tasks), return_exceptions=True)
            await context.close()
            await browser.close()
