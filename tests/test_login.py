import sys
from types import SimpleNamespace

import pytest

from bridge.login import browser_session
from bridge.security import BridgeError


class Request:
    url = "https://cloud.o2online.es/sapi/login/oauth?action=login"

    async def all_headers(self):
        return {
            "authorization": "oauth fresh-oauth",
            "x-deviceid": "browser-device",
            "user-agent": "Browser UA",
        }


class Response:
    url = Request.url

    async def all_headers(self):
        return {}

    async def json(self):
        return {"data": {"validationkey": "fresh-validation", "access_token": "fresh-oauth"}}


class Page:
    def __init__(self):
        self.events = {}

    def on(self, name, fn):
        self.events[name] = fn

    async def goto(self, *args, **kwargs):
        self.events["request"](Request())
        self.events["response"](Response())


class Context:
    closed = False

    async def new_page(self):
        return Page()

    async def cookies(self, origin):
        assert origin == "https://cloud.o2online.es/"
        return [
            {"name": "JSESSIONID", "value": "fresh-js", "domain": "cloud.o2online.es", "path": "/"},
            {
                "name": "validationKey",
                "value": "stale-cookie",
                "domain": "cloud.o2online.es",
                "path": "/",
            },
            {
                "name": "analytics",
                "value": "not-a-session",
                "domain": "cloud.o2online.es",
                "path": "/",
            },
        ]

    async def close(self):
        self.closed = True


class Browser:
    closed = False

    def __init__(self):
        self.context = Context()

    async def new_context(self, **kwargs):
        assert kwargs["color_scheme"] == "dark"
        return self.context

    async def close(self):
        self.closed = True


class Runtime:
    def __init__(self):
        self.browser = Browser()
        self.chromium = self

    async def launch(self, **kwargs):
        assert not kwargs["headless"]
        return self.browser

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


@pytest.fixture
def runtime(monkeypatch):
    runtime = Runtime()
    monkeypatch.setitem(
        sys.modules, "playwright.async_api", SimpleNamespace(async_playwright=lambda: runtime)
    )
    return runtime


async def test_authentication_validates_network_session_before_closing(runtime):
    class Api:
        async def validate_session(self, session):
            assert session.validation_key == "fresh-validation"
            assert session.oauth_bundle == "fresh-oauth"
            assert session.device_id == "browser-device"
            assert session.user_agent == "Browser UA"
            assert {c.name for c in session.cookies} == {"JSESSIONID", "validationKey"}
            return True

    result = await browser_session(Api())
    assert result.validation_key == "fresh-validation"
    assert runtime.browser.closed and runtime.browser.context.closed


async def test_authentication_timeout_closes_ephemeral_browser(runtime):
    with pytest.raises(BridgeError, match="sesión válida"):
        await browser_session(SimpleNamespace(), timeout=0)
    assert runtime.browser.closed and runtime.browser.context.closed
