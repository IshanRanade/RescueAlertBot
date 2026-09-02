from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError, Error as PlaywrightError
import re
import requests
import signal
import socket
import time
import os
import sys
import json
from datetime import datetime, timezone, timedelta

from zoneinfo import ZoneInfo

import urllib3.util.connection

from blocklist import is_hospital_blocked

sys.stdout.reconfigure(line_buffering=True)


# Force IPv4 for all `requests`/urllib3 traffic (e.g. Telegram). Chromium is
# pinned to IPv4 separately via the --disable-ipv6 launch flag. All bot traffic
# exits through the WireGuard VPN, whose IPv6 path is unreliable, so we make
# urllib3's resolver hand back only IPv4 addresses.
def _allowed_gai_family_ipv4_only():
    return socket.AF_INET


urllib3.util.connection.allowed_gai_family = _allowed_gai_family_ipv4_only


LOCAL_TZ = ZoneInfo("America/Los_Angeles")


def log(msg):
    """Print with timestamp."""
    timestamp = datetime.now(LOCAL_TZ).strftime("%Y/%m/%d %H:%M:%S %Z")
    print(f"[{timestamp}] {msg}", flush=True)

# Global flag for graceful shutdown
SHUTDOWN_REQUESTED = False

# FAILSAFE: Hard max runtime (timer duration + 5 min buffer)
TIMER_DURATION = int(os.environ.get("TIMER_DURATION", 60 * 60))  # Default 1 hour
MAX_RUNTIME_SECONDS = TIMER_DURATION + 5 * 60  # Timer + 5 min buffer
BOT_START_TIME = time.time()


def check_hard_timeout():
    """Failsafe: Gracefully stop bot if it's been running too long (backup for timer)."""
    global SHUTDOWN_REQUESTED
    elapsed = time.time() - BOT_START_TIME
    if elapsed > MAX_RUNTIME_SECONDS:
        log(f"⛔ FAILSAFE: Bot exceeded max runtime ({MAX_RUNTIME_SECONDS}s). Shutting down gracefully.")
        SHUTDOWN_REQUESTED = True


def handle_shutdown(signum, frame):
    """Handle SIGTERM/SIGINT for graceful shutdown."""
    global SHUTDOWN_REQUESTED
    log(f"🛑 Received signal {signum}, shutting down gracefully...")
    SHUTDOWN_REQUESTED = True


def handle_timer_reset(signum, frame):
    """Handle SIGUSR1 to reset failsafe timer."""
    global BOT_START_TIME
    BOT_START_TIME = time.time()
    log("🔄 Failsafe timer reset.")


signal.signal(signal.SIGTERM, handle_shutdown)
signal.signal(signal.SIGINT, handle_shutdown)
signal.signal(signal.SIGUSR1, handle_timer_reset)

CASE_ACCEPTED_FILE = "case_accepted.json"
CASE_ACKNOWLEDGED_FILE = "case_acknowledged"
LOGIN_URL = "https://login.mysevaro.com"
HOME_URL = "https://login.mysevaro.com/app/UserHome"
RESCUE_SELECTOR = "li.rescue-dashboard-container a.nav-link"
SYNAPSE_SELECTOR = '[data-se="app-card-title"][title="Synapse 2.0"]'
NOTIFICATION_POPUP_SELECTOR = "div.rescue-notification-container"

EMAIL = os.environ.get("EMAIL")
PASSWORD = os.environ.get("PASSWORD")
OTP = os.environ.get("OTP", "")
MS_EMAIL = os.environ.get("MS_EMAIL") or EMAIL
MS_PASSWORD = os.environ.get("MS_PASSWORD") or PASSWORD
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")


TELEGRAM_MAX_ATTEMPTS = int(os.environ.get("TELEGRAM_MAX_ATTEMPTS", 5))


