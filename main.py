"""Fireflare: Firefox Nightly runs @cloudflare/speedtest and we save its
structured results as JSON.

Direct (no proxy) baseline. HTTP CONNECT + MASQUE configs are future phases.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import threading
import time
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from selenium import webdriver
from selenium.webdriver.firefox.options import Options
from selenium.webdriver.firefox.service import Service

ROOT = Path(__file__).parent
CACHE = ROOT / ".cache"
RESULTS = ROOT / "results"
PROFILE = ROOT / "profile"

FIREFOX_NIGHTLY_URL = (
    "https://download.mozilla.org/?product=firefox-nightly-latest-ssl"
    "&os=linux64&lang=en-US"
)
# Build of the try push for IP-protection HTTP/3 experiments. Used by
# `--custom-firefox`. Try push:
# https://treeherder.mozilla.org/jobs?repo=try&revision=dfa4118d57f0ef8772dd566ca9de04ae359562bf
#
# Patch stack on top of mozilla-central, newest first:
#   e84f456b9a  Proxy remote subresources of loopback-hosted pages
#   571a01bbbb  Use MASQUE (connect-udp) for the outer VPN connection
#   1b21315eed  Bug 2005211, skip Alt-Svc route validation, which is what
#               carries HTTP/3 to the origin (connect-udp inner)
#   434f5a0cc9  Bug 1978893, back-pressure the inner connect-udp connection
#   c7e1938d97  Bug 1978893, vendor neqo (mxinden-bot/neqo c4e3b18158) with
#               outbound datagram backpressure, adapt the neqo_glue FFI
#
# Changed since the previous pin (W0DwkqlJTqGYtbKYGqr9iA): the bottom two
# commits are new. Datagrams that no longer fit are refused at the API instead
# of queued without bound, so an overloaded inner connection now applies
# backpressure rather than growing the queue.
#
# Also in the build: D304159 (Bug 2043768, "Honor http2/http3 prefs in Happy
# Eyeballs v3"), and IPProtectionServerlist gates the synthesized
# MASQUE-primary entry on network.http.http3.enable, so clearing that pref
# leaves the plain CONNECT entry and forces an HTTP/2 CONNECT hop (matrix
# config 3).
#
# Known orange on try, and expected: test_bug1948203.js and
# test_happy_eyeballs_altsvc_h3_only_claim.js fail because 1b21315eed drops
# Alt-Svc validation for every mapping, not only the proxy route. The builds
# and all lint jobs are green; the artifact is fine.
FIREFOX_CUSTOM_URL = (
    "https://firefox-ci-tc.services.mozilla.com/api/queue/v1/task/"
    "Oi_t-CFET_aPMpb1COlQmA/runs/0/artifacts/public/build/target.tar.xz"
)
# Origin whose h3 Alt-Svc we prime before a --vpn --h3 run (see prime_h3_altsvc).
H3_PRIME_URL = "https://bastion.h3.speed.cloudflare.com/cdn-cgi/trace"
GECKODRIVER_LATEST_API = (
    "https://api.github.com/repos/mozilla/geckodriver/releases/latest"
)

SPEEDTEST_HTML = (ROOT / "speedtest.html").read_bytes()


def http_version_short(v: str | None) -> str:
    """Map a verbose HTTP version label ('HTTP/3', 'http/1.1', 'HTTP <= 1.1',
    …) to the compact tag form used in result filenames ('h3', 'h2', 'h1').
    Returns '?' for unknown / missing values."""
    if not v:
        return "?"
    if "3" in v:
        return "h3"
    if "2" in v:
        return "h2"
    if "1" in v:
        return "h1"
    return "?"


def require_linux_x86_64() -> None:
    if platform.system() != "Linux" or platform.machine() != "x86_64":
        sys.exit(f"fireflare currently only supports Linux x86_64 "
                 f"(got {platform.system()} {platform.machine()})")


def download(url: str, dest: Path) -> None:
    print(f"  downloading {url}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(url) as resp, dest.open("wb") as out:
        shutil.copyfileobj(resp, out)


def ensure_firefox(url: str = FIREFOX_NIGHTLY_URL) -> Path:
    """Download a Firefox build if not cached. Return path to the binary.

    Default downloads the current Linux x86_64 Nightly. Pass a different URL
    (e.g. a Taskcluster try-build artifact) to use a custom build; the cache
    key is derived from the URL so swapping between builds doesn't redownload.
    """
    if url == FIREFOX_NIGHTLY_URL:
        install_dir = CACHE / "firefox"
        label = "Firefox Nightly"
    else:
        key = hashlib.sha1(url.encode()).hexdigest()[:12]
        install_dir = CACHE / f"firefox-{key}"
        label = f"Firefox build ({url})"
    binary = install_dir / "firefox"
    if binary.exists():
        return binary

    print(f"Fetching {label}...")
    req = urllib.request.Request(url, method="HEAD")
    with urllib.request.urlopen(req) as resp:
        final_url = resp.url
    suffix = ".tar.xz" if final_url.endswith(".tar.xz") else ".tar.bz2"
    archive = CACHE / f"{install_dir.name}{suffix}"
    download(final_url, archive)

    print(f"  extracting to {install_dir}")
    if install_dir.exists():
        shutil.rmtree(install_dir)
    CACHE.mkdir(parents=True, exist_ok=True)
    # The tarball contains a top-level `firefox/` dir. Extract into a staging
    # dir so we can install into an install_dir whose name doesn't match.
    staging = CACHE / f".staging-{install_dir.name}"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir()
    with tarfile.open(archive) as tf:
        tf.extractall(staging)
    (staging / "firefox").rename(install_dir)
    shutil.rmtree(staging)
    archive.unlink()

    if not binary.exists():
        sys.exit(f"expected {binary} after extracting Firefox build")
    return binary


def ensure_geckodriver() -> Path:
    """Download the latest geckodriver release if not cached."""
    binary = CACHE / "geckodriver"
    if binary.exists():
        return binary

    print("Fetching latest geckodriver release metadata...")
    with urllib.request.urlopen(GECKODRIVER_LATEST_API) as resp:
        release = json.load(resp)
    asset = next(
        (a for a in release["assets"] if a["name"].endswith("linux64.tar.gz")),
        None,
    )
    if asset is None:
        sys.exit("could not find a linux64 geckodriver asset in latest release")

    archive = CACHE / asset["name"]
    download(asset["browser_download_url"], archive)
    with tarfile.open(archive) as tf:
        tf.extractall(CACHE)
    archive.unlink()

    binary.chmod(0o755)
    return binary


def firefox_version(firefox: Path) -> str:
    out = subprocess.run(
        [str(firefox), "--version"], capture_output=True, text=True, check=True
    )
    return out.stdout.strip()


def scrub_profile_test_stubs() -> None:
    """Remove test-stub prefs from the profile before Firefox launches.

    Past sessions can leave `{server}` / `%(server)s` placeholders in
    prefs.js (FxA auth.uri, telemetry, addons blocklist, …). Firefox caches
    these at startup, so clearing at runtime is too late, FxA keeps
    POSTing to `https://{server}/dummy/fxa/oauth/token`. We rewrite
    prefs.js up front so the cached values are sane from the start.
    """
    prefs = PROFILE / "prefs.js"
    if not prefs.exists():
        return
    original = prefs.read_text().splitlines(keepends=True)
    kept = [
        line for line in original
        if "{server}" not in line and "%(server)s" not in line
    ]
    if len(kept) != len(original):
        prefs.write_text("".join(kept))
        print(f"Scrubbed {len(original) - len(kept)} stub pref(s) from prefs.js")


def build_driver(firefox: Path, geckodriver: Path,
                 extra_prefs: dict | None = None) -> webdriver.Firefox:
    options = Options()
    options.binary_location = str(firefox)
    # Headed so progress is visible during local development. Flip for CI.
    # Persist the Firefox profile under ./profile/ so state (prefs, caches,
    # any MASQUE config) carries across runs instead of being wiped with the
    # default throwaway profile.
    options.add_argument("-profile")
    options.add_argument(str(PROFILE))
    # Allow Marionette's chrome-context switch, needed to flip privileged
    # prefs at runtime (e.g. browser.ipProtection.userEnabled).
    options.add_argument("-remote-allow-system-access")
    # Disable Firefox's runtime-applied "recommended" WebDriver preferences.
    # Those stub real endpoints (e.g. identity.fxaccounts.auth.uri →
    # https://{server}/dummy/fxa) to isolate tests, which breaks anything
    # that actually needs to talk to Mozilla services, including IP
    # protection. See remote/shared/RecommendedPreferences.sys.mjs.
    options.set_preference("remote.prefs.recommended", False)
    # IP protection's channel filter excludes any request triggered from a
    # loopback origin, which is exactly what our local test page is. Add
    # an inclusion list so the Cloudflare speedtest endpoints are proxied
    # anyway.
    options.set_preference(
        "browser.ipProtection.inclusion.match_patterns",
        json.dumps([
            "*://speed.cloudflare.com/*",
            "*://bastion.h3.speed.cloudflare.com/*",
        ]),
    )
    # Caller-supplied prefs applied at launch (e.g. network.http.http3.enable),
    # so they take effect before any connection rather than mid-session.
    for key, value in (extra_prefs or {}).items():
        options.set_preference(key, value)
    # Clear LD_LIBRARY_PATH inherited from the parent shell: Firefox devs
    # often point it at a local ASAN build, which breaks the downloaded
    # Nightly's updater and makes Firefox exit 127 before Marionette comes up.
    service = Service(
        executable_path=str(geckodriver),
        log_output=str(CACHE / "geckodriver.log"),
        env={**os.environ, "LD_LIBRARY_PATH": ""},
    )
    return webdriver.Firefox(service=service, options=options)


class _PageHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(SPEEDTEST_HTML)

    def log_message(self, *_args):
        pass


def serve_page() -> tuple[ThreadingHTTPServer, str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _PageHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    return server, f"http://127.0.0.1:{port}/"


def set_ip_protection(driver: webdriver.Firefox, enabled: bool) -> None:
    """Turn the Firefox IP-protection (MASQUE VPN) proxy on or off at runtime.

    Flipping `browser.ipProtection.userEnabled` alone only changes the
    persisted UI toggle, the proxy doesn't actually start. We also call
    IPPProxyManager.start()/stop() the same way the panel UI does.
    Requires: profile already signed in + `browser.ipProtection.enabled=true`.
    """
    print(f"{'Starting' if enabled else 'Stopping'} IP protection proxy...")
    driver.set_script_timeout(60)
    with driver.context(driver.CONTEXT_CHROME):
        if enabled:
            result = driver.execute_async_script("""
                const done = arguments[arguments.length - 1];
                Services.prefs.setBoolPref('browser.ipProtection.userEnabled', true);
                const { IPPProxyManager } = ChromeUtils.importESModule(
                  'moz-src:///toolkit/components/ipprotection/IPPProxyManager.sys.mjs'
                );
                const failed = [];
                const observer = {
                  observe(subject) {
                    try {
                      const ch = subject.QueryInterface(Ci.nsIHttpChannel);
                      if (!Components.isSuccessCode(ch.status)) {
                        failed.push({
                          url: ch.URI.spec,
                          status: '0x' + (ch.status >>> 0).toString(16),
                        });
                      }
                    } catch (e) {}
                  },
                };
                Services.obs.addObserver(observer, 'http-on-stop-request');
                IPPProxyManager.start(true, false).then(
                  r => {
                    Services.obs.removeObserver(observer, 'http-on-stop-request');
                    // Record proxyInfo from the first proxied channel during
                    // the test so collect_proxy_info() knows which host/port
                    // to look up in the HTTP connection table. Stashed on the
                    // IPPProxyManager singleton because Marionette's chrome
                    // sandbox does not share globalThis across execute_script
                    // calls, but this singleton is shared.
                    IPPProxyManager.__fireflare_proxy = null;
                    const proxyObs = {
                      observe(subject) {
                        if (IPPProxyManager.__fireflare_proxy) return;
                        try {
                          const ch = subject.QueryInterface(Ci.nsIHttpChannel);
                          const pi = ch.QueryInterface(Ci.nsIProxiedChannel).proxyInfo;
                          if (!pi || pi.type === 'direct') return;
                          IPPProxyManager.__fireflare_proxy = {
                            type: pi.type, host: pi.host, port: pi.port,
                          };
                        } catch (e) {}
                      },
                    };
                    Services.obs.addObserver(proxyObs, 'http-on-stop-request');
                    IPPProxyManager.__fireflare_obs = proxyObs;
                    done({
                      result: r || {},
                      failed,
                      state: IPPProxyManager.state,
                      isActive: IPPProxyManager.isActive,
                    });
                  },
                  e => {
                    Services.obs.removeObserver(observer, 'http-on-stop-request');
                    done({
                      result: { error: String(e) },
                      failed,
                      state: IPPProxyManager.state,
                      isActive: IPPProxyManager.isActive,
                    });
                  }
                );
            """)
            if result["result"].get("error"):
                if result["failed"]:
                    print("Failed HTTP channels during VPN start:")
                    for f in result["failed"]:
                        print(f"  {f['status']:>12}  {f['url']}")
                sys.exit(f"VPN start failed: {result['result']['error']}")
            if result.get("state") != "active":
                sys.exit(
                    f"VPN start returned without error but proxy is not "
                    f"active (state={result.get('state')!r})"
                )
        else:
            driver.execute_async_script("""
                const done = arguments[arguments.length - 1];
                Services.prefs.setBoolPref('browser.ipProtection.userEnabled', false);
                const { IPPProxyManager } = ChromeUtils.importESModule(
                  'moz-src:///toolkit/components/ipprotection/IPPProxyManager.sys.mjs'
                );
                IPPProxyManager.stop().then(() => done(null));
            """)


def collect_proxy_info(driver: webdriver.Firefox) -> dict | None:
    """Return the proxyInfo from the first proxied channel plus the actual
    HTTP version negotiated on the connection to the proxy. None if no
    proxied channel was seen (e.g. VPN disabled).

    Firefox creates a wildcard `*:0` entry in its HTTP connection table for
    HTTPS-proxy h2 coalescing (see nsHttpConnectionInfo::CreateWildCard);
    that row's `httpVersion` is the ALPN-negotiated protocol on the
    browser↔proxy connection (HTTP/2 or `HTTP <= 1.1`).
    """
    driver.set_script_timeout(10)
    with driver.context(driver.CONTEXT_CHROME):
        return driver.execute_async_script("""
            const done = arguments[arguments.length - 1];
            const { IPPProxyManager } = ChromeUtils.importESModule(
              'moz-src:///toolkit/components/ipprotection/IPPProxyManager.sys.mjs'
            );
            const info = IPPProxyManager.__fireflare_proxy || null;
            const obs = IPPProxyManager.__fireflare_obs;
            if (obs) {
              try { Services.obs.removeObserver(obs, 'http-on-stop-request'); } catch (e) {}
              IPPProxyManager.__fireflare_obs = null;
            }
            IPPProxyManager.__fireflare_proxy = null;
            if (!info) { done(null); return; }
            const dashboard = Cc['@mozilla.org/network/dashboard;1']
              .getService(Ci.nsIDashboard);
            // This reports only the TRANSPORT to the proxy (Firefox to the
            // Fastly server), NOT how the proxy reaches the origin. An h3
            // transport does not imply connect-udp to the origin: Firefox can
            // (and currently does) tunnel the origin via classic CONNECT over
            // an h3 proxy connection, leaving the origin on h2. The origin's
            // own HTTP version (result.trace.http) is the signal for that.
            // - h3 transport: the QUIC connection to the proxy never gets a
            //   wildcard *:0 row (CreateWildCard is HTTPS/h2 only). Detect it
            //   via an active UDP socket on the proxy port; QUIC implies h3.
            // - h2 / h1 transport: the connection manager creates a wildcard
            //   `*:0` row for h2 coalescing; its httpVersion is the ALPN
            //   negotiated with the proxy (HTTP/2 or HTTP <= 1.1).
            if (info.type === 'masque') {
              dashboard.requestSockets(sockData => {
                let httpVersion = null;
                for (const s of (sockData.sockets || [])) {
                  if (s.port === info.port && s.active && s.type === 'UDP') {
                    httpVersion = 'HTTP/3';
                    break;
                  }
                }
                done({ ...info, httpVersion });
              });
            } else {
              dashboard.requestHttpConnections(httpData => {
                let httpVersion = null;
                for (const c of httpData.connections) {
                  if (c.host === '*' && c.port === 0) {
                    httpVersion = c.httpVersion;
                    break;
                  }
                }
                done({ ...info, httpVersion });
              });
            }
        """)


def disable_http_cache(driver: webdriver.Firefox) -> None:
    """Disable the disk+memory HTTP cache so every request is a live network
    response. Without this the persistent profile serves/revalidates responses
    from cache, which skips the live Alt-Svc processing path and pins requests
    to the cached (h2) connection, masking the h3 (connect-udp) inner route.
    """
    with driver.context(driver.CONTEXT_CHROME):
        driver.execute_script("""
            Services.prefs.setBoolPref('browser.cache.disk.enable', false);
            Services.prefs.setBoolPref('browser.cache.memory.enable', false);
        """)


def prime_h3_altsvc(driver: webdriver.Firefox) -> None:
    """Make the speedtest run over h3 to the origin through the MASQUE proxy.

    There is no HTTPS RR (DNS is resolved through the proxy), so the only h3
    signal is Alt-Svc on a reconnect. A single page load would open one h2
    CONNECT tunnel, learn Alt-Svc, then reuse that h2 connection for the whole
    test. So we first prime: fetch the origin once (h2) to store its h3 Alt-Svc
    mapping, then drop all connections and clear the h3-excluded list so the
    speedtest's own requests open fresh connections that take the h3
    (connect-udp) route from the now-stored mapping.
    """
    print("Priming h3 Alt-Svc for the origin...")
    driver.get(H3_PRIME_URL)
    time.sleep(2)
    with driver.context(driver.CONTEXT_CHROME):
        driver.execute_script("""
            Services.obs.notifyObservers(null, 'net:cancel-all-connections');
            Services.obs.notifyObservers(null, 'network:reset-http3-excluded-list');
        """)
    time.sleep(2)


# Firefox Profiler "Networking" preset, from the devtools definition in
# devtools/shared/performance-new/prefs-presets.sys.mjs (networking). The
# upstream preset also lists "java" (Android Java-stack sampling); dropped here
# since it is a no-op on desktop Linux.
PROFILER_ENTRIES = 128 * 1024 * 1024
PROFILER_INTERVAL = 1
PROFILER_FEATURES = ["screenshots", "js", "stackwalk", "cpu", "processcpu",
                     "bandwidth", "memory"]
PROFILER_THREADS = ["Cache2 I/O", "Compositor", "DNS Resolver", "DOM Worker",
                    "GeckoMain", "Renderer", "Socket Thread", "StreamTrans",
                    "SwComposite", "TRR Background"]


def start_profiler(driver: webdriver.Firefox) -> None:
    """Start the Gecko profiler with the Firefox Profiler 'Networking' preset."""
    print("Starting profiler (Networking preset)...")
    with driver.context(driver.CONTEXT_CHROME):
        driver.execute_script(
            "const [entries, interval, features, threads] = arguments;"
            "Services.profiler.StartProfiler(entries, interval, features, threads);",
            PROFILER_ENTRIES, PROFILER_INTERVAL, PROFILER_FEATURES, PROFILER_THREADS,
        )


def capture_profiler(driver: webdriver.Firefox, path: Path) -> None:
    """Dump the collected profile to `path` (load it at profiler.firefox.com),
    then stop the profiler. The target directory must already exist."""
    driver.set_script_timeout(180)
    with driver.context(driver.CONTEXT_CHROME):
        err = driver.execute_async_script(
            "const filename = arguments[0];"
            "const done = arguments[arguments.length - 1];"
            "Services.profiler.dumpProfileToFileAsync(filename).then("
            "  () => { Services.profiler.StopProfiler(); done(null); },"
            "  e => done(String(e)));",
            str(path),
        )
    if err:
        raise RuntimeError(err)


def collect_results(driver: webdriver.Firefox, url: str, timeout_s: int = 300) -> dict:
    driver.get(url)
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        err = driver.execute_script("return window.__fireflare_error || null;")
        if err:
            sys.exit(f"speed test error: {err}")
        result = driver.execute_script("return window.__fireflare_result || null;")
        if result is not None:
            return result
        time.sleep(1)
    sys.exit(f"speed test did not complete within {timeout_s}s")


def save_result(result: dict, *, debug: bool, label: str | None = None) -> Path:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    parts = []
    if label:
        parts.append(label)
    if debug:
        parts.append("debug")
    proxy = result.get("proxy")
    if proxy:
        parts.append(f"proxy-{http_version_short(proxy.get('httpVersion'))}")
    parts.append(f"origin-{http_version_short((result.get('trace') or {}).get('http'))}")
    parts.append(ts)
    out_path = RESULTS / ("-".join(parts) + ".json")
    out_path.write_text(json.dumps(result, indent=2))
    print(f"Saved {out_path.relative_to(ROOT)}")
    return out_path


def run_once(firefox: Path, geckodriver: Path, base_url: str, *, vpn: bool,
             h3: bool, debug: bool, disable_h3: bool = False,
             label: str | None = None, profile: bool = False) -> dict:
    """Run a single speed test in its own Firefox session and save the result.
    Returns the result dict (with the negotiated proxy hop + origin protocol,
    so callers can confirm what actually happened at runtime)."""
    qs = []
    if h3:
        qs.append("h3=1")
    if debug:
        qs.append("debug=1")
    url = base_url + (f"?{'&'.join(qs)}" if qs else "")
    scrub_profile_test_stubs()
    # Set the h3 pref at launch (before any connection) so the proxy comes up on
    # the intended transport: h2 CONNECT when h3 is disabled, h3/connect-udp when
    # enabled. Set explicitly every run so a value persisted by a prior
    # disable_h3 run can't leak in.
    driver = build_driver(firefox, geckodriver,
                          extra_prefs={"network.http.http3.enable": not disable_h3})
    prof_tmp = None
    try:
        print(f"Serving {url}")
        # Always flip the VPN toggle to the requested state, the persisted
        # profile may have left it on from a previous run.
        set_ip_protection(driver, vpn)
        if vpn:
            disable_http_cache(driver)
        # For h3 over the VPN we must prime the origin's Alt-Svc and force fresh
        # connections, otherwise the speedtest reuses the initial h2 tunnel.
        if vpn and h3:
            prime_h3_altsvc(driver)
        if profile:
            start_profiler(driver)
        print("Running speed test...")
        result = collect_results(driver, url)
        if vpn:
            result["proxy"] = collect_proxy_info(driver)
        if profile:
            prof_tmp = CACHE / "fireflare-profile.json"
            try:
                capture_profiler(driver, prof_tmp)
            except Exception as e:
                print(f"profiler capture failed: {e}")
                prof_tmp = None
    finally:
        driver.quit()
    if label:
        result["config"] = label
    out_path = save_result(result, debug=debug, label=label)
    if prof_tmp and prof_tmp.exists():
        prof_path = out_path.with_name(out_path.stem + ".profile.json")
        shutil.move(str(prof_tmp), str(prof_path))
        print(f"Saved {prof_path.relative_to(ROOT)}")
    return result


# The comparison matrix. All configs run on the custom build (FIREFOX_CUSTOM_URL):
# the proxy transport follows the inner automatically, a TCP inner (normal
# endpoint) tunnels via h3 CONNECT, a QUIC inner (h3 endpoint, primed) via h3
# connect-udp/MASQUE. Protocols are confirmed from each result at runtime.
MATRIX = [
    # label, run_once kwargs, skip_reason
    ("1-direct-h1h2",    dict(vpn=False, h3=False), None),
    ("2-direct-h3",      dict(vpn=False, h3=True),  None),
    ("3-proxy-h2connect", dict(vpn=True, h3=False, disable_h3=True), None),
    ("4-proxy-h3connect", dict(vpn=True, h3=False), None),
    ("5-proxy-h3masque",  dict(vpn=True, h3=True),  None),
]


def run_matrix(firefox: Path, geckodriver: Path, *, debug: bool,
               profile: bool = False) -> None:
    server, base_url = serve_page()
    summary = []
    try:
        for label, kwargs, skip_reason in MATRIX:
            print(f"\n===== matrix: {label} =====")
            if skip_reason:
                print(f"SKIP {label}: {skip_reason}")
                summary.append((label, "skipped", skip_reason))
                continue
            # run_once -> set_ip_protection/collect_results sys.exit on a failed
            # run; VPN start in particular hits a transient "pass-unavailable"
            # when toggled rapidly. Retry a few times, and never let one config
            # abort the rest of the matrix.
            result = None
            for attempt in range(1, 4):
                try:
                    result = run_once(firefox, geckodriver, base_url, debug=debug,
                                      label=label, profile=profile, **kwargs)
                    break
                except SystemExit as e:
                    print(f"FAILED {label} (attempt {attempt}/3): {e}")
                    if attempt < 3:
                        time.sleep(10)
            if result is None:
                summary.append((label, "FAILED", "all attempts failed"))
                continue
            proxy = (result.get("proxy") or {}).get("httpVersion") or "-"
            origin = (result.get("trace") or {}).get("http")
            down = result.get("downloadBandwidth") or 0
            up = result.get("uploadBandwidth") or 0
            summary.append((label, f"proxy={proxy} origin={origin}",
                            f"down={down / 1e6:.1f}Mbps up={up / 1e6:.1f}Mbps"))
    finally:
        server.shutdown()
    print("\n===== matrix summary (confirmed at runtime) =====")
    for row in summary:
        print("  " + "  |  ".join(str(c) for c in row))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--h3", action="store_true",
        help="route the test through h3.speed.cloudflare.com (forces HTTP/3)",
    )
    parser.add_argument(
        "--vpn", action="store_true",
        help="enable Firefox's IP protection (MASQUE proxy) before measuring "
             "(requires the profile to already be signed in)",
    )
    parser.add_argument(
        "--debug", action="store_true",
        help="run a minimal speed test (1 of each measurement) to make "
             "end-to-end iteration fast; numbers are not meaningful",
    )
    parser.add_argument(
        "--custom-firefox", action="store_true",
        help="use the hardcoded custom build (see FIREFOX_CUSTOM_URL) "
             "instead of the latest Nightly",
    )
    parser.add_argument(
        "--matrix", action="store_true",
        help="run the full comparison matrix (direct h1/h2, direct h3, VPN "
             "h2-CONNECT, VPN h3-CONNECT, VPN h3-MASQUE) on the custom build, "
             "one result per config",
    )
    parser.add_argument(
        "--profile", action="store_true",
        help="capture a Firefox Profiler profile (Networking preset) during "
             "each run, saved next to the result as <name>.profile.json "
             "(load at profiler.firefox.com)",
    )
    args = parser.parse_args()

    require_linux_x86_64()
    CACHE.mkdir(exist_ok=True)
    RESULTS.mkdir(exist_ok=True)
    PROFILE.mkdir(exist_ok=True)

    # The matrix always uses the custom build (configs 4/5 need it).
    use_custom = args.custom_firefox or args.matrix
    firefox = ensure_firefox(FIREFOX_CUSTOM_URL if use_custom else FIREFOX_NIGHTLY_URL)
    geckodriver = ensure_geckodriver()
    print(f"Using {firefox_version(firefox)}")

    if args.matrix:
        run_matrix(firefox, geckodriver, debug=args.debug, profile=args.profile)
        return

    server, base_url = serve_page()
    try:
        run_once(firefox, geckodriver, base_url, vpn=args.vpn, h3=args.h3,
                 debug=args.debug, profile=args.profile)
    finally:
        server.shutdown()


if __name__ == "__main__":
    main()
