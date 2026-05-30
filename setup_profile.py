"""Automated one-time setup of the persistent Firefox profile used by `--vpn`.

`main.py --vpn` needs a `./profile/` that is already (a) signed in to a Firefox
Account and (b) has `browser.ipProtection.enabled = true`. The README walks
through doing this by hand; this script does it unattended instead.

It mirrors how mozilla/blurts-server's functional tests create throwaway
accounts: register a fresh `<something>@restmail.net` address, drive the Firefox
Account sign-up form (email → password → confirmation code), and read the
verification short-code straight out of the restmail inbox over HTTP. The one
fireflare-specific twist is that we sign the *browser* in (via the FxA desktop
"connect account" flow / WebChannel) rather than a website, so IP protection
picks up the session.

Usage:
    uv run setup_profile.py                 # create a fresh restmail account
    uv run setup_profile.py --email a@b.c --password 'hunter2longer'
    uv run setup_profile.py --custom-firefox
"""

from __future__ import annotations

import argparse
import json
import re
import secrets
import sys
import time
import urllib.request
from datetime import datetime, timezone

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from main import (
    FIREFOX_CUSTOM_URL,
    FIREFOX_NIGHTLY_URL,
    PROFILE,
    ROOT,
    build_driver,
    ensure_firefox,
    ensure_geckodriver,
    firefox_version,
    require_linux_x86_64,
    scrub_profile_test_stubs,
)

# Where we stash the generated credentials so the account can be reused or
# looked up later. Gitignored alongside ./profile/.
ACCOUNT_FILE = ROOT / "profile-account.json"

RESTMAIL_DOMAIN = "restmail.net"


def generate_email() -> str:
    """A unique restmail address. Restmail inboxes are public and created on
    first read, so a hard-to-guess local part is all the isolation we get."""
    return f"fireflare-{secrets.token_hex(6)}@{RESTMAIL_DOMAIN}"


def generate_password() -> str:
    """Meet FxA's policy (>= 8 chars, mixed classes, not the email)."""
    return f"Fireflare!{secrets.token_hex(5)}"


def fetch_restmail_code(email: str, timeout_s: int = 120, interval_s: float = 3.0) -> str:
    """Poll the restmail inbox until the FxA verification short-code arrives.

    Restmail exposes each inbox as JSON at `/mail/<local-part>`. FxA's
    confirmation email carries the code in the `x-verify-short-code` header
    (template `verifyShortCode`); we fall back to scraping a 6-digit code from
    the subject of an obviously-verification email if the header is missing.
    """
    local_part = email.split("@", 1)[0]
    url = f"https://{RESTMAIL_DOMAIN}/mail/{local_part}"
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url) as resp:
                mails = json.load(resp)
        except Exception as e:  # transient network / empty inbox 404
            print(f"  restmail not ready yet ({e}); retrying...")
            mails = []
        # Newest first so a re-run picks up the latest code, not a stale one.
        mails.sort(key=lambda m: m.get("receivedAt", 0), reverse=True)
        for mail in mails:
            headers = mail.get("headers", {})
            if headers.get("x-template-name") == "verifyShortCode":
                code = headers.get("x-verify-short-code")
                if code:
                    return str(code)
        for mail in mails:
            subject = mail.get("subject", "") or ""
            if re.search(r"verif|confirm|code", subject, re.IGNORECASE):
                m = re.search(r"\b(\d{6})\b", subject)
                if m:
                    return m.group(1)
        time.sleep(interval_s)
    sys.exit(
        f"no FxA verification code arrived at {url} within {timeout_s}s "
        f"(inbox had {len(mails)} message(s))"
    )


def enable_ip_protection_pref(driver: webdriver.Firefox) -> None:
    """Persist `browser.ipProtection.enabled = true` into the profile.

    Replaces the manual `about:config` flip from the README. Written as a user
    pref, so it lands in prefs.js on the next clean shutdown.
    """
    with driver.context(driver.CONTEXT_CHROME):
        driver.execute_script(
            "Services.prefs.setBoolPref('browser.ipProtection.enabled', true);"
        )


def fxa_connect_url(driver: webdriver.Firefox) -> str:
    """Ask Firefox for the same "connect your account" URL its Sync pane uses.

    Going through this URL (rather than just loading accounts.firefox.com) is
    what wires the FxA WebChannel up to the browser, so a successful sign-up
    actually signs the *browser* in instead of just a web session.
    """
    driver.set_script_timeout(60)
    with driver.context(driver.CONTEXT_CHROME):
        result = driver.execute_async_script("""
            const done = arguments[arguments.length - 1];
            const { FxAccountsConfig } = ChromeUtils.importESModule(
              'resource://gre/modules/FxAccountsConfig.sys.mjs'
            );
            FxAccountsConfig.promiseConnectAccountURI('fireflare').then(
              uri => done({ uri }),
              e => done({ error: String(e) })
            );
        """)
    if result.get("error"):
        sys.exit(f"could not build FxA connect URL: {result['error']}")
    return result["uri"]