def send_notification(msg):
    """Send Telegram notification, retrying transient failures with backoff.

    All bot traffic exits through the WireGuard VPN, where Telegram is reachable
    but ~3x slower and occasionally spikes past a tight timeout. A single blip
    should not kill the bot, so retry a few times before giving up.
    Returns True on success, False after all attempts fail."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log("Telegram disabled (missing token/chat id)")
        return False

    log(f"📤 Sending Telegram: {msg}")
    for attempt in range(1, TELEGRAM_MAX_ATTEMPTS + 1):
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                data={"chat_id": TELEGRAM_CHAT_ID, "text": msg},
                timeout=20,
            )
            if r.ok:
                log(f"📱 Telegram sent:\n{msg}")
                return True
            log(f"Telegram failed ({r.status_code}, attempt {attempt}/{TELEGRAM_MAX_ATTEMPTS}): {r.text}")
        except Exception as e:
            log(f"Telegram error (attempt {attempt}/{TELEGRAM_MAX_ATTEMPTS}): {e}")

        if attempt < TELEGRAM_MAX_ATTEMPTS:
            backoff = min(2 ** attempt, 30)  # 2s, 4s, 8s, 16s, capped at 30s
            interruptible_sleep(backoff)

    log(f"❌ Telegram failed after {TELEGRAM_MAX_ATTEMPTS} attempts")
    return False


# Okta has shipped sign-in widget versions that use <input type="submit"> and
# versions that use <button type="submit">, so match either.
SUBMIT_SELECTOR = 'input[type="submit"]:visible, button[type="submit"]:visible'


def _submit_okta_step(page, field_selector):
    """Submit the current Okta step. Click a visible submit button, or press
    Enter in the field if there isn't one (Enter submits the form either way)."""
    button = page.locator(SUBMIT_SELECTOR).first
    try:
        button.click(timeout=5000)
    except PlaywrightTimeoutError:
        log("⚠️ No submit button found, pressing Enter instead")
        page.press(field_selector, "Enter")


def dump_login_failure(page, label="login_failed"):
    """Save HTML, a screenshot, and the URL so we can see which screen login got stuck on."""
    try:
        log(f"🌐 Login page URL ({label}): {page.url}")
    except Exception:
        pass
    dump_page_html(page, label)
    try:
        ts = datetime.now(LOCAL_TZ).strftime("%Y%m%d_%H%M%S")
        path = f"data/{ts}_{label}.png"
        page.screenshot(path=path, full_page=True)
        log(f"📸 Screenshot saved to {path}")
    except Exception as e:
        log(f"⚠️ Could not save screenshot: {e}")


def send_photo_notification(page, caption):
    """Send a screenshot of the current page to Telegram. Used as a fallback when
    the bot can't read the Authenticator number off the page, so the user can
    still read it themselves. Returns True on success."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False
    try:
        png = page.screenshot()
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto",
            data={"chat_id": TELEGRAM_CHAT_ID, "caption": caption},
            files={"photo": ("screen.png", png, "image/png")},
            timeout=30,
        )
        if r.ok:
            log(f"📱 Telegram photo sent: {caption}")
            return True
        log(f"Telegram photo failed ({r.status_code}): {r.text}")
    except Exception as e:
        log(f"Telegram photo error: {e}")
    return False


class LoginError(Exception):
    """Login failed for a reason we can explain to the user."""


def login(page):
    log("🔐 Login required")
    try:
        _login_steps(page)
    except Exception as e:
        dump_login_failure(page)
        reason = str(e).strip().splitlines()[0] if str(e).strip() else type(e).__name__
        send_notification(f"❌ Login failed: {reason}")
        raise
    log("✅ Login successful")
    dump_page_html(page, "after_login")


# Login is a sequence of Okta and Microsoft screens whose order Sevaro can change
# (e.g. Okta now hands sign-in to Microsoft after the email step). Rather than
# hard-coding the order, poll for whichever screen is showing and handle it.
LOGIN_TIMEOUT_SECONDS = 300  # includes time for the user to approve on their phone
LOGIN_STUCK_DUMP_SECONDS = 30
MAX_ATTEMPTS_PER_SCREEN = 3

MS_HOST = "login.microsoftonline.com"
MS_SUBMIT = "#idSIButton9"  # Microsoft's Next / Sign in / Yes button
# Microsoft keeps inactive inputs in the DOM, parked off-screen with the
# moveOffScreen class (e.g. the password box exists on the email screen).
# Playwright counts those as visible, so they must be excluded explicitly.
MS_EMAIL_INPUT = 'input[name="loginfmt"]:not(.moveOffScreen)'
MS_PASSWORD_INPUT = 'input[name="passwd"]:not(.moveOffScreen)'
MS_NUMBER_SELECTORS = ["#idRichContext_DisplaySign", ".displaySign", '[data-testid="displaySign"]']
MS_APPROVE_TITLE = "#idDiv_SAOTCAS_Title"  # "Approve sign in request"
MS_APP_NOTIFICATION_OPTION = '[data-value="PhoneAppNotification"]'  # MFA method picker
MS_KMSI = "#KmsiCheckboxField, #KmsiDescription"  # "Stay signed in?"
MS_ERRORS = {
    "#usernameError": "Microsoft rejected the email",
    "#passwordError": "Microsoft rejected the password",
    "#idDiv_SAASDS_Title": "Microsoft Authenticator request was denied",
    "#idDiv_SAASTO_Title": "Microsoft Authenticator request timed out (not approved in time)",
}

OKTA_IDENTIFIER_INPUT = 'input[name="identifier"]'
OKTA_PASSWORD_INPUT = 'input[type="password"]'
# Okta's password box is also named credentials.passcode, so exclude type=password.
OKTA_TOTP_INPUT = 'input[name="otp"], input[name="credentials.passcode"]:not([type="password"]), input[id*="totp"]'


def _visible(page, selector):
    try:
        return page.locator(selector).first.is_visible()
    except PlaywrightError:
        return False


def _wait_hidden(page, selector, timeout=10000):
    """After submitting a screen, give the page time to move on so the next poll
    doesn't see the same screen again and re-submit it."""
    try:
        page.locator(selector).first.wait_for(state="hidden", timeout=timeout)
    except PlaywrightError:
        pass


