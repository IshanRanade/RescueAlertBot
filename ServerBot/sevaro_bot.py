from playwright.sync_api import sync_playwright
import requests
import time
import os

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
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            data={"chat_id": TELEGRAM_CHAT_ID, "text": msg},
            timeout=10,
        )
        print("📱 Telegram sent")
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
        login(page)
        context.storage_state(path=STATE_FILE)
        print("💾 Session saved")
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


def bot_loop(new_page):
    rescue_selector = 'li.rescue-dashboard-container a.nav-link'
    check_interval = 2

    print("👀 Bot running...")

    while True:
        try:
            badge = new_page.locator(
                'li.rescue-dashboard-container .rescue-dashboard-count'
            )

            if badge.count() > 0:
                text = badge.text_content()

                if text and text.strip().isdigit() and int(text.strip()) > 0:
                    print(f"🔔 New case detected: {text}")

                    new_page.reload()
                    new_page.wait_for_load_state("load", timeout=30000)
                    new_page.wait_for_selector(rescue_selector, timeout=15000)
                    new_page.locator(rescue_selector).click()

                    time.sleep(3)

                    for _ in range(10):
                        accept_btn = new_page.locator('button:has-text("Accept")')

                        if accept_btn.count() > 0:
                            try:
                                accept_btn.first.click(force=True)
                                print("✅ Accepted case!")
                                send_notification("🚨 Rescue case accepted!")
                                break
                            except:
                                pass

                        time.sleep(1)
            else:
                print("💤 No cases")
        except Exception as e:
            print("⚠️ Bot error:", e)

        time.sleep(check_interval)


# ================= MAIN =================

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)

    if os.path.exists(STATE_FILE):
        context = browser.new_context(storage_state=STATE_FILE)
    else:
        context = browser.new_context()

    page = context.new_page()
    ensure_logged_in(context, page)
    new_page = start_synapse(context, page)
    context.storage_state(path=STATE_FILE)
    bot_loop(new_page)
