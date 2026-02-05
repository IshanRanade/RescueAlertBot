from playwright.sync_api import sync_playwright
import requests
import time
import os

STATE_FILE = "okta_state.json"

# Telegram configuration
TELEGRAM_BOT_TOKEN = "8160686991:AAGo5m6aj8BZ7kmfo4phgOojLGlyNkgkePg"  # Get from @BotFather
TELEGRAM_CHAT_ID = "8492458042"                                        # Get from getUpdates API

def send_notification(message):
    """Send push notification via Telegram"""
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        resp = requests.post(url, data={
            'chat_id': TELEGRAM_CHAT_ID,
            'text': message
        })
        if resp.json().get('ok'):
            print("📱 Telegram notification sent!")
            return True
        else:
            print(f"⚠️ Telegram failed: {resp.text}")
            return False
    except Exception as e:
        print(f"❌ Telegram failed: {e}")
        return False

with sync_playwright() as p:
    browser = p.chromium.launch(headless=False)

    if os.path.exists(STATE_FILE):
        context = browser.new_context(storage_state=STATE_FILE)
        print("🔐 Using saved login state")
    else:
        context = browser.new_context()
        print("⚠️ No saved state — login required")

    page = context.new_page()
    page.goto("https://login.mysevaro.com/app/UserHome")

    if page.locator("text=Synapse").count() == 0:
        print("⏳ Session expired — please log in again")
        page.reload()
        page.wait_for_selector("text=Synapse", timeout=0)
        context.storage_state(path=STATE_FILE)
        print("🔄 Login complete, state saved")

    print("✅ Logged in")

    page.get_by_role("button", name="Synapse").click()

    launch_button = page.locator('[data-se="app-settings-launch-app-button"]')
    launch_button.wait_for(state="visible", timeout=5000)

    with context.expect_page() as new_page_info:
        launch_button.click()
    new_page = new_page_info.value
    print("🚀 Synapse Launch App clicked, new tab opened!")

    rescue_selector = 'li.rescue-dashboard-container a.nav-link'
    try:
        new_page.wait_for_selector(rescue_selector, timeout=60000)
        new_page.locator(rescue_selector).click()
        print("🎯 Rescue Dashboard clicked!")
    except:
        print("❌ Rescue Dashboard element not found after 60s")

    time.sleep(3)

    print("👀 Starting monitoring loop...")
    check_interval = 2

    while True:
        try:
            notification_badge = new_page.locator('li.rescue-dashboard-container .rescue-dashboard-count')
            
            if notification_badge.count() > 0:
                badge_text = notification_badge.text_content()
                
                if badge_text and badge_text.strip().isdigit() and int(badge_text.strip()) > 0:
                    print(f"🔔 Notification detected: {badge_text.strip()} case(s)!")
                    
                    print("🔄 Refreshing page...")
                    new_page.reload()
                    new_page.wait_for_load_state("load", timeout=30000)
                    
                    new_page.wait_for_selector(rescue_selector, timeout=15000)
                    
                    print("🎯 Clicking Rescue Dashboard...")
                    new_page.locator(rescue_selector).click()
                    
                    time.sleep(3)
                    
                    try:
                        new_page.wait_for_selector('.rescue-dashboard-card .inner-body .complete-row', timeout=15000)
                        print("📋 Table rows loaded!")
                    except:
                        print("⚠️ No table rows found, trying anyway...")
                    
                    try:
                        accept_button = new_page.locator('button:has-text("Accept")')
                        
                        count = accept_button.count()
                        print(f"🔍 Found {count} Accept button(s)")
                        
                        if count > 0:
                            accept_button.first.wait_for(state="visible", timeout=10000)
                            accept_button.first.click(force=True)
                            print("✅ Accept button clicked!")
                            
                            # Send Telegram notification
                            send_notification("🚨 Rescue case accepted!")
                            
                            time.sleep(2)
                        else:
                            print("⚠️ No Accept button found after refresh")
                            
                    except Exception as e:
                        print(f"❌ Accept button error: {e}")
                else:
                    print(f"💤 No new cases (badge: {badge_text})")
            else:
                print("💤 No notification badge visible")
                
        except Exception as e:
            print(f"⚠️ Error during check: {e}")
        
        time.sleep(check_interval)