def _read_authenticator_number(page):
    """Return the number-matching code shown on Microsoft's MFA screen, or None."""
    for sel in MS_NUMBER_SELECTORS:
        if _visible(page, sel):
            text = (page.locator(sel).first.text_content() or "").strip()
            if text.isdigit():
                return text
    # Fallback: a line that is only a 2-3 digit number, on a page that mentions
    # the Authenticator app.
    try:
        body = page.locator("body").inner_text()
    except PlaywrightError:
        return None
    if "authenticator" in body.lower() or "enter the number" in body.lower():
        m = re.search(r"^\s*(\d{2,3})\s*$", body, re.M)
        if m:
            return m.group(1)
    return None


def _login_steps(page):
    page.goto(LOGIN_URL)

    attempts = {}  # screen name -> times we've submitted it
    sent_number = None
    sent_approve_screenshot = False
    deadline = time.time() + LOGIN_TIMEOUT_SECONDS
    last_recognized = time.time()
    dumped_stuck = False

    def act(screen):
        attempts[screen] = attempts.get(screen, 0) + 1
        if attempts[screen] > MAX_ATTEMPTS_PER_SCREEN:
            raise LoginError(f"Stuck on the {screen} screen")
        log(f"🔐 Login screen: {screen}")

    while time.time() < deadline:
        if SHUTDOWN_REQUESTED:
            raise LoginError("Shutdown requested during login")

        try:
            recognized = True

            if MS_HOST in page.url:
                for sel, reason in MS_ERRORS.items():
                    if _visible(page, sel):
                        detail = (page.locator(sel).first.text_content() or "").strip()
                        raise LoginError(f"{reason}: {detail}" if detail else reason)

                number = _read_authenticator_number(page)
                if number:
                    if number != sent_number:
                        log(f"🔢 Microsoft Authenticator number: {number}")
                        if not send_notification(
                            f"🔢 Microsoft sign-in: enter {number} in your Microsoft Authenticator app."
                        ):
                            raise LoginError("Could not send the Authenticator number over Telegram")
                        sent_number = number
                elif _visible(page, MS_APPROVE_TITLE):
                    # Approval screen without a readable number: send a screenshot
                    # so the user can read it (or just approve if there's none).
                    if not sent_approve_screenshot:
                        log("📲 Microsoft approval screen, number not found; sending screenshot")
                        if not send_photo_notification(
                            page, "📲 Microsoft sign-in: approve this request in your Microsoft Authenticator app."
                        ) and not send_notification(
                            "📲 Microsoft sign-in: approve the request in your Microsoft Authenticator app."
                        ):
                            raise LoginError("Could not send the Authenticator prompt over Telegram")
                        sent_approve_screenshot = True
                elif _visible(page, MS_PASSWORD_INPUT):
                    act("Microsoft password")
                    page.fill(MS_PASSWORD_INPUT, MS_PASSWORD)
                    page.click(MS_SUBMIT)
                    _wait_hidden(page, MS_PASSWORD_INPUT)
                elif _visible(page, MS_EMAIL_INPUT):
                    act("Microsoft email")
                    page.fill(MS_EMAIL_INPUT, MS_EMAIL)
                    page.click(MS_SUBMIT)
                    _wait_hidden(page, MS_EMAIL_INPUT)
                elif _visible(page, MS_APP_NOTIFICATION_OPTION):
                    act("Microsoft MFA method picker")
                    page.click(MS_APP_NOTIFICATION_OPTION)
                    _wait_hidden(page, MS_APP_NOTIFICATION_OPTION)
                elif _visible(page, MS_KMSI):
                    act("Microsoft 'Stay signed in?'")
                    page.click(MS_SUBMIT)
                    _wait_hidden(page, MS_KMSI)
                else:
                    recognized = False
            else:
                if _visible(page, SYNAPSE_SELECTOR):
                    return
                if _visible(page, OKTA_TOTP_INPUT):
                    if not OTP:
                        raise LoginError("Okta asked for a Google Authenticator code but none was entered")
                    act("Okta authenticator code")
                    page.click(OKTA_TOTP_INPUT)
                    page.type(OKTA_TOTP_INPUT, OTP, delay=50)
                    _submit_okta_step(page, OKTA_TOTP_INPUT)
                    _wait_hidden(page, OKTA_TOTP_INPUT)
                elif _visible(page, OKTA_PASSWORD_INPUT):
                    act("Okta password")
                    page.fill(OKTA_PASSWORD_INPUT, PASSWORD)
                    _submit_okta_step(page, OKTA_PASSWORD_INPUT)
                    _wait_hidden(page, OKTA_PASSWORD_INPUT)
                elif _visible(page, OKTA_IDENTIFIER_INPUT):
                    act("Okta email")
                    page.fill(OKTA_IDENTIFIER_INPUT, EMAIL)
                    _submit_okta_step(page, OKTA_IDENTIFIER_INPUT)
                    _wait_hidden(page, OKTA_IDENTIFIER_INPUT)
                else:
                    recognized = False
        except LoginError:
            raise
        except PlaywrightError as e:
            # Usually the page navigated mid-check ("execution context was
            # destroyed"); just look again on the next poll.
            log(f"⚠️ Login poll interrupted, retrying: {str(e).splitlines()[0]}")
            recognized = False

        if recognized:
            last_recognized = time.time()
            dumped_stuck = False
        elif not dumped_stuck and time.time() - last_recognized > LOGIN_STUCK_DUMP_SECONDS:
            log(f"⚠️ Unrecognized login screen for {LOGIN_STUCK_DUMP_SECONDS}s at {page.url}")
            dump_login_failure(page, "login_unrecognized_screen")
            dumped_stuck = True

        time.sleep(1)

    raise LoginError(f"Timed out after {LOGIN_TIMEOUT_SECONDS}s (last page: {page.url})")


