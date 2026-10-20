"""
Adobe Podcast enhancement automation via SeleniumBase.

Key HTML facts:
  Upload input : <input type="file" hidden="" id="enhance-file-upload" ...>
  Upsell modal : <div data-testid="upsell-dialog-close-button" ...>
  Processing   : <div data-testid="enhance-speech-track-processing-animated-text">
  Spinner      : <sp-progress-circle indeterminate ...>
  Download btn : <button aria-label="Download" data-testid="download-button" ...>
"""

import time
import threading
from pathlib import Path
from typing import Optional, Callable, List, Tuple

from core.config import (
    ADOBE_ENHANCE_URL,
    ADOBE_UPLOAD_SELECTORS,
    ADOBE_DOWNLOAD_SELECTORS,
    INITIAL_WAIT_SECONDS,
    SCAN_INTERVAL_SECONDS,
    TIMEOUT_SECONDS,
    UPLOAD_RETRY_COUNT,
    APP_DIR,
)
from core.logger import get_logger

log = get_logger(__name__)

# How long to wait for a downloaded file (large files can take many minutes)
DOWNLOAD_WAIT_TIMEOUT = 900   # 15 min

# ── URL patterns that indicate an audio download URL ──────────────────────────
# Adobe Podcast stores enhanced audio on AWS S3 with presigned GET URLs.
# These URLs include ``response-content-disposition=attachment`` (or
# ``response-content-type=audio``) query parameters that signal a download.
#
# Common false positives we explicitly REJECT:
#   * S3 PUT presigned URLs (upload) — they have only X-Amz-*, no content-disposition
#   * Adobe API endpoints (analytics, status, user, direct_uploads) — different host
#   * Adobe personalization / Behance APIs — 403 on GET

# Hosts we treat as S3 (download source)
_S3_HOST_MARKERS = (
    "s3.amazonaws.com",         # virtual-hosted–style & path-style
    ".s3.",                     # regional S3 (e.g. .s3.us-east-1.amazonaws.com)
    "phonos-recordings",        # Adobe's specific S3 bucket subdomain
)

# Query markers that signal a browser file download
_DOWNLOAD_MARKERS = (
    "response-content-disposition=attachment",   # S3 GET presigned for download
    "response-content-type=audio",               # explicit audio content-type
)

# All presigned-URL markers (X-Amz-* is present in BOTH upload & download)
_PRESIGN_MARKERS = (
    "x-amz-signature",
    "x-amz-credential",
)

# Hosts / paths to NEVER consider as a download (403 traps)
_API_BLOCKLIST = (
    "phonos-server-flex.adobe.io",   # API server
    "p13n.adobe.io",                 # personalization
    "cc-api-behance.adobe.io",       # Behance integration
    "/api/v1/analytics",             # analytics events
    "/api/v1/user",                  # user info
    "/api/v1/cms/",                  # CMS guide pages
    "/rails/active_storage/direct_uploads",  # S3 upload presigner
)

