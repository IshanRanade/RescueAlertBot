from playwright.sync_api import sync_playwright
import requests
import time
import os
import sys

sys.stdout.reconfigure(line_buffering=True)

STATE_FILE = "okta_state.json"

LOGIN_URL = "https://login.mysevaro.com"
HOME_URL = "https://login.mysevaro.com/app/UserHome"

# Telegram
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

EMAIL = os.environ.get("EMAIL")
PASSWORD = os.environ.get("PASSWORD")
OTP = os.environ.get("OTP")


def send_notification(msg):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")

    if not token or not chat_id:
        print("Telegram disabled (missing token/chat id)")
        return

    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data={"chat_id": chat_id, "text": msg},
            timeout=10,
        )
        print("📱 Telegram sent:", r.text)
    except Exception as e:
        print("Telegram error:", e)


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


def ensure_logged_in(context, page):
    page.goto(HOME_URL)
    time.sleep(3)

    if page.locator("text=Synapse").count() == 0:
        print("⚠️ Session expired or invalid. Logging in again...")
        login(page)
    else:
        print("🔐 Session valid")


def start_synapse(context, page):
    page.get_by_role("button", name="Synapse").click()

    launch_button = page.locator('[data-se="app-settings-launch-app-button"]')
    launch_button.wait_for(state="visible", timeout=10000)

    with context.expect_page() as new_page_info:
        launch_button.click()

    new_page = new_page_info.value
    print("🚀 Synapse opened")

    rescue_selector = 'li.rescue-dashboard-container a.nav-link'
    new_page.wait_for_selector(rescue_selector, timeout=60000)
    new_page.locator(rescue_selector).click()
    print("🎯 Rescue Dashboard opened")

    return new_page


def extract_case_info(new_page):
    """Extract hospital name, patient name, and patient ID from the rescue case row."""
    try:
        # Find the case row that contains the Accept button
        case_row = new_page.locator('div.complete-row:has(button:has-text("Accept"))').first
        
        if case_row.count() == 0:
            print("⚠️ No case row with Accept button found")
            return None, None, None
        
        # Extract hospital name from facility-name div
        hospital_el = case_row.locator('div.facility-name div').first
        hospital_name = hospital_el.text_content().strip() if hospital_el.count() > 0 else None
        
        # Extract patient name from rescue-dashboard-patient-name
        patient_name_el = case_row.locator('div[data-dd-action-name="rescue-dashboard-patient-name"] span[data-dd-privacy="mask"] span[apptruncatepopover]').first
        patient_name = patient_name_el.text_content().strip() if patient_name_el.count() > 0 else None
        
        # Extract patient ID (MRN) from rescue-dashboard-mrn
        patient_id_el = case_row.locator('span[data-dd-action-name="rescue-dashboard-mrn"]').first
        patient_id = patient_id_el.text_content().strip() if patient_id_el.count() > 0 else None
        
        return hospital_name, patient_name, patient_id
        
    except Exception as e:
        print(f"⚠️ Error extracting case info: {e}")
        return None, None, None


def bot_loop(new_page):
    rescue_selector = 'li.rescue-dashboard-container a.nav-link'
    check_interval = 2
    last_case_state = None

    print("👀 Bot running...")

    try:
        while True:
            # Detect if login page is shown (session expired)
            if new_page.locator('input[name="identifier"]').count() > 0:
                print("⚠️ Detected login page. Session expired, exiting bot.")
                sys.exit(1)

            badge = new_page.locator('li.rescue-dashboard-container .rescue-dashboard-count')
            new_cases = False
            if badge.count() > 0:
                text = badge.text_content()
                if text and text.strip().isdigit() and int(text.strip()) > 0:
                    new_cases = True
                    print(f"🔔 New case detected: {text}")

                    new_page.reload()
                    new_page.wait_for_load_state("load", timeout=30000)
                    new_page.wait_for_selector(rescue_selector, timeout=15000)
                    new_page.locator(rescue_selector).click()

                    time.sleep(3)

                    for _ in range(10):
                        accept_btn = new_page.locator('button:has-text("Accept")')
                        if accept_btn.count() > 0:
                            # Extract case info before accepting
                            time.sleep(1)  # Small delay before extraction
                            hospital_name, patient_name, patient_id = extract_case_info(new_page)
                            
                            # Only accept if all 3 pieces of information are found
                            if not hospital_name or not patient_name or not patient_id:
                                print(f"⚠️ Missing case info - Hospital: {hospital_name}, Patient: {patient_name}, ID: {patient_id}")
                                print("⏭️ Ignoring notification (incomplete info)")
                                break
                            
                            try:
                                accept_btn.first.click(force=True)
                                print("✅ Accepted case!")
                                print(f"   Hospital: {hospital_name}")
                                print(f"   Patient: {patient_name}")
                                print(f"   Patient ID: {patient_id}")
                                
                                notification_msg = (
                                    f"🚨 Rescue case accepted!\n\n"
                                    f"🏥 Hospital: {hospital_name}\n"
                                    f"👤 Patient: {patient_name}\n"
                                    f"🆔 Patient ID: {patient_id}"
                                )
                                send_notification(notification_msg)
                                break
                            except Exception as e:
                                print("⚠️ Accept click failed:", e)
                        time.sleep(1)

            # Print "No cases" only if state changed
            if not new_cases and last_case_state != "no_cases":
                print("💤 No cases")
                last_case_state = "no_cases"
            elif new_cases:
                last_case_state = "new_cases"

            time.sleep(check_interval)

    except Exception as e:
        print("⚠️ Unhandled bot error:", e)
        sys.exit(1)  # Exit bot process on any exception


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
        ensure_logged_in(context, page)
        new_page = start_synapse(context, page)
        context.storage_state(path=STATE_FILE)
        bot_loop(new_page)

    finally:
        if browser:
            print("🧹 Closing browser...")
            browser.close()