def launch_synapse_tab(context, page):
    """Open a new Synapse tab from the Okta home page. Returns the new page."""
    page.get_by_role("button", name="Settings for Synapse 2.0").click()

    launch_btn = page.locator('[data-se="app-settings-launch-app-button"]')
    launch_btn.wait_for(state="visible", timeout=15000)

    with context.expect_page() as new_page_info:
        launch_btn.click()

    synapse_page = new_page_info.value
    log("🚀 Synapse opened")

    synapse_page.wait_for_load_state("load", timeout=30000)
    dump_page_html(synapse_page, "after_synapse_opened")
    return synapse_page


SYNAPSE_RENDER_TIMEOUT_MS = 15000
SYNAPSE_MAX_RETRIES = 1


def _synapse_app_is_healthy(synapse_page):
    """Check if the Synapse SPA rendered with a sidebar (not an empty shell)."""
    try:
        synapse_page.wait_for_selector(
            RESCUE_SELECTOR, state="visible", timeout=SYNAPSE_RENDER_TIMEOUT_MS
        )
        return True
    except Exception:
        return False


def start_synapse(context, page):
    for attempt in range(1, SYNAPSE_MAX_RETRIES + 1):
        synapse_page = None
        try:
            synapse_page = launch_synapse_tab(context, page)

            if _synapse_app_is_healthy(synapse_page):
                synapse_page.locator(RESCUE_SELECTOR).click()
                log("🎯 Rescue Dashboard opened")
                dump_page_html(synapse_page, "after_rescue_dashboard")
                return synapse_page

            log(f"⚠️ Synapse loaded without sidebar (attempt {attempt}/{SYNAPSE_MAX_RETRIES})")
            dump_page_html(synapse_page, f"synapse_no_sidebar_attempt{attempt}")

        except Exception as e:
            log(f"⚠️ Synapse failed to load (attempt {attempt}/{SYNAPSE_MAX_RETRIES}): {e}")
            if synapse_page is not None:
                dump_page_html(synapse_page, f"synapse_error_attempt{attempt}")

        if synapse_page is not None:
            try:
                synapse_page.close()
            except Exception:
                pass

        if attempt < SYNAPSE_MAX_RETRIES:
            log("🔄 Closing Synapse tab, relaunching from Okta...")
            page.bring_to_front()
            page.goto(HOME_URL)
            page.wait_for_load_state("load", timeout=30000)
            time.sleep(3)

    send_notification("❌ Synapse failed to load. Please start the bot again.")
    raise RuntimeError("Synapse failed to load after all attempts")