def signed_in_email(driver: webdriver.Firefox) -> str | None:
    """The email of the account the browser is currently signed in as, if any."""
    driver.set_script_timeout(30)
    with driver.context(driver.CONTEXT_CHROME):
        result = driver.execute_async_script("""
            const done = arguments[arguments.length - 1];
            let fxa;
            try {
              const m = ChromeUtils.importESModule(
                'resource://gre/modules/FxAccounts.sys.mjs'
              );
              fxa = m.getFxAccountsSingleton ? m.getFxAccountsSingleton() : m.fxAccounts;
            } catch (e) {
              done({ error: String(e) });
              return;
            }
            fxa.getSignedInUser().then(
              u => done({ email: u ? u.email : null }),
              e => done({ error: String(e) })
            );
        """)
    if result.get("error"):
        return None
    return result.get("email")


def _submit_enclosing_form(field) -> None:
    field.find_element(By.XPATH, "./ancestor::form[1]//button[@type='submit']").click()


def fill_and_submit_email(driver: webdriver.Firefox, email: str) -> None:
    field = WebDriverWait(driver, 60).until(
        EC.element_to_be_clickable((By.CSS_SELECTOR, 'input[name="email"]'))
    )
    field.clear()
    field.send_keys(email)
    _submit_enclosing_form(field)


def fill_and_submit_password(driver: webdriver.Firefox, password: str) -> None:
    WebDriverWait(driver, 60).until(
        EC.element_to_be_clickable((By.CSS_SELECTOR, 'input[type="password"]'))
    )
    # The sign-up page has both a password and a "repeat password" field; fill
    # every password input with the same value so validation passes.
    fields = driver.find_elements(By.CSS_SELECTOR, 'input[type="password"]')
    for field in fields:
        field.clear()
        field.send_keys(password)
    # Older flows additionally gate sign-up behind an age field.
    age = driver.find_elements(By.CSS_SELECTOR, 'input[name="age"]')
    if age:
        age[0].send_keys("25")
    _submit_enclosing_form(fields[0])


def fill_and_submit_code(driver: webdriver.Firefox, email: str) -> None:
    WebDriverWait(driver, 60).until(
        lambda d: "confirm_signup_code" in d.current_url
        or d.find_elements(By.CSS_SELECTOR, 'input[name="code"]')
    )
    print("Waiting for the verification code from restmail...")
    code = fetch_restmail_code(email)
    print(f"  got code {code}")
    field = WebDriverWait(driver, 60).until(
        EC.element_to_be_clickable((By.CSS_SELECTOR, 'input[name="code"]'))
    )
    field.clear()
    field.send_keys(code)
    _submit_enclosing_form(field)


def sign_up(driver: webdriver.Firefox, email: str, password: str) -> None:
    print(f"Signing up {email} ...")
    fill_and_submit_email(driver, email)
    fill_and_submit_password(driver, password)
    fill_and_submit_code(driver, email)


def wait_until_signed_in(driver: webdriver.Firefox, timeout_s: int = 120) -> str:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        email = signed_in_email(driver)
        if email:
            return email
        time.sleep(2)
    sys.exit(
        f"browser did not report a signed-in Firefox Account within {timeout_s}s "
        f"(the WebChannel sign-in may not have completed)"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--email",
        help="use this address instead of generating a fresh restmail one "
        "(must be reachable for the verification code if not @restmail.net)",
    )
    parser.add_argument(
        "--password",
        help="use this password instead of generating one (>= 8 chars)",
    )
    parser.add_argument(
        "--custom-firefox", action="store_true",
        help="use the hardcoded custom build instead of the latest Nightly",
    )
    args = parser.parse_args()

    if args.email and not args.email.endswith("@" + RESTMAIL_DOMAIN):
        sys.exit(
            "automated verification only works with @restmail.net addresses; "
            "sign in by hand (see README) for other providers"
        )

    require_linux_x86_64()
    PROFILE.mkdir(parents=True, exist_ok=True)

    firefox = ensure_firefox(
        FIREFOX_CUSTOM_URL if args.custom_firefox else FIREFOX_NIGHTLY_URL
    )
    geckodriver = ensure_geckodriver()
    print(f"Using {firefox_version(firefox)}")

    email = args.email or generate_email()
    password = args.password or generate_password()

    scrub_profile_test_stubs()
    driver = build_driver(firefox, geckodriver)
    try:
        enable_ip_protection_pref(driver)
        url = fxa_connect_url(driver)
        print(f"Opening FxA sign-up flow at {url}")
        driver.get(url)
        sign_up(driver, email, password)
        signed_in = wait_until_signed_in(driver)
        print(f"Browser is signed in as {signed_in}")
    finally:
        # A clean quit releases the profile lock and flushes prefs.js, so the
        # ipProtection pref and the FxA session both persist for `--vpn` runs.
        driver.quit()

    ACCOUNT_FILE.write_text(
        json.dumps(
            {
                "email": email,
                "password": password,
                "created": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            },
            indent=2,
        )
    )
    print(
        f"Saved credentials to {ACCOUNT_FILE.relative_to(ROOT)}.\n"
        f"Profile is ready — run `uv run main.py --vpn`."
    )


if __name__ == "__main__":
    main()
