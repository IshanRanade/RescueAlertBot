from playwright.sync_api import sync_playwright
import requests
import signal
import time
import os
import sys
from datetime import datetime

sys.stdout.reconfigure(line_buffering=True)


def log(msg):
    """Print with timestamp."""
    timestamp = datetime.now().astimezone().strftime("%Y/%m/%d %H:%M:%S %Z")
    print(f"[{timestamp}] {msg}", flush=True)

# Global flag for graceful shutdown
SHUTDOWN_REQUESTED = False

# FAILSAFE: Hard max runtime (timer duration + 5 min buffer)
TIMER_DURATION = int(os.environ.get("TIMER_DURATION", 60 * 60))  # Default 1 hour
MAX_RUNTIME_SECONDS = TIMER_DURATION + 5 * 60  # Timer + 5 min buffer
BOT_START_TIME = time.time()


def check_hard_timeout():
    """Failsafe: Kill bot if it's been running too long (backup for timer)."""
    elapsed = time.time() - BOT_START_TIME
    if elapsed > MAX_RUNTIME_SECONDS:
        log(f"⛔ FAILSAFE: Bot exceeded max runtime ({MAX_RUNTIME_SECONDS}s). Forcing exit.")
        sys.exit(1)


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

STATE_FILE = "okta_state.json"
LOGIN_URL = "https://login.mysevaro.com"
HOME_URL = "https://login.mysevaro.com/app/UserHome"
RESCUE_SELECTOR = "li.rescue-dashboard-container a.nav-link"

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

    log(f"📤 Sending Telegram:\n{msg}")
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            data={"chat_id": TELEGRAM_CHAT_ID, "text": msg},
            timeout=10,
        )
        if r.ok:
            log("📱 Telegram sent")
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

    page.wait_for_selector("text=Synapse", timeout=60000)
    log("✅ Login successful")


def ensure_logged_in(page):
    page.goto(HOME_URL)
    time.sleep(3)

    if page.locator("text=Synapse").count() == 0:
        log("⚠️ Session expired or invalid. Logging in again...")
        login(page)
    else:
        log("🔐 Session valid")


def start_synapse(context, page):
    page.get_by_role("button", name="Synapse").click()

    launch_btn = page.locator('[data-se="app-settings-launch-app-button"]')
    launch_btn.wait_for(state="visible", timeout=10000)

    with context.expect_page() as new_page_info:
        launch_btn.click()

    synapse_page = new_page_info.value
    log("🚀 Synapse opened")

    synapse_page.wait_for_selector(RESCUE_SELECTOR, timeout=60000)
    synapse_page.locator(RESCUE_SELECTOR).click()
    log("🎯 Rescue Dashboard opened")

    return synapse_page


def get_text(locator):
    """Get text content from locator, or None if not found."""
    return locator.text_content().strip() if locator.count() > 0 else None


def extract_case_info(page):
    """Extract hospital name, patient name, and patient ID from the rescue case row."""
    try:
        case_row = page.locator('div.complete-row:has(button:has-text("Accept"))').first
        if case_row.count() == 0:
            log("⚠️ No case row with Accept button found")
            return None, None, None

        hospital = get_text(case_row.locator("div.facility-name div").first)
        patient = get_text(case_row.locator('div[data-dd-action-name="rescue-dashboard-patient-name"] span[data-dd-privacy="mask"] span[apptruncatepopover]').first)
        patient_id = get_text(case_row.locator('span[data-dd-action-name="rescue-dashboard-mrn"]').first)

        return hospital, patient, patient_id
    except Exception as e:
        log(f"⚠️ Error extracting case info: {e}")
        return None, None, None


def handle_new_case(page):
    """Handle a detected new case - reload, extract info, and accept if valid.
    Returns True if case was accepted, False otherwise."""
    try:
        page.reload()
        page.wait_for_load_state("load", timeout=30000)
        page.wait_for_selector(RESCUE_SELECTOR, timeout=15000)
        page.locator(RESCUE_SELECTOR).click()
        time.sleep(3)

        for attempt in range(10):
            accept_btn = page.locator('button:has-text("Accept")')
            if accept_btn.count() > 0:
                time.sleep(1)
                hospital, patient, patient_id = extract_case_info(page)

                # Validate all fields: must exist, be non-empty, and patient_id must be numeric
                if not hospital or not patient or not patient_id or not patient_id.isdigit():
                    log(f"⚠️ Invalid case info - Hospital: {hospital}, Patient: {patient}, ID: {patient_id}")
                    log("⏭️ Ignoring notification (incomplete or invalid info)")
                    return False

                try:
                    accept_btn.first.click(force=True)
                    log(f"✅ Accepted case!\n   Hospital: {hospital}\n   Patient: {patient}\n   Patient ID: {patient_id}")
                    if not send_notification(
                        f"🚨 Rescue case accepted!\n\n🏥 Hospital: {hospital}\n👤 Patient: {patient}\n🆔 Patient ID: {patient_id}"
                    ):
                        log("❌ Telegram failed. Exiting bot.")
                        sys.exit(1)
                    return True
                except Exception as e:
                    log(f"⚠️ Accept click failed (attempt {attempt + 1}): {e}")
            time.sleep(1)

        log("⚠️ Accept button not found after 10 attempts")
        return False
    except Exception as e:
        log(f"⚠️ Error in handle_new_case: {e}")
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
            # Failsafe: check hard timeout every loop iteration
            check_hard_timeout()

            if page.locator('input[name="identifier"]').count() > 0:
                log("⚠️ Detected login page. Session expired, exiting bot.")
                return

            case_count = get_case_count(page)

            if case_count > 0:
                log(f"🔔 New case detected: {case_count}")
                if handle_new_case(page):
                    last_state = "new_cases"
                else:
                    # Failed to handle case - wait before retrying to avoid hammering the page
                    log("⏳ Failed to handle case, waiting 10s before retrying...")
                    interruptible_sleep(10)
            elif last_state != "no_cases":
                log("💤 No cases")
                last_state = "no_cases"

            interruptible_sleep(2)
    except Exception as e:
        log(f"⚠️ Unhandled bot error: {e}")


# ================= MAIN =================

with sync_playwright() as p:
    browser = None
    try:
        browser = p.chromium.launch(headless=True)

        if os.path.exists(STATE_FILE) and os.path.getsize(STATE_FILE) > 0:
            context = browser.new_context(storage_state=STATE_FILE)
        else:
            context = browser.new_context()

        page = context.new_page()
        ensure_logged_in(page)
        new_page = start_synapse(context, page)
        context.storage_state(path=STATE_FILE)
        bot_loop(new_page)

    finally:
        if browser:
            log("🧹 Closing browser...")
            browser.close()