def get_text(locator):
    """Get text content from locator, or None if not found."""
    return locator.text_content().strip() if locator.count() > 0 else None


def extract_case_info(page):
    """Extract hospital name, patient name, and patient ID from the case row."""
    try:
        case_row = page.locator('div.complete-row:has(button:has-text("Accept"))').first
        if case_row.count() == 0:
            case_row = page.locator('div.complete-row').first
        if case_row.count() == 0:
            return None, None, None

        hospital = get_text(case_row.locator("div.facility-name div").first)
        patient = get_text(case_row.locator('div[data-dd-action-name="rescue-dashboard-patient-name"] span[data-dd-privacy="mask"] span[apptruncatepopover]').first)
        patient_id = get_text(case_row.locator('span[data-dd-action-name="rescue-dashboard-mrn"]').first)
        return hospital, patient, patient_id
    except Exception as e:
        log(f"⚠️ Error extracting case info: {e}")
        return None, None, None


def extract_notification_case_info(page):
    """Extract hospital, patient name, and patient ID from the notification popup."""
    try:
        popup = page.locator(NOTIFICATION_POPUP_SELECTOR).first
        if popup.count() == 0:
            return None, None, None

        hospital = get_text(popup.locator('[data-dd-action-name="rescue-notification-facility-name"]').first)
        patient = get_text(popup.locator('[data-dd-action-name="rescue-notification-patient-name"]').first)
        patient_id = get_text(popup.locator('[data-dd-action-name="rescue-notification-patient-mrn"]').first)
        return hospital, patient, patient_id
    except Exception as e:
        log(f"⚠️ Error extracting notification case info: {e}")
        return None, None, None


def write_case_accepted(hospital, patient, patient_id):
    """Write accepted case info to file so the Flask app can detect it."""
    data = {
        "hospital": hospital,
        "patient": patient,
        "patient_id": patient_id,
        "accepted_at": datetime.now().astimezone().isoformat(),
    }
    with open(CASE_ACCEPTED_FILE, "w") as f:
        json.dump(data, f)


def clear_case_files():
    """Remove case accepted/acknowledged files."""
    for path in (CASE_ACCEPTED_FILE, CASE_ACKNOWLEDGED_FILE):
        try:
            os.remove(path)
        except FileNotFoundError:
            pass


def wait_for_acknowledge(hospital, patient, patient_id):
    """Send Telegram every 30 seconds until user acknowledges via the UI.
    Blocks the bot from accepting new cases while waiting."""
    msg = f"🚨 Rescue case accepted!\n\n🏥 Hospital: {hospital}\n👤 Patient: {patient}\n🆔 Patient ID: {patient_id}"
    log("⏳ Waiting for user to acknowledge the accepted case...")

    while not SHUTDOWN_REQUESTED:
        check_hard_timeout()

        if os.path.exists(CASE_ACKNOWLEDGED_FILE):
            log("✅ Case acknowledged by user.")
            clear_case_files()
            return

        if not send_notification(msg):
            log("❌ Telegram failed. Exiting bot.")
            clear_case_files()
            sys.exit(1)

        for _ in range(300):
            if SHUTDOWN_REQUESTED or os.path.exists(CASE_ACKNOWLEDGED_FILE):
                break
            time.sleep(0.1)

    if os.path.exists(CASE_ACKNOWLEDGED_FILE):
        log("✅ Case acknowledged by user.")
    clear_case_files()