# Common audio file extensions (URL path may or may not have them)
_AUDIO_FILE_EXTS = (".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".oga")

# Asset extensions to reject (even if the URL has S3 markers)
_EXCLUDE_URL_HINTS = (".js", ".css", ".png", ".jpg", ".jpeg", ".svg",
                     ".woff", ".woff2", ".ico", ".map", ".chunk",
                     ".json", ".xml", ".txt", ".html")

# ── JavaScript snippets ───────────────────────────────────────────────────────

_JS_UNHIDE = """
(function(el) {
    el.removeAttribute('hidden');
    el.removeAttribute('disabled');
    el.style.display      = 'block';
    el.style.visibility   = 'visible';
    el.style.opacity      = '1';
    el.style.position     = 'fixed';
    el.style.top          = '0';
    el.style.left         = '0';
    el.style.width        = '1px';
    el.style.height       = '1px';
    el.style.zIndex       = '99999';
    el.style.pointerEvents = 'all';
})(arguments[0]);
"""

_JS_TRIGGER_CHANGE = """
(function(el) {
    el.dispatchEvent(new Event('change', {bubbles: true, composed: true}));
    el.dispatchEvent(new Event('input',  {bubbles: true, composed: true}));
})(arguments[0]);
"""

_JS_PAGE_STATE = """
(function() {
    var dlSels = [
        'button[data-testid="download-button"]',
        'button[aria-label="Download"]',
        'button.sc-hKKfEi',
        '.sc-hKKfEi[role="button"]'
    ];
    for (var i = 0; i < dlSels.length; i++) {
        var el = document.querySelector(dlSels[i]);
        if (el && el.offsetParent !== null) return 'download_ready';
    }

    var spinner = document.querySelector('sp-progress-circle[indeterminate], .spinner, [class*="spinner"]');
    if (spinner) return 'spinner';

    var procText = document.querySelector(
        'div[data-testid="enhance-speech-track-processing-animated-text"], [data-testid*="processing"], [data-testid*="enhance"]'
    );
    if (procText) return 'processing_text';

    var bodyText = document.body ? document.body.innerText : '';
    if (bodyText.includes('Preparing') || bodyText.includes('Enhancing') || bodyText.includes('Processing') || bodyText.includes('Uploading')) {
        return 'processing_text';
    }

    return 'unknown';
})();
"""

_JS_DISMISS_UPSELL = """
(function() {
    // ── Tier 1: known Adobe/Spectrum close buttons ───────────────────
    var sels = [
        'div[data-testid="upsell-dialog-close-button"]',
        'button[data-testid="upsell-dialog-close-button"]',
        '[aria-label="Dismiss modal"]',
        '[aria-label="Close"]',
        '[aria-label="Close dialog"]',
        '[aria-label="Close popup"]',
        '[data-testid="modal-close-button"]',
        'button[data-testid*="close" i]',
        'button[aria-label*="close" i]',
        'button[aria-label*="dismiss" i]',
        '.spectrum-Underlay',
        '.spectrum-Modal-wrapper',
        '.spectrum-CSSIcon-Close',
        'sp-icon-close',
        '.close-button',
        'button.close',
        'a.close'
    ];
    for (var i = 0; i < sels.length; i++) {
        var els = document.querySelectorAll(sels[i]);
        for (var j = 0; j < els.length; j++) {
            var el = els[j];
            if (el && el.offsetParent !== null) {
                try { el.click(); return sels[i]; } catch(e) {}
            }
        }
    }

    // ── Tier 2: any visible modal/dialog — find its close button ────
    var dialogs = document.querySelectorAll('[role="dialog"], [aria-modal="true"]');
    for (var k = 0; k < dialogs.length; k++) {
        var d = dialogs[k];
        if (d.offsetParent === null) continue;       // hidden
        var rect = d.getBoundingClientRect();
        if (rect.width < 10 || rect.height < 10) continue;
        // Find close button within the dialog
        var closeBtn = d.querySelector(
            'button[aria-label*="close" i], button[aria-label*="dismiss" i], ' +
            '[data-testid*="close" i], .close, button.close, ' +
            'svg.spectrum-icon-Cross, .spectrum-CSSIcon-Close'
        );
        if (closeBtn && closeBtn.offsetParent !== null) {
            try { closeBtn.click(); return "dialog_close:" + (closeBtn.getAttribute('aria-label') || '?'); } catch(e) {}
        }
        // No close button — try ESC key on the dialog
        try {
            var evt = new KeyboardEvent('keydown', {key: 'Escape', keyCode: 27, which: 27, bubbles: true, cancelable: true});
            d.dispatchEvent(evt);
            document.dispatchEvent(evt);
            return "escape_key";
        } catch(e) {}
    }

    // ── Tier 3: click the underlay/backdrop ──────────────────────────
    var overlays = document.querySelectorAll('[class*="Underlay"], [class*="Overlay"], [class*="backdrop"]');
    for (var m = 0; m < overlays.length; m++) {
        var o = overlays[m];
        if (o.offsetParent === null) continue;
        var r2 = o.getBoundingClientRect();
        if (r2.width < 100 || r2.height < 100) continue;
        try { o.click(); return "overlay:" + o.className.split(' ')[0]; } catch(e) {}
    }

    return null;
})();
"""


# ── Sentinel for early-exit-and-download pattern ─────────────────────────────

class _EarlyDownloadSignal(Exception):
    """
    Raised inside the `with _make_sb(...)` block to trigger browser close
    BEFORE starting the direct download.  The browser's __exit__ runs
    (Chrome closes), then _process_once catches this exception and
    calls _download_url_direct with full network bandwidth.
    """
    def __init__(self, url: str, dest_path: Path):
        self.url       = url
        self.dest_path = dest_path
        super().__init__(url)


# ── SB factory ────────────────────────────────────────────────────────────────

def _make_sb(session_dir: str, headless: bool = False):
    from seleniumbase import SB
    return SB(browser="chrome", user_data_dir=session_dir, headless=headless, log_cdp=True)


def _set_download_dir(sb, download_dir: Path):
    """
    Force Chrome to download to download_dir.
    Tries both Browser.setDownloadBehavior (Chrome 109+) and
    Page.setDownloadBehavior (older) via CDP.
    """
    download_dir.mkdir(parents=True, exist_ok=True)
    path_str = str(download_dir.resolve())

    for cmd in ["Browser.setDownloadBehavior", "Page.setDownloadBehavior"]:
        try:
            sb.driver.execute_cdp_cmd(
                cmd,
                {"behavior": "allow", "downloadPath": path_str},
            )
            log.info("CDP %s → %s", cmd, path_str)
            return
        except Exception as e:
            log.debug("CDP %s failed: %s", cmd, e)

    log.warning("Both CDP download commands failed — will scan fallback dirs")


# ── Popup watcher (background thread) ────────────────────────────────────────

class _PopupWatcher:
    """
    Continuously dismisses Adobe upsell/modal popups in a background thread.

    NOTE: This is the most common cause of "UI glitches" — when the watcher
    is too aggressive, it dismisses dialogs the user might be trying to
    interact with. We therefore:
      - Run at a slower cadence (every 4 s instead of every 2 s)
      - Skip the dismiss if a download button is currently visible
        (so the user can manually click it)
      - Auto-pause for 6 s after each dismiss to let the page settle
    """

    def __init__(self, sb, emit: Callable[[str], None]):
        self._sb    = sb
        self._emit  = emit
        self._stop  = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        # Cadence + cooldowns
        self.TICK_SECONDS   = 4     # was 2 — too aggressive
        self.COOLDOWN_AFTER = 6     # seconds to sleep after a successful dismiss

    def start(self):
        self._stop.clear()
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()

    def _is_download_button_visible(self) -> bool:
        """If a download button is on the page, do not touch it."""
        try:
            for sel in ADOBE_DOWNLOAD_SELECTORS:
                res = self._sb.execute_script(
                    f"const el = document.querySelector({repr(sel)}); "
                    f"return el && el.offsetParent !== null;"
                )
                if res:
                    return True
        except Exception:
            pass
        return False

    def _run(self):
        last_dismiss_at = 0.0
        while not self._stop.is_set():
            try:
                # Don't disturb the user when the download is ready
                if self._is_download_button_visible():
                    self._stop.wait(timeout=self.TICK_SECONDS)
                    continue

                now = time.time()
                # Respect cooldown so the page can settle after each dismiss
                if now - last_dismiss_at < self.COOLDOWN_AFTER:
                    self._stop.wait(
                        timeout=min(self.TICK_SECONDS, self.COOLDOWN_AFTER - (now - last_dismiss_at))
                    )
                    continue

                matched = self._sb.execute_script(_JS_DISMISS_UPSELL)
                if matched:
                    log.info("PopupWatcher dismissed: %s", matched)
                    self._emit(f"Modal dismissed ✓ ({matched})")
                    last_dismiss_at = time.time()
            except Exception:
                # Browser may be busy or closing — silently back off
                self._stop.wait(timeout=self.TICK_SECONDS)
                continue
            self._stop.wait(timeout=self.TICK_SECONDS)


# ── Public: open browser for login / re-login ─────────────────────────────────

def open_profile_browser(
    profile_name: str,
    session_dir: str,
    url: str = ADOBE_ENHANCE_URL,
    on_done: Optional[Callable] = None,
):
    """Open Chrome for a manual login session. Blocks until user closes browser."""
    log.info("Launching browser for profile '%s'", profile_name)
    try:
        with _make_sb(session_dir) as sb:
            sb.open(url)
            log.info("[%s] Browser open — log in then close.", profile_name)
            while True:
                time.sleep(2)
                try:
                    _ = sb.get_current_url()
                except Exception:
                    break
    except Exception as e:
        log.error("[%s] Browser session error: %s", profile_name, e)
    finally:
        log.info("[%s] Browser closed.", profile_name)
        if on_done:
            on_done()


# ── Page helpers ──────────────────────────────────────────────────────────────

def _get_page_state(sb) -> str:
    try:
        return sb.execute_script(_JS_PAGE_STATE) or "unknown"
    except Exception:
        return "unknown"


def _force_dismiss_popups(sb, max_attempts: int = 3) -> bool:
    """
    Synchronously dismiss any visible popup/modal before a scan.
    Runs the JS dismisser up to ``max_attempts`` times.

    This is the KEY fix for the "popup blocks scan" issue — the background
    PopupWatcher can be slow or paused (e.g. it skips when a download button
    is visible), so we call this RIGHT BEFORE scanning for the button or URLs.
    """
    if sb is None:
        return False
    dismissed = False
    try:
        for attempt in range(1, max_attempts + 1):
            try:
                result = sb.execute_script(_JS_DISMISS_UPSELL)
            except Exception as e:
                log.debug("Force-dismiss JS error: %s", e)
                break
            if result:
                log.info("Force-dismissed popup: %s (attempt %d)", result, attempt)
                dismissed = True
                time.sleep(0.5)  # let page settle
            else:
                break  # nothing to dismiss
    except Exception as e:
        log.debug("Force-dismiss wrapper error: %s", e)
    return dismissed


def _has_visible_modal(sb) -> bool:
    """Quick check: is there a modal/popup currently blocking the page?"""
    if sb is None:
        return False
    try:
        return bool(sb.execute_script("""
            (function() {
                var d = document.querySelector('[role="dialog"], [aria-modal="true"]');
                if (d && d.offsetParent !== null) {
                    var r = d.getBoundingClientRect();
                    if (r.width >= 50 && r.height >= 50) return true;
                }
                var u = document.querySelector('.spectrum-Underlay, [class*="Underlay"][class*="open" i]');
                if (u && u.offsetParent !== null) {
                    var r2 = u.getBoundingClientRect();
                    if (r2.width >= 100 && r2.height >= 100) return true;
                }
                return false;
            })();
        """))
    except Exception:
        return False


def _find_upload_input_js(sb, selectors: List[str]) -> Tuple:
    """Find hidden file input via JS querySelector (bypasses visibility checks)."""
    for sel in selectors:
        try:
            el = sb.execute_script(f"return document.querySelector({repr(sel)});")
            if el is not None:
                log.debug("JS querySelector matched: %s", sel)
                return el, sel
        except Exception as e:
            log.debug("JS querySelector failed (%s): %s", sel, e)

    # Last resort: any file input on the page
    try:
        els = sb.execute_script(
            'return Array.from(document.querySelectorAll(\'input[type="file"]\'));'
        )
        if els:
            log.info("querySelectorAll fallback: %d file input(s) found", len(els))
            return els[0], 'input[type="file"] [querySelectorAll]'
    except Exception as e:
        log.debug("querySelectorAll fallback failed: %s", e)

    raise RuntimeError(
        f"No file input found in DOM via JavaScript.\nTried: {selectors}"
    )


def _upload_file(sb, file_path: Path) -> bool:
    """Send file to hidden Adobe input via JS querySelector + send_keys."""
    log.info("Attempting file upload: %s", file_path.name)
    time.sleep(2)

    el, matched_sel = _find_upload_input_js(sb, ADOBE_UPLOAD_SELECTORS)
    log.info("File input found via JS: %s", matched_sel)

    try:
        sb.execute_script(_JS_UNHIDE, el)
        log.debug("Input unhidden")
    except Exception as e:
        log.warning("JS unhide failed (continuing): %s", e)

    time.sleep(0.5)
    abs_path = str(file_path.resolve())

    try:
        el.send_keys(abs_path)
        log.info("send_keys() OK: %s", abs_path)
    except Exception as e:
        log.warning("Primary send_keys failed (%s), trying driver fallback", e)
        try:
            from selenium.webdriver.common.by import By
            driver_el = sb.driver.find_element(By.CSS_SELECTOR, "input#enhance-file-upload")
            sb.execute_script(_JS_UNHIDE, driver_el)
            driver_el.send_keys(abs_path)
            el = driver_el
            log.info("Driver fallback send_keys OK")
        except Exception as e2:
            raise RuntimeError(f"All send_keys attempts failed: {e} / {e2}")

    try:
        sb.execute_script(_JS_TRIGGER_CHANGE, el)
    except Exception:
        pass

    time.sleep(1.5)
    try:
        count = sb.execute_script(
            "return arguments[0].files ? arguments[0].files.length : -1;", el
        )
        if isinstance(count, int) and count > 0:
            log.info("Confirmed: input.files.length = %d", count)
        else:
            log.warning("input.files.length = %s (SPA may handle async)", count)
    except Exception:
        pass

    return True


def _find_download_button(sb):
    for sel in ADOBE_DOWNLOAD_SELECTORS:
        try:
            el = sb.find_element(sel, timeout=3)
            if el and el.is_displayed():
                return el
        except Exception:
            continue
    return None


def _extract_download_urls_from_logs(sb) -> List[str]:
    """
    Scan Chrome performance logs for any potential audio download URLs.

    Adobe Podcast stores enhanced audio on AWS S3 with presigned GET URLs.
    These don't always include the file extension in the path (e.g. ``/premixed``
    in the user's example) — the file type is conveyed by the
    ``response-content-type=audio/x-wav`` query parameter and the
    ``response-content-disposition=attachment; filename="chunk_001-enhanced-v2.wav"``
    query parameter.

    Returns URLs sorted by likelihood (highest priority first).
    """
    import json
    urls: list[str] = []
    try:
        logs = sb.driver.get_log("performance")
    except Exception as e:
        log.debug("Failed to read performance logs: %s", e)
        return urls

    for entry in logs:
        try:
            log_message = json.loads(entry["message"])["message"]
            method = log_message.get("method")
            params = log_message.get("params", {})

            url = None
            if method == "Network.requestWillBeSent":
                url = params.get("request", {}).get("url")
            elif method == "Network.responseReceived":
                url = params.get("response", {}).get("url")

            if not url or not isinstance(url, str):
                continue

            if _looks_like_audio_url(url):
                if url not in urls:
                    urls.append(url)
                    log.debug("Network log matched audio URL: %s", url[:160])
        except Exception:
            continue

    # Sort by priority (highest first) so the most-likely URL is tried first
    urls.sort(key=_priority_audio_url, reverse=True)
    if urls:
        log.debug("Sorted audio URL candidates: %d (top score=%d)",
                  len(urls), _priority_audio_url(urls[0]))
    return urls


def _looks_like_audio_url(url: str) -> bool:
    """
    Strict check: does this URL look like a direct audio file download?

    True positives (the S3 GET presigned URL for the enhanced audio):
        https://phonos-recordings-production.s3.amazonaws.com/QuickActionJobs/{UUID}/premixed
            ?response-content-disposition=attachment
            &response-content-type=audio%2Fx-wav
            &X-Amz-Algorithm=AWS4-HMAC-SHA256
            &X-Amz-Expires=...
            &X-Amz-Signature=...

    False positives (now REJECTED — these wasted 3 retries × 30+ URLs in testing):
        - https://phonos-server-flex.adobe.io/api/v1/analytics_event      (API 403)
        - https://phonos-server-flex.adobe.io/api/v1/user                 (API 403)
        - https://phonos-server-flex.adobe.io/api/v1/cms/guide-pages      (API 403)
        - https://phonos-server-flex.adobe.io/rails/active_storage/...     (S3 PUT presigner)
        - https://p13n.adobe.io/fg/api/v3/feature                         (personalisation)
        - https://phonos-recordings-production.s3.amazonaws.com/QuickActionJobs/{UUID}?X-Amz-...
                                          ^ this is the UPLOAD URL (no content-disposition)
    """
    if not url or not isinstance(url, str):
        return False
    url_l = url.lower()

    # ── (1) Reject known asset extensions (no audio) ──────────────────
    #   (these are CDN/static assets, never audio)
    if any(h in url_l for h in _EXCLUDE_URL_HINTS):
        if not any(m in url_l for m in _DOWNLOAD_MARKERS):
            return False

    # ── (2) Reject known non-audio Adobe endpoints ────────────────────
    if any(p in url_l for p in _API_BLOCKLIST):
        return False

    # ── (3) S3 host pattern required for "cloud download" ────────────
    is_s3_host = any(h in url_l for h in _S3_HOST_MARKERS)

    # ── (4) S3 + download-marker in query string = audio download ─────
    #   This is the canonical pattern: S3 GET presigned with
    #   ``response-content-disposition=attachment`` (forces browser to save).
    if is_s3_host and any(m in url_l for m in _DOWNLOAD_MARKERS):
        return True

    # ── (5) S3 + presigned-URL marker + path keyword = audio download ─
    #   (fallback in case content-disposition is URL-encoded differently)
    if is_s3_host and any(m in url_l for m in _PRESIGN_MARKERS):
        if any(p in url_l for p in ("/premixed", "/enhanced", "/enhanced-v2",
                                    "/audio", "/output", "/download")):
            return True

    # ── (6) Explicit audio file extension in URL path ─────────────────
    #   (works for non-S3 hosts too — e.g. a CDN that hosts the file)
    if any(url_l.endswith(ext) or f"{ext}?" in url_l or f"{ext}&" in url_l
           for ext in _AUDIO_FILE_EXTS):
        return True

    return False


def _priority_audio_url(url: str) -> int:
    """
    Higher = more likely the actual enhanced-audio download URL.
    Used to sort candidates so the best one is tried first.
    """
    url_l = url.lower()
    score = 0
    # /premixed is the canonical enhanced-audio path
    if "/premixed" in url_l:
        score += 100
    if "/enhanced" in url_l:
        score += 50
    if "response-content-disposition=attachment" in url_l:
        score += 30
    if "response-content-type=audio" in url_l:
        score += 20
    if "x-amz-signature" in url_l:
        score += 10
    # Penalise upload-only patterns
    if "/direct_uploads" in url_l:
        score -= 200
    return score


def _download_url_direct(
    url: str,
    dest_path: Path,
    min_size_bytes: int = 100 * 1024,   # 100 KB minimum (audio is usually >1MB)
    max_size_bytes: int = 500 * 1024 * 1024,  # 500 MB hard cap
    retries: int = 3,
) -> bool:
    """
    Download a URL directly using the `requests` library.

    Used as a fallback when:
      - Chrome fails to download via the download button
      - The download button is blocked by a modal / popup
      - The page is glitched and the user can't click

    Validates:
      - Response is not HTML (error page)
      - Content-Type starts with "audio" or matches audio extensions
      - File size is within sane bounds

    Returns True on success.
    """
    try:
        import requests
    except ImportError:
        log.error("`requests` library not installed. Run: pip install requests")
        return False

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/120.0.0.0 Safari/537.36",
        "Accept": "audio/*,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }

    last_err: Optional[Exception] = None
    for attempt in range(1, retries + 1):
        try:
            log.info(
                "Direct download attempt %d/%d: %s",
                attempt, retries, url[:160] + ("..." if len(url) > 160 else ""),
            )

            with requests.get(
                url, headers=headers, stream=True, timeout=(15, 300)
            ) as resp:
                # ── Status check ────────────────────────────────────────
                # 401/403/404 are non-retryable: the URL is wrong, expired, or
                # auth-required. Retrying just wastes time.
                if resp.status_code in (401, 403, 404, 410):
                    log.warning(
                        "HTTP %d for %s — non-retryable, skipping",
                        resp.status_code, url[:100]
                    )
                    return False
                if resp.status_code != 200:
                    log.warning("HTTP %d for %s", resp.status_code, url[:100])
                    last_err = RuntimeError(f"HTTP {resp.status_code}")
                    continue

                # ── Content-Type guard ──────────────────────────────────
                ctype = (resp.headers.get("Content-Type") or "").lower().split(";")[0].strip()
                # Allow audio types, octet-stream (common for S3), or empty
                if ctype and not (ctype.startswith("audio")
                                  or ctype == "application/octet-stream"
                                  or ctype == "binary/octet-stream"):
                    log.debug("Skipping URL — content-type=%s is not audio", ctype)
                    return False

                # ── Content-Length guard (if provided) ─────────────────
                cl = resp.headers.get("Content-Length")
                if cl:
                    try:
                        cl_int = int(cl)
                        if cl_int > max_size_bytes:
                            log.warning(
                                "File too large (%.1f MB > %d MB cap) — skipping",
                                cl_int / (1024 * 1024), max_size_bytes // (1024 * 1024),
                            )
                            return False
                        if cl_int < min_size_bytes:
                            log.warning(
                                "File too small (%d bytes < %d) — likely an error page",
                                cl_int, min_size_bytes,
                            )
                            return False
                    except ValueError:
                        pass

                # ── Stream to disk ──────────────────────────────────────
                dest_path.parent.mkdir(parents=True, exist_ok=True)
                tmp_path = dest_path.with_suffix(dest_path.suffix + ".part")
                written = 0
                last_pct_logged = -1
                with open(tmp_path, "wb") as f:
                    for chunk in resp.iter_content(chunk_size=64 * 1024):
                        if not chunk:
                            continue
                        f.write(chunk)
                        written += len(chunk)
                        # Light progress logging
                        if cl and int(cl) > 0:
                            pct = int(written * 100 / int(cl))
                            if pct >= last_pct_logged + 20:
                                last_pct_logged = pct
                                log.info("  … %d%%  (%.1f MB / %s MB)",
                                         pct,
                                         written / (1024 * 1024),
                                         f"{int(cl) / (1024 * 1024):.1f}")
                        # Hard cap during stream (in case CL header lied)
                        if written > max_size_bytes:
                            log.error("Download exceeded size cap — aborting")
                            f.close()
                            tmp_path.unlink(missing_ok=True)
                            return False

                # ── Validate result ─────────────────────────────────────
                if written < min_size_bytes:
                    log.warning(
                        "Downloaded file too small (%d bytes) — likely error",
                        written,
                    )
                    tmp_path.unlink(missing_ok=True)
                    return False

                # Atomic rename
                tmp_path.replace(dest_path)
                size_mb = written / (1024 * 1024)
                log.info(
                    "✅ Direct download OK: %s  (%.2f MB, content-type=%s)",
                    dest_path.name, size_mb, ctype or "?",
                )
                return True

        except requests.exceptions.RequestException as e:
            log.warning("Direct download attempt %d failed: %s", attempt, e)
            last_err = e
            if attempt < retries:
                time.sleep(2 * attempt)
        except Exception as e:
            log.warning("Direct download attempt %d error: %s", attempt, e)
            last_err = e
            if attempt < retries:
                time.sleep(2 * attempt)
        finally:
            # Clean up any orphan .part file on failure
            part = dest_path.with_suffix(dest_path.suffix + ".part")
            if part.exists():
                try:
                    part.unlink()
                except Exception:
                    pass

    if last_err:
        log.error("All direct download attempts failed: %s", last_err)
    return False


# ── Download directory scanner ────────────────────────────────────────────────

def _build_watch_dirs(primary_dir: Path) -> List[Path]:
    """
    Build the list of directories to monitor for downloaded files.
    Includes primary project dir, known Chrome fallback dirs on this machine.
    """
    candidates = [
        primary_dir,
        Path.home() / "Downloads",
        APP_DIR / "downloaded_files",
        APP_DIR,                         # catches root-level stray files
    ]
    seen  = set()
    dirs  = []
    for d in candidates:
        r = d.resolve()
        if r not in seen:
            seen.add(r)
            dirs.append(d)
    return dirs


# ── Main Worker ───────────────────────────────────────────────────────────────

class AdobeWorker:
    """Processes a single audio chunk through Adobe Podcast Enhance."""

    def __init__(
        self,
        profile_name: str,
        session_dir: str,
        chunk_path: Path,
        download_dir: Path,
        status_cb: Optional[Callable[[str], None]] = None,
        cancel_flag: Optional[Callable[[], bool]] = None,
    ):
        self.profile_name = profile_name
        self.session_dir  = session_dir
        self.chunk_path   = chunk_path
        self.download_dir = download_dir
        self.status_cb    = status_cb or (lambda s: None)
        self.cancel_flag  = cancel_flag or (lambda: False)

    def _emit(self, msg: str):
        log.info("[%s] %s", self.profile_name, msg)
        self.status_cb(msg)

    def _cancelled(self) -> bool:
        return self.cancel_flag()

    def run(self) -> Optional[Path]:
        for attempt in range(1, UPLOAD_RETRY_COUNT + 1):
            if self._cancelled():
                return None
            try:
                result = self._process_once()
                if result:
                    return result
            except Exception as e:
                self._emit(f"Attempt {attempt}/{UPLOAD_RETRY_COUNT} failed: {e}")
                log.exception("Attempt %d traceback:", attempt)
                if attempt < UPLOAD_RETRY_COUNT:
                    self._emit("Retrying in 10 s …")
                    time.sleep(10)
                else:
                    self._emit("All attempts exhausted — chunk marked FAILED.")
                    return None
        return None

    def _process_once(self) -> Optional[Path]:
        self._emit(f"Opening Chrome with profile '{self.profile_name}' …")

        try:
            with _make_sb(self.session_dir) as sb:

                # ── Navigate ──────────────────────────────────────────────────────
                sb.open(ADOBE_ENHANCE_URL)
                self._emit("Page loaded — waiting for SPA to initialise …")
                time.sleep(5)

                # ── Force download directory (try Browser + Page CDP commands) ────
                _set_download_dir(sb, self.download_dir)

                # ── Login check ───────────────────────────────────────────────────
                self._emit("Checking login state …")
                url = sb.get_current_url()
                if "login" in url.lower() or "auth" in url.lower():
                    raise RuntimeError(
                        "Session expired — please click 'Re-Login' on this profile."
                    )

                # ── Wait up to 30 s for upload UI ─────────────────────────────────
                self._emit("Waiting for upload UI …")
                for _w in range(30):
                    if sb.execute_script(
                        "return !!document.querySelector('input#enhance-file-upload');"
                    ):
                        break
                    time.sleep(1)
                else:
                    raise RuntimeError(
                        "Upload UI not found after 30 s. Check internet & login."
                    )

                # ── Start background popup watcher ────────────────────────────────
                watcher = _PopupWatcher(sb, self._emit)
                watcher.start()

                try:
                    # ── Upload ────────────────────────────────────────────────────
                    self._emit(f"Uploading: {self.chunk_path.name}")
                    _upload_file(sb, self.chunk_path)
                    self._emit("✅ Upload sent — Adobe processing …")

                    # ── Smart initial wait (replaces fixed 4-min blind wait) ──────
                    # Poll every 5 s; break early the moment the download button
                    # appears OR a valid audio URL appears in network logs.
                    wait_mins = INITIAL_WAIT_SECONDS // 60
                    self._emit(f"Smart wait: up to {wait_mins} min (will break early if ready) …")
                    _early_break_url: Optional[str] = None
                    for elapsed_s in range(0, INITIAL_WAIT_SECONDS, 5):
                        if self._cancelled():
                            return None

                        # Check page state every 5 s
                        state = _get_page_state(sb)

                        # Break immediately if download button is visible
                        if state == "download_ready":
                            self._emit(f"✅ Download ready after {elapsed_s}s — skipping remaining wait")
                            break

                        # Also scan network logs for the URL (appears before/without button)
                        try:
                            early_urls = _extract_download_urls_from_logs(sb)
                            if early_urls:
                                _early_break_url = early_urls[0]
                                self._emit(
                                    f"✅ Audio URL captured after {elapsed_s}s — "
                                    "closing browser & downloading …"
                                )
                                break
                        except Exception:
                            pass

                        if elapsed_s > 0 and elapsed_s % 60 == 0:
                            rem = (INITIAL_WAIT_SECONDS - elapsed_s) // 60
                            self._emit(f"  … {rem} min remaining | state: {state}")

                        time.sleep(5)

                    # ── If URL found during initial wait → close browser, then download ──
                    # Raising _EarlyDownloadSignal exits this try/finally (watcher.stop()),
                    # then exits the with-block (Chrome closes), then is caught OUTSIDE.
                    if _early_break_url:
                        watcher.stop()
                        self._emit("Closing browser to free bandwidth …")
                        temp_dst = self.download_dir / f"{self.chunk_path.stem}_direct.wav"
                        raise _EarlyDownloadSignal(_early_break_url, temp_dst)

                    # ── Poll for download ──────────────────────────────────────────
                    # Strategy:
                    #   1) Force-dismiss any popup FIRST (popup blocks scan otherwise)
                    #   2) Scan network logs for audio/S3 URL → raise signal (close browser)
                    #   3) If no URL found → look for the download button
                    #   4) After click, re-scan network logs (URL appears post-click)
                    #   5) If everything fails → retry the whole upload
                    self._emit("Polling for download …")
                    poll_elapsed = 0
                    poll_timeout = TIMEOUT_SECONDS - INITIAL_WAIT_SECONDS
                    last_state   = ""
                    last_url_scan = 0
                    last_popup_dismiss = 0
                    URL_SCAN_INTERVAL    = 8    # seconds between network-log scans
                    POPUP_DISMISS_INTERVAL = 10 # seconds between forced dismisses
                    tried_urls: set[str] = set()  # URLs already attempted (skip on next scan)

                    while poll_elapsed < poll_timeout:
                        if self._cancelled():
                            return None

                        # ── (0) Force-dismiss popup before any scan ─────────
                        if poll_elapsed - last_popup_dismiss >= POPUP_DISMISS_INTERVAL:
                            if _has_visible_modal(sb):
                                _force_dismiss_popups(sb, max_attempts=2)
                            last_popup_dismiss = poll_elapsed

                        # ── (1) Network-log scan for direct URL ──────────────
                        # When found, raise signal → browser closes → download starts
                        if poll_elapsed - last_url_scan >= URL_SCAN_INTERVAL:
                            last_url_scan = poll_elapsed
                            try:
                                urls = _extract_download_urls_from_logs(sb)
                            except Exception as e:
                                log.debug("URL scan error: %s", e)
                                urls = []
                            new_urls = [u for u in urls if u not in tried_urls]
                            if len(new_urls) < len(urls):
                                log.debug("Skipping %d already-tried URLs", len(urls) - len(new_urls))
                            for url in new_urls[:3]:
                                tried_urls.add(url)
                                temp_dst = self.download_dir / f"{self.chunk_path.stem}_direct.wav"
                                self._emit("Audio URL found — closing browser to free bandwidth …")
                                raise _EarlyDownloadSignal(url, temp_dst)

                        # ── (2) Look for the download button on the page ──────
                        if _has_visible_modal(sb):
                            _force_dismiss_popups(sb, max_attempts=2)

                        btn = _find_download_button(sb)
                        if btn:
                            self._emit("✅ Download button found — clicking …")
                            watcher.stop()

                            clicked = False
                            try:
                                btn.click()
                                clicked = True
                            except Exception as e:
                                log.debug("Native click failed (%s) — using JS click", e)
                                try:
                                    sb.execute_script("arguments[0].click();", btn)
                                    clicked = True
                                except Exception as e2:
                                    log.warning("JS click also failed: %s", e2)

                            if clicked:
                                # Give the page a moment to start the download
                                time.sleep(2)

                                # ── (3) Re-scan network logs after the click ──
                                try:
                                    post_urls = _extract_download_urls_from_logs(sb)
                                except Exception:
                                    post_urls = []
                                post_new = [u for u in post_urls if u not in tried_urls]
                                for url in post_new[:3]:
                                    tried_urls.add(url)
                                    temp_dst = self.download_dir / f"{self.chunk_path.stem}_direct.wav"
                                    self._emit("Post-click URL found — closing browser …")
                                    raise _EarlyDownloadSignal(url, temp_dst)

                                # ── (4) Wait for Chrome to write the file to disk ──
                                downloaded = self._wait_for_download(tried_urls=tried_urls)
                                if downloaded:
                                    self._emit(f"✅ Downloaded: {downloaded.name}")
                                    return downloaded

                                log.warning("Download button clicked but no file appeared — will retry")
                                watcher = _PopupWatcher(sb, self._emit).start()

                        # Update state description for console feedback
                        state = _get_page_state(sb)
                        if state != last_state:
                            labels = {
                                "download_ready":  "✅ Enhancement complete — download ready",
                                "spinner":         "⏳ Adobe loading/processing (spinner) …",
                                "processing_text": "🔄 Adobe processing audio …",
                                "unknown":         "⏳ Waiting for Adobe …",
                            }
                            self._emit(labels.get(state, f"State: {state}"))
                            last_state = state

                        time.sleep(SCAN_INTERVAL_SECONDS)
                        poll_elapsed += SCAN_INTERVAL_SECONDS

                        if poll_elapsed % 60 == 0:
                            self._emit(
                                f"Still waiting … "
                                f"{(poll_timeout - poll_elapsed) // 60} min left"
                            )

                    raise RuntimeError(
                        f"Timeout after {TIMEOUT_SECONDS // 60} min — "
                        "download button never appeared."
                    )
                except _EarlyDownloadSignal:
                    raise   # bubble up through the with-block so browser closes
                finally:
                    watcher.stop()

        except _EarlyDownloadSignal as sig:
            # Browser is now CLOSED (with-block exited).  Download with full bandwidth.
            self._emit("Browser closed ✅ — starting direct download with full bandwidth …")
            if _download_url_direct(sig.url, sig.dest_path):
                self._emit(
                    f"✅ Download complete: {sig.dest_path.name} "
                    f"({sig.dest_path.stat().st_size / (1024*1024):.1f} MB)"
                )
                return sig.dest_path
            # URL failed (expired / 403) — fall through to a full retry
            self._emit("⚠ Direct download failed — will retry full cycle …")
            return None

    # ── Download monitor ──────────────────────────────────────────────────────

    def _wait_for_download(self, tried_urls: Optional[set] = None) -> Optional[Path]:
        """
        Watch multiple directories for a new completed audio file.
        Scans: project download dir, system Downloads, app downloaded_files,
               and app root — because Chrome sometimes ignores CDP download path.
        Large files can take many minutes — timeout is 15 min.

        Handles the "user manually clicked the button" case:
          - The file may already be in download_dir (from the manual click)
          - The standard "before snapshot" approach would miss it
          - So we FIRST check for any large recent file in the dirs, then
            fall through to the standard polling loop.

        Args:
            tried_urls: URLs already attempted via direct download (kept here
                        for compatibility, no longer used for fallback).
        """
        IN_PROGRESS = {".crdownload", ".tmp", ".part", ".download"}
        AUDIO_EXTS  = {".wav", ".mp3", ".aac", ".flac", ".m4a", ".ogg", ".oga"}
        MIN_AUDIO_BYTES = 100 * 1024   # 100 KB — smaller is likely an error
        MAX_AGE_SECONDS = 600          # 10 min — how stale a "manual" file can be

        watch_dirs = _build_watch_dirs(self.download_dir)

        # ── Pre-scan: catch the "user already manually clicked" case ────
        # The file might already be in the dir (Chrome's download was triggered
        # manually before our polling started), so it would never appear as
        # "new" in the before/current diff.
        for watch_dir in watch_dirs:
            try:
                for f in watch_dir.glob("*"):
                    if not f.is_file():
                        continue
                    if f.suffix.lower() in IN_PROGRESS:
                        continue
                    if f.suffix.lower() not in AUDIO_EXTS:
                        continue
                    try:
                        if f.stat().st_size < MIN_AUDIO_BYTES:
                            continue
                        age = time.time() - f.stat().st_mtime
                        if age > MAX_AGE_SECONDS:
                            continue
                    except Exception:
                        continue
                    # Found a recent large audio file — assume it was just downloaded
                    self._emit(
                        f"✅ Found recent file (manual click?): {f.name} "
                        f"({f.stat().st_size / (1024*1024):.1f} MB, {int(age)}s old)"
                    )
                    # Move to project downloads dir if needed
                    if watch_dir.resolve() != self.download_dir.resolve():
                        dest = self.download_dir / f.name
                        self.download_dir.mkdir(parents=True, exist_ok=True)
                        try:
                            f.rename(dest)
                            self._emit(f"Moved {watch_dir.name} → downloads: {dest.name}")
                            return dest
                        except Exception as e:
                            log.warning("Could not move recent file: %s", e)
                            return f
                    return f
            except Exception as e:
                log.debug("Pre-scan error in %s: %s", watch_dir, e)

        # Snapshot before-state for all dirs (for the standard polling)
        before: dict[Path, set] = {}
        for d in watch_dirs:
            try:
                d.mkdir(parents=True, exist_ok=True)
                before[d] = set(d.glob("*"))
            except Exception:
                before[d] = set()

        self._emit("Watching: " + " | ".join(str(d) for d in watch_dirs))
        self._emit(f"Download timeout: {DOWNLOAD_WAIT_TIMEOUT // 60} min")

        deadline = time.time() + DOWNLOAD_WAIT_TIMEOUT
        ticks    = 0

        while time.time() < deadline:
            if self._cancelled():
                return None

            for watch_dir in watch_dirs:
                try:
                    current = set(watch_dir.glob("*"))
                except Exception:
                    continue

                new_files = [
                    f for f in (current - before[watch_dir])
                    if f.is_file()
                    and f.suffix.lower() not in IN_PROGRESS
                ]

                if new_files:
                    audio = [f for f in new_files if f.suffix.lower() in AUDIO_EXTS]
                    result = max(
                        audio if audio else new_files,
                        key=lambda p: p.stat().st_mtime,
                    )
                    time.sleep(1.5)  # let Chrome finish writing

                    # Move to project downloads dir if found elsewhere
                    if watch_dir.resolve() != self.download_dir.resolve():
                        dest = self.download_dir / result.name
                        self.download_dir.mkdir(parents=True, exist_ok=True)
                        try:
                            result.rename(dest)
                            result = dest
                            self._emit(f"Moved {watch_dir.name} → downloads: {dest.name}")
                        except Exception as e:
                            log.warning("Could not move file: %s", e)
                            # Use in-place if rename fails
                            result = result

                    self._emit(f"✅ Download complete: {result.name}")
                    return result

                before[watch_dir] = current  # slide the window

            ticks += 1
            if ticks % 30 == 0:
                elapsed_m = ticks // 60
                remain_m  = (DOWNLOAD_WAIT_TIMEOUT - ticks) // 60
                self._emit(
                    f"  Waiting for file … {elapsed_m} min elapsed, "
                    f"{remain_m} min remaining"
                )
            time.sleep(1)

        self._emit("⚠ Download timeout — file not found in any watched directory.")
        return None
