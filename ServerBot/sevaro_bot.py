from playwright.sync_api import sync_playwright
import requests
import signal
import time
import os
import sys

sys.stdout.reconfigure(line_buffering=True)

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
        print(f"⛔ FAILSAFE: Bot exceeded max runtime ({MAX_RUNTIME_SECONDS}s). Forcing exit.", flush=True)
        sys.exit(1)


def handle_shutdown(signum, frame):
    """Handle SIGTERM/SIGINT for graceful shutdown."""
    global SHUTDOWN_REQUESTED
    print(f"🛑 Received signal {signum}, shutting down gracefully...", flush=True)
    SHUTDOWN_REQUESTED = True


def handle_timer_reset(signum, frame):
    """Handle SIGUSR1 to reset failsafe timer."""
    global BOT_START_TIME
    BOT_START_TIME = time.time()
    print("🔄 Failsafe timer reset.", flush=True)


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
        print("Telegram disabled (missing token/chat id)")
        return False

    print(f"📤 Sending Telegram: {msg}", flush=True)
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            data={"chat_id": TELEGRAM_CHAT_ID, "text": msg},
            timeout=10,
        )
        if r.ok:
            print(f"📱 Telegram sent: {msg}", flush=True)
            return True
        print(f"Telegram failed ({r.status_code}): {r.text}", flush=True)
        return False
    except Exception as e:
        print(f"Telegram error: {e}", flush=True)
        return False


def login(page):
    print("🔐 Login required")

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
    print("✅ Login successful")


def ensure_logged_in(page):
    page.goto(HOME_URL)
    time.sleep(3)

    if page.locator("text=Synapse").count() == 0:
        print("⚠️ Session expired or invalid. Logging in again...")
        login(page)
    else:
        print("🔐 Session valid")


def start_synapse(context, page):
    page.get_by_role("button", name="Synapse").click()

    launch_btn = page.locator('[data-se="app-settings-launch-app-button"]')
    launch_btn.wait_for(state="visible", timeout=10000)

    with context.expect_page() as new_page_info:
        launch_btn.click()

    synapse_page = new_page_info.value
    print("🚀 Synapse opened")

    synapse_page.wait_for_selector(RESCUE_SELECTOR, timeout=60000)
    synapse_page.locator(RESCUE_SELECTOR).click()
    print("🎯 Rescue Dashboard opened")

    return synapse_page


def get_text(locator):
    """Get text content from locator, or None if not found."""
    return locator.text_content().strip() if locator.count() > 0 else None


def extract_case_info(page):
    """Extract hospital name, patient name, and patient ID from the rescue case row."""
    try:
        case_row = page.locator('div.complete-row:has(button:has-text("Accept"))').first
        if case_row.count() == 0:
            print("⚠️ No case row with Accept button found")
            return None, None, None

        hospital = get_text(case_row.locator("div.facility-name div").first)
        patient = get_text(case_row.locator('div[data-dd-action-name="rescue-dashboard-patient-name"] span[data-dd-privacy="mask"] span[apptruncatepopover]').first)
        patient_id = get_text(case_row.locator('span[data-dd-action-name="rescue-dashboard-mrn"]').first)

        return hospital, patient, patient_id
    except Exception as e:
        print(f"⚠️ Error extracting case info: {e}")
        return None, None, None


def handle_new_case(page):
    """Handle a detected new case - reload, extract info, and accept if valid."""
    page.reload()
    page.wait_for_load_state("load", timeout=30000)
    page.wait_for_selector(RESCUE_SELECTOR, timeout=15000)
    page.locator(RESCUE_SELECTOR).click()
    time.sleep(3)

    for _ in range(10):
        accept_btn = page.locator('button:has-text("Accept")')
        if accept_btn.count() > 0:
            time.sleep(1)
            hospital, patient, patient_id = extract_case_info(page)

            if not all([hospital, patient, patient_id]):
                print(f"⚠️ Missing case info - Hospital: {hospital}, Patient: {patient}, ID: {patient_id}")
                print("⏭️ Ignoring notification (incomplete info)")
                return

            try:
                accept_btn.first.click(force=True)
                print(f"✅ Accepted case!\n   Hospital: {hospital}\n   Patient: {patient}\n   Patient ID: {patient_id}")
                if not send_notification(
                    f"🚨 Rescue case accepted!\n\n🏥 Hospital: {hospital}\n👤 Patient: {patient}\n🆔 Patient ID: {patient_id}"
                ):
                    print("❌ Telegram failed. Exiting bot.", flush=True)
                    sys.exit(1)
                return
            except Exception as e:
                print("⚠️ Accept click failed:", e)
        time.sleep(1)


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
    print("👀 Bot running...")

    try:
        while not SHUTDOWN_REQUESTED:
            # Failsafe: check hard timeout every loop iteration
            check_hard_timeout()

            if page.locator('input[name="identifier"]').count() > 0:
                print("⚠️ Detected login page. Session expired, exiting bot.")
                return

            case_count = get_case_count(page)

            if case_count > 0:
                print(f"🔔 New case detected: {case_count}")
                handle_new_case(page)
                last_state = "new_cases"
            elif last_state != "no_cases":
                print("💤 No cases")
                last_state = "no_cases"

            interruptible_sleep(2)
    except Exception as e:
        print("⚠️ Unhandled bot error:", e)


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
            print("🧹 Closing browser...")
            browser.close()