MAX_HTML_DUMPS = int(os.environ.get("MAX_HTML_DUMPS", 200))


def _prune_html_dumps():
    """Keep only the newest MAX_HTML_DUMPS HTML/PNG dumps in data/ so the disk
    doesn't fill up over time (an ENOSPC will crash Chromium)."""
    try:
        dumps = [
            os.path.join("data", f)
            for f in os.listdir("data")
            if f.endswith((".html", ".png"))
        ]
        if len(dumps) <= MAX_HTML_DUMPS:
            return
        dumps.sort(key=os.path.getmtime)
        for old in dumps[:-MAX_HTML_DUMPS]:
            try:
                os.remove(old)
            except OSError:
                pass
    except Exception as e:
        log(f"⚠️ Could not prune HTML dumps: {e}")


def dump_page_html(page, label="debug"):
    """Dump page HTML to a file for debugging."""
    try:
        ts = datetime.now(LOCAL_TZ).strftime("%Y%m%d_%H%M%S")
        path = f"data/{ts}_{label}.html"
        html = page.content()
        with open(path, "w", encoding="utf-8") as f:
            f.write(html)
        log(f"📄 Page HTML dumped to {path}")
        _prune_html_dumps()
    except Exception as e:
        log(f"⚠️ Could not dump page HTML: {e}")


# Remembers the last case we ignored so we log it once, not every poll.
LAST_IGNORED_KEY = None


def case_is_blocked(hospital, patient, patient_id):
    """Return True if this case's hospital is on the user's blocklist. When it
    is, the bot must NOT accept the case. Logs once per distinct blocked case to
    avoid spamming the log while the case lingers on the dashboard."""
    global LAST_IGNORED_KEY
    blocked, matched = is_hospital_blocked(hospital)
    if not blocked:
        return False
    key = f"{hospital}|{patient_id}"
    if key != LAST_IGNORED_KEY:
        LAST_IGNORED_KEY = key
        log(
            f"🚫 Ignoring case from blocked hospital '{hospital}' "
            f"(matched '{matched}'). Patient: {patient}, ID: {patient_id}"
        )
    return True


