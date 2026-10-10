"""Render shipped pages with isolated API fixtures; never use a live account.

Run: uv run --with playwright==1.57.0 python scripts/check_management_ui.py
Install Chromium first with: uv run --with playwright==1.57.0 playwright install chromium
"""

import argparse
import json
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

STATIC = Path(__file__).resolve().parents[1] / "extore" / "static"
AUTH = {
    "configured": True,
    "password_enabled": False,
    "registration_enabled": True,
    "role": "admin",
    "shop_id": None,
    "superadmin": True,
    "session_id": "isolated-review-session",
}
FIXTURES = {
    "/api/source": {},
    "/api/auth/status": AUTH,
    "/api/admin/products": [],
    "/api/products": [],
    "/api/admin/product-templates": [],
    "/api/platform/shops": [
        {
            "id": "review-shop-one",
            "name": "拾光工作室",
            "email": "owner@example.test",
            "verified": True,
            "enabled": True,
        },
        {
            "id": "review-shop-two",
            "name": "文字与设计",
            "email": "design@example.test",
            "verified": False,
            "enabled": True,
        },
    ],
    "/api/platform/settings": {
        "registration_enabled": False,
        "smtp": {
            "enabled": True,
            "host": "smtp.example.test",
            "sender": "store@example.test",
            "from_name": "Extore",
            "port": 587,
            "mode": "starttls",
            "password_configured": True,
        },
    },
    "/api/auth/passkeys": [
        {"id": "review-passkey", "name": "日常使用的设备", "created": 1791547200}
    ],
    "/api/auth/reauth/passkey/options": {
        "challenge": "Y2hhbGxlbmdl",
        "userVerification": "required",
        "allowCredentials": [{"id": "a2V5", "type": "public-key"}],
    },
    "/api/upload-limits": {"max_file_bytes": 20971520},
}


class Handler(SimpleHTTPRequestHandler):
    def translate_path(self, path):
        relative = urlparse(path).path.removeprefix("/static/")
        target = (STATIC / relative).resolve()
        return str(
            target
            if target.is_relative_to(STATIC) and target.is_file()
            else STATIC / "index.html"
        )

    def log_message(self, *_args):
        pass


