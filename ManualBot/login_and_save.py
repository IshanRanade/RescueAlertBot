from playwright.sync_api import sync_playwright

STATE_FILE = "okta_state.json"

# Prompt for credentials
email = input("Enter your email: ")
password = input("Enter your password: ")
totp_code = input("Enter your Google Authenticator code: ")

with sync_playwright() as p:
    browser = p.chromium.launch(headless=False)
    context = browser.new_context()
    page = context.new_page()

    page.goto("https://login.mysevaro.com")

    # Fill email and click next
    page.fill('input[name="identifier"]', email)
    page.click('input.button.button-primary[type="submit"]')

    # Wait for password page and fill password
    page.wait_for_selector('input[type="password"]', timeout=15000)
    page.fill('input[type="password"]', password)
    page.click('input.button.button-primary[type="submit"]')

    # Wait for TOTP input
    totp_selector = 'input[name="otp"], input[name="credentials.passcode"], input[id*="totp"]'
    page.wait_for_selector(totp_selector, timeout=15000)

    # Type the code (fires real key events)
    page.click(totp_selector)  # make sure the field is focused
    page.type(totp_selector, totp_code, delay=50)  # slight delay between keys
    page.click('input.button.button-primary[type="submit"]')

    # Wait for final dashboard page
    page.wait_for_selector("text=Synapse", timeout=60000)

    # Save login state
    print("💾 Saving login state...")
    context.storage_state(path=STATE_FILE)
    print("✅ Login state saved to", STATE_FILE)

    input("Press Enter to close the browser...")