def handle_new_case(page):
    """Look for an Accept button on the page and click it.
    Uses the same proven poll-and-click approach from v1.2/v1.3: find the button,
    extract case info, click, and trust the click succeeded.
    Returns: "accepted" if case was accepted,
             "ignored" if the case's hospital is on the user's blocklist,
             "not_credentialed" if no Accept button found (user not credentialed),
             "failed" if credentialed but could not complete acceptance."""
    try:
        accept_selector = 'button:has-text("Accept")'
        saw_accept_button = False

        # Ensure we're on the rescue dashboard before looking for buttons
        try:
            page.locator(RESCUE_DASHBOARD_INDICATOR).wait_for(state="attached", timeout=5000)
        except Exception:
            log("⚠️ Not on rescue dashboard, dashboard is broken")
            dump_page_html(page, "dashboard_broken")
            if not send_notification("❌ Dashboard is broken, please restart the bot. A case was detected that you may be credentialed for, please check the dashboard manually."):
                log("❌ Telegram failed. Exiting bot.")
            sys.exit(1)

        # Scope the dashboard Accept button to the case row. The notification popup
        # also contains a "Accept" button, so a page-wide button:has-text("Accept")
        # would match the popup too — and if a blocked popup is persisting, its
        # Accept button could be clicked here and accept the blocked case. Matching
        # only inside div.complete-row keeps the dashboard branch popup-proof and
        # mirrors the row selector used by extract_case_info.
        dashboard_accept_selector = 'div.complete-row button:has-text("Accept")'

        for attempt in range(20):
            if SHUTDOWN_REQUESTED:
                return "not_credentialed"

            # A blocked case shown in the notification popup must NOT short-circuit
            # this call: the popup overlays the dashboard, and a *different*,
            # non-blocked case may be sitting in the dashboard row behind it. So a
            # blocked popup falls through to the dashboard check below instead of
            # returning "ignored" immediately (which would shadow that case).
            popup_case_blocked = False

            # Check notification popup first (overlays dashboard with higher z-index)
            popup = page.locator(NOTIFICATION_POPUP_SELECTOR)
            if popup.count() > 0:
                popup_accept = popup.locator(accept_selector)
                if popup_accept.count() > 0:
                    saw_accept_button = True
                    log("📢 Notification popup detected, using popup Accept button")
                    hospital, patient, patient_id = extract_notification_case_info(page)

                    if not hospital or not patient or not patient_id:
                        log(f"⚠️ Invalid notification case info - Hospital: {hospital}, Patient: {patient}, ID: {patient_id}")
                        dump_page_html(page, "invalid_notification_info")
                        time.sleep(1)
                        continue

                    if case_is_blocked(hospital, patient, patient_id):
                        # Don't return yet — fall through to the dashboard check so a
                        # simultaneous non-blocked case can still be accepted. The
                        # popup exposes only Accept/Reject (no safe dismiss control),
                        # so we leave it alone rather than clicking Reject.
                        popup_case_blocked = True
                    else:
                        popup_accept.first.click(force=True)
                        log(f"✅ Accepted case!\n   Hospital: {hospital}\n   Patient: {patient}\n   Patient ID: {patient_id}")
                        dump_page_html(page, "accepted_popup")
                        write_case_accepted(hospital, patient, patient_id)
                        wait_for_acknowledge(hospital, patient, patient_id)
                        return "accepted"

            # Fall back to dashboard row Accept button (scoped to exclude the popup)
            btn = page.locator(dashboard_accept_selector)
            if btn.count() > 0:
                saw_accept_button = True
                time.sleep(1)
                hospital, patient, patient_id = extract_case_info(page)

                if not hospital or not patient or not patient_id:
                    log(f"⚠️ Invalid case info - Hospital: {hospital}, Patient: {patient}, ID: {patient_id}")
                    dump_page_html(page, "invalid_case_info")
                    time.sleep(1)
                    continue

                if case_is_blocked(hospital, patient, patient_id):
                    # Dashboard case is also blocked; nothing acceptable to act on.
                    return "ignored"

                btn.first.click(force=True)
                log(f"✅ Accepted case!\n   Hospital: {hospital}\n   Patient: {patient}\n   Patient ID: {patient_id}")
                dump_page_html(page, "accepted_dashboard")
                write_case_accepted(hospital, patient, patient_id)
                wait_for_acknowledge(hospital, patient, patient_id)
                return "accepted"

            # A blocked popup with no non-blocked dashboard case behind it: the case
            # is accounted for, so report "ignored" and keep watching. bot_loop calls
            # us again shortly, so a case that appears later is still caught.
            if popup_case_blocked:
                return "ignored"

            time.sleep(1)

        if saw_accept_button:
            log("⚠️ Accept button was visible but could not complete accept")
            dump_page_html(page, "failed_accept_credentialed")
            return "failed"
        else:
            log("💤 No Accept button (not credentialed for this case)")
            return "not_credentialed"
    except Exception as e:
        log(f"⚠️ Error in handle_new_case: {e}")
        dump_page_html(page, "handle_error")
        return "failed"


def get_case_count(page):
    """Get number of pending cases from badge, or 0 if none."""
    badge = page.locator("li.rescue-dashboard-container .rescue-dashboard-count")
    if badge.count() == 0:
        return 0
    text = (badge.text_content() or "").strip()
    return int(text) if text.isdigit() else 0


def interruptible_sleep(seconds):
    """Sleep that can be interrupted by shutdown signal."""
    for _ in range(int(seconds * 10)):
        if SHUTDOWN_REQUESTED:
            return
        time.sleep(0.1)


RESCUE_DASHBOARD_INDICATOR = "app-rescue-dashboard"


def _refresh_dashboard(page):
    """Navigate away from rescue dashboard and back to force Angular to rebuild the component.
    This ensures the table always shows fresh data from the API."""
    try:
        away_link = page.locator("li.waitingRoom")
        if away_link.count() > 0 and away_link.first.is_visible():
            away_link.first.click()
            time.sleep(2)
        rescue_link = page.locator(RESCUE_SELECTOR)
        if rescue_link.count() > 0 and rescue_link.first.is_visible():
            rescue_link.first.click()
            time.sleep(2)
    except Exception as e:
        log(f"⚠️ Dashboard refresh failed: {e}")


