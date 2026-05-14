from playwright.sync_api import sync_playwright
import requests
import signal
import time
import os
import sys
import json
from datetime import datetime, timezone, timedelta

sys.stdout.reconfigure(line_buffering=True)


PST = timezone(timedelta(hours=-8), name="PST")


def log(msg):
    """Print with timestamp."""
    timestamp = datetime.now(PST).strftime("%Y/%m/%d %H:%M:%S %Z")
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
OTP = os.environ.get("OTP")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")


def send_notification(msg):
    """Send Telegram notification. Returns True on success, False on failure."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log("Telegram disabled (missing token/chat id)")
        return False

    log(f"📤 Sending Telegram: {msg}")
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            data={"chat_id": TELEGRAM_CHAT_ID, "text": msg},
            timeout=10,
        )
        if r.ok:
            log(f"📱 Telegram sent:\n{msg}")
            return True
        log(f"Telegram failed ({r.status_code}): {r.text}")
        return False
    except Exception as e:
        log(f"Telegram error: {e}")
        return False


def login(page):
    log("🔐 Login required")

    page.goto(LOGIN_URL)
    page.fill('input[name="identifier"]', EMAIL)
    page.click('input.button.button-primary[type="submit"]')

    page.wait_for_selector('input[type="password"]', timeout=20000)
    page.fill('input[type="password"]', PASSWORD)
    page.click('input.button.button-primary[type="submit"]')

    totp_selector = 'input[name="otp"], input[name="credentials.passcode"], input[id*="totp"]'
    page.wait_for_selector(totp_selector, timeout=20000)

    page.click(totp_selector)
    page.type(totp_selector, OTP, delay=50)
    page.click('input.button.button-primary[type="submit"]')

    page.wait_for_selector(SYNAPSE_SELECTOR, timeout=60000)
    log("✅ Login successful")
    dump_page_html(page, "after_login")


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
SYNAPSE_MAX_RETRIES = 3


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


def dump_page_html(page, label="debug"):
    """Dump page HTML to a file for debugging."""
    try:
        ts = datetime.now(PST).strftime("%Y%m%d_%H%M%S")
        path = f"data/page_{label}_{ts}.html"
        html = page.content()
        with open(path, "w", encoding="utf-8") as f:
            f.write(html)
        log(f"📄 Page HTML dumped to {path}")
    except Exception as e:
        log(f"⚠️ Could not dump page HTML: {e}")


ACCEPT_MAX_RETRIES = 3


def _verify_accept_on_dashboard(page, patient_id, accept_selector):
    """Check the dashboard row to confirm a case was truly accepted.
    Returns True if the Accept button is gone from the row (case accepted)."""
    row_selector = f'div.complete-row:has(span[data-dd-action-name="rescue-dashboard-mrn"]:text-is("{patient_id}"))'
    row_btn = page.locator(f'{row_selector} {accept_selector}')
    if row_btn.count() > 0 and row_btn.first.is_visible():
        return False
    return True


def _accept_via_notification_popup(page):
    """Try to accept a case using the notification popup overlay.
    Returns (True, hospital, patient, patient_id) on success, (False, ...) on failure."""
    accept_selector = 'button:has-text("Accept")'
    popup = page.locator(NOTIFICATION_POPUP_SELECTOR)

    if popup.count() == 0:
        return False, None, None, None

    log("📢 Notification popup detected, using popup Accept button")
    hospital, patient, patient_id = extract_notification_case_info(page)

    if not hospital or not patient or not patient_id:
        log(f"⚠️ Invalid notification case info - Hospital: {hospital}, Patient: {patient}, ID: {patient_id}")
        dump_page_html(page, "invalid_notification_info")
        return False, None, None, None

    popup_accept = popup.locator(accept_selector)
    if popup_accept.count() == 0:
        log("⚠️ Notification popup has no Accept button")
        return False, None, None, None

    popup_accept.first.click(force=True)
    time.sleep(3)

    accepted = _verify_accept_on_dashboard(page, patient_id, accept_selector)
    if not accepted:
        log(f"⏳ Dashboard row still shows Accept for {patient_id} after popup click")
    return accepted, hospital, patient, patient_id


def _accept_via_dashboard_row(page):
    """Try to accept a case using the dashboard row Accept button.
    Returns (True, hospital, patient, patient_id) on success, (False, ...) on failure."""
    accept_selector = 'button:has-text("Accept")'

    hospital, patient, patient_id = extract_case_info(page)
    if not hospital or not patient or not patient_id:
        log(f"⚠️ Invalid case info - Hospital: {hospital}, Patient: {patient}, ID: {patient_id}")
        dump_page_html(page, "invalid_case_info")
        return False, None, None, None

    row_selector = f'div.complete-row:has(span[data-dd-action-name="rescue-dashboard-mrn"]:text-is("{patient_id}"))'
    case_row = page.locator(row_selector)
    case_btn = case_row.locator(accept_selector)

    if case_btn.count() == 0:
        log(f"⚠️ No Accept button in dashboard row for case {patient_id}")
        return False, hospital, patient, patient_id

    case_btn.first.click(force=True)
    time.sleep(3)

    accepted = _verify_accept_on_dashboard(page, patient_id, accept_selector)
    if not accepted:
        log(f"⏳ Accept button still visible for {patient_id} after dashboard row click")
    return accepted, hospital, patient, patient_id


def _reload_rescue_dashboard(page):
    """Reload the page and re-navigate to the Rescue Dashboard so the DOM is fresh."""
    page.reload()
    page.wait_for_load_state("load", timeout=30000)
    page.wait_for_selector(RESCUE_SELECTOR, state="visible", timeout=15000)
    page.locator(RESCUE_SELECTOR).click()
    time.sleep(3)


def handle_new_case(page):
    """Look for an Accept button on the page and click it.
    Tries the notification popup first (it overlays the dashboard with higher z-index),
    then falls back to the dashboard row. Retries up to ACCEPT_MAX_RETRIES times
    before notifying the user of failure.
    Returns True if case was accepted, False otherwise."""
    try:
        accept_selector = 'button:has-text("Accept")'

        # Check for notification popup before reloading (it may disappear on reload)
        popup_visible = page.locator(NOTIFICATION_POPUP_SELECTOR).count() > 0

        if not popup_visible:
            try:
                _reload_rescue_dashboard(page)
            except Exception as e:
                log(f"⚠️ Failed to reload rescue dashboard: {e}")
                dump_page_html(page, "reload_failed")

        try:
            page.wait_for_selector(accept_selector, state="visible", timeout=20000)
        except Exception:
            log("💤 No Accept button (not credentialed for this case)")
            return False

        time.sleep(1)

        last_hospital, last_patient, last_patient_id = None, None, None

        for attempt in range(1, ACCEPT_MAX_RETRIES + 1):
            if SHUTDOWN_REQUESTED:
                return False

            if attempt > 1:
                if page.locator(accept_selector).count() == 0:
                    log("ℹ️ Accept button gone before retry (case removed or accepted elsewhere)")
                    return False
                time.sleep(1)

            log(f"🔄 Accept attempt {attempt}/{ACCEPT_MAX_RETRIES}")

            accepted, hospital, patient, patient_id = _accept_via_notification_popup(page)
            if hospital:
                last_hospital, last_patient, last_patient_id = hospital, patient, patient_id

            if not accepted:
                if attempt == 1:
                    log("⬇️ Falling back to dashboard row Accept")
                accepted, hospital, patient, patient_id = _accept_via_dashboard_row(page)
                if hospital:
                    last_hospital, last_patient, last_patient_id = hospital, patient, patient_id

            if accepted:
                log(f"✅ Accepted case!\n   Hospital: {hospital}\n   Patient: {patient}\n   Patient ID: {patient_id}")
                write_case_accepted(hospital, patient, patient_id)
                wait_for_acknowledge(hospital, patient, patient_id)
                return True

        log(f"❌ Failed to accept case after {ACCEPT_MAX_RETRIES} attempts")
        dump_page_html(page, "accept_failed")
        if last_patient_id:
            if not send_notification(
                f"⚠️ Case appeared but was unable to accept. You need to manually accept case.\n\n"
                f"🏥 Hospital: {last_hospital}\n👤 Patient: {last_patient}\n🆔 Patient ID: {last_patient_id}"
            ):
                log("❌ Telegram failed. Exiting bot.")
                sys.exit(1)
        return False
    except Exception as e:
        log(f"⚠️ Error in handle_new_case: {e}")
        dump_page_html(page, "handle_error")
        return False


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


def bot_loop(page):
    last_state = None
    log("👀 Bot running...")

    try:
        while not SHUTDOWN_REQUESTED:
            check_hard_timeout()

            if page.locator('input[name="identifier"]').count() > 0:
                log("⚠️ Detected login page. Session expired, exiting bot.")
                send_notification("❌ Session expired while running. Please start the bot again.")
                return

            case_count = get_case_count(page)

            if case_count > 0:
                if last_state != "has_cases":
                    log(f"🔔 New case detected: {case_count}")
                if handle_new_case(page):
                    last_state = "has_cases"
                else:
                    log("⏳ Failed to handle case, waiting 10s before retrying...")
                    last_state = "has_cases"
                    interruptible_sleep(10)
            else:
                if last_state != "no_cases":
                    log("💤 No cases")
                last_state = "no_cases"

            interruptible_sleep(2)
    except Exception as e:
        log(f"⚠️ Unhandled bot error: {e}")


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