def review(browser, origin, output, width, theme, language="zh-CN"):
    context = browser.new_context(
        viewport={"width": width, "height": 900},
        locale=language,
        reduced_motion="reduce",
        color_scheme=theme,
    )
    requests, errors, shots = [], [], []
    anonymous = False

    def route_request(route):
        request = route.request
        if not request.url.startswith(origin + "/"):
            route.abort()
            return
        path = urlparse(request.url).path
        if not path.startswith("/api/"):
            route.continue_()
            return
        requests.append(
            {"path": path, "method": request.method, "headers": request.headers}
        )
        if path == "/api/auth/reauth/passkey/verify":
            route.fulfill(
                status=401, json={"detail": "所选 Passkey 不属于当前账号，请重新选择"}
            )
        elif path == "/api/auth/status" and anonymous:
            route.fulfill(
                json={**AUTH, "role": None, "superadmin": False, "session_id": None}
            )
        elif path in FIXTURES:
            route.fulfill(json=FIXTURES[path])
        else:
            errors.append(f"Unexpected API: {path}")
            route.fulfill(status=404, json={"detail": "Missing review fixture"})

    context.route("**/*", route_request)
    context.add_init_script("""Object.defineProperty(navigator, 'credentials', {value: {get: async () => ({
      id: 'wrong-key', rawId: new Uint8Array([1]).buffer, type: 'public-key', getClientExtensionResults: () => ({}),
      response: {clientDataJSON: new Uint8Array([1]).buffer, authenticatorData: new Uint8Array([2]).buffer,
        signature: new Uint8Array([3]).buffer, userHandle: null}
    })}});""")
    page = context.new_page()
    page.on("pageerror", lambda error: errors.append(str(error)))

    def snap(name):
        page.evaluate("window.scrollTo(0, 0)")
        page.wait_for_timeout(80)
        assert not page.evaluate("document.documentElement.scrollWidth > innerWidth"), (
            f"Horizontal overflow: {name}/{width}"
        )
        assert not errors, errors
        filename = f"{width}-{theme}-{language}-{name}.png"
        page.screenshot(path=str(output / filename), full_page=True)
        shots.append(filename)

    def tab(name):
        if width <= 860:
            page.locator("#management-section").select_option(name)
        else:
            page.locator(f'[data-tab="{name}"]').click()
        page.wait_for_timeout(80)

    page.goto(origin + "/admin")
    page.locator(".management-shell").wait_for()
    snap("products")
    if width <= 860:
        page.locator("#management-menu-toggle").click()
        assert (
            page.locator("#management-menu-toggle").get_attribute("aria-expanded")
            == "true"
        )
        assert page.locator('[data-tab="mail"]').is_visible()
        assert page.locator('[data-tab="security"]').is_visible()
        snap("directory")
        page.locator('[data-tab="shops"]').click()
        assert (
            page.locator("#management-menu-toggle").get_attribute("aria-expanded")
            == "false"
        )
    else:
        tab("shops")
    page.locator("#shop-name").fill("待确认的新店铺")
    page.locator("#shop-email").fill("new@example.test")
    snap("shops")
    page.locator("#account-shop-form button[type=submit]").click()
    page.locator("#account-fresh-passkey").click()
    page.locator("#account-error").filter(has_text="不属于当前账号").wait_for()
    assert page.locator("#shop-name").input_value() == "待确认的新店铺"
    assert page.locator("#shop-email").input_value() == "new@example.test"
    assert not any(
        item["path"].startswith("/api/auth/login/")
        or (item["path"] == "/api/platform/shops" and item["method"] == "POST")
        for item in requests
    )
    for item in requests:
        if item["path"].startswith("/api/auth/reauth/passkey/"):
            assert item["headers"]["x-extore-shop-scope"] == "platform"
            assert item["headers"]["x-extore-session-id"] == AUTH["session_id"]
    snap("wrong-passkey")
    page.locator("#account-fresh-cancel").click()
    tab("mail")
    page.locator("#mail-host").fill("draft.example.test")
    snap("mail")
    page.locator("#appearance summary").click()
    page.locator("#theme").select_option(theme)
    for accent in ["green", "blue", "violet", "rose", "amber", "graphite"]:
        page.locator("#accent").select_option(accent)
        assert page.locator("html").get_attribute("data-accent") == accent
        assert page.locator("#mail-host").input_value() == "draft.example.test"
    snap("appearance")
    page.locator("#appearance summary").press("Escape")
    assert not page.locator("#appearance").evaluate("node => node.open")
    tab("security")
    page.get_by_text("日常使用的设备", exact=True).wait_for()
    snap("security")
    page.reload()
    assert page.locator("html").get_attribute("data-accent") == "graphite"
    anonymous = True
    page.goto(origin + "/account/login")
    page.locator("#login-email").wait_for()
    snap("login")
    page.goto(origin + "/")
    page.locator("#code").wait_for()
    snap("home")
    context.close()
    return {
        "width": width,
        "theme": theme,
        "language": language,
        "screenshots": shots,
        "errors": errors,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path("output/ui-review"))
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            try:
                results = [
                    review(
                        browser,
                        f"http://127.0.0.1:{server.server_port}",
                        args.output,
                        width,
                        theme,
                        language,
                    )
                    for width, theme, language in [
                        (320, "dark", "zh-CN"),
                        (390, "light", "zh-CN"),
                        (768, "dark", "en"),
                        (1440, "light", "zh-CN"),
                    ]
                ]
            finally:
                browser.close()
        (args.output / "results.json").write_text(
            json.dumps(results, ensure_ascii=False, indent=2)
        )
        print(
            f"Reviewed {sum(len(result['screenshots']) for result in results)} screenshots across {len(results)} viewport/theme combinations."
        )
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