def bot_loop(page):
    last_state = None
    cases_without_popup = 0
    POPUP_FAILURE_THRESHOLD = 3
    log("👀 Bot running...")

    try:
        while not SHUTDOWN_REQUESTED:
            check_hard_timeout()

            _refresh_dashboard(page)

            if page.locator('input[name="identifier"]').count() > 0:
                log("⚠️ Detected login page. Session expired, exiting bot.")
                dump_page_html(page, "session_expired")
                send_notification("❌ Session expired while running. Please start the bot again.")
                return

            case_count = get_case_count(page)

            if case_count > 0:
                # Check if table is stale (badge shows cases but table is empty)
                has_rows = page.locator("div.complete-row").count() > 0
                if not has_rows:
                    log("⚠️ Badge shows cases but table is empty — retrying refresh")
                    _refresh_dashboard(page)
                    time.sleep(3)
                    has_rows = page.locator("div.complete-row").count() > 0
                    if not has_rows:
                        log("⚠️ Still no rows after retry — dashboard is broken")
                        dump_page_html(page, "dashboard_broken")
                        if not send_notification("❌ Dashboard is broken, the bot has shut down. A case was detected that you may be credentialed for, please check the dashboard manually."):
                            log("❌ Telegram failed. Exiting bot.")
                            sys.exit(1)
                        return

                if last_state != "has_cases":
                    log(f"🔔 New case detected: {case_count}")
                    dump_page_html(page, "new_case_detected")
                result = handle_new_case(page)
                if result == "accepted":
                    cases_without_popup = 0
                elif result == "ignored":
                    # Hospital is on the blocklist: leave the case alone and
                    # keep watching. Not a popup failure — we saw the case fine.
                    pass
                elif result == "failed":
                    if not send_notification("🚨 Credentialed case seen but unable to accept, please manually accept the case."):
                        log("❌ Telegram failed. Exiting bot.")
                        sys.exit(1)
                    cases_without_popup += 1
                    if cases_without_popup >= POPUP_FAILURE_THRESHOLD:
                        log(f"⚠️ {cases_without_popup} consecutive credentialed cases with no popup. Notification system may be dead.")
                        dump_page_html(page, "notification_system_dead")
                        if not send_notification(f"⚠️ {cases_without_popup} credentialed cases failed acceptance. Restarting bot to reconnect notifications."):
                            log("❌ Telegram failed. Exiting bot.")
                            sys.exit(1)
                        return
                # "not_credentialed" - don't count toward popup failure threshold
                last_state = "has_cases"
            else:
                if last_state != "no_cases":
                    log("💤 No cases")
                last_state = "no_cases"

            interruptible_sleep(2)
    except Exception as e:
        log(f"⚠️ Unhandled bot error: {e}")
        dump_page_html(page, "unhandled_error")


# ================= MAIN =================

def log_external_ip(playwright):
    """Log the external IP address for verification."""
    test_browser = None
    try:
        test_browser = playwright.chromium.launch(headless=True, args=["--disable-ipv6"])
        test_context = test_browser.new_context()
        test_page = test_context.new_page()
        test_page.goto("https://api.ipify.org", timeout=15000)
        ip = test_page.locator("body").text_content().strip()
        log(f"🌐 External IP: {ip}")
        test_browser.close()
    except Exception as e:
        log(f"⚠️ Could not determine external IP: {e}")
        if test_browser:
            test_browser.close()


with sync_playwright() as p:
    browser = None
    try:
        clear_case_files()
        log("🧹 Cleared stale case files from previous run.")

        log_external_ip(p)

        browser = p.chromium.launch(
            headless=True,
            args=["--disable-ipv6"]
        )

        context = browser.new_context()
        page = context.new_page()
        login(page)
        new_page = start_synapse(context, page)
        if not send_notification("🟢 Bot is now watching for rescue cases."):
            log("❌ Telegram failed. Exiting bot.")
            sys.exit(1)
        bot_loop(new_page)

    finally:
        if browser:
            log("🧹 Closing browser...")
            browser.close()
