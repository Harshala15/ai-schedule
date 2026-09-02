from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page()
    page.goto("https://account.windy.com/login", wait_until="domcontentloaded")
    page.wait_for_timeout(2000)
    inputs = page.locator("input").all()
    inputs[0].fill("code.vedanjaypower@gmail.com")
    inputs[1].fill("Code@123")
    page.locator("button.windy-button.w-full").first.click()
    page.wait_for_timeout(5000)
    page.screenshot(path="windy/login_feedback.png")
    text = page.locator("body").inner_text()
    print("PAGE TEXT:")
    print(text)
    browser.close()
