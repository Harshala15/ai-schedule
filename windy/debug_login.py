import time
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page()
    page.goto('https://account.windy.com/login', wait_until='domcontentloaded', timeout=30000)
    page.wait_for_timeout(3000)
    
    inputs = page.locator('input').all()
    inputs[0].fill('code.vedanjaypower@gmail.com')
    inputs[1].fill('Code@123')
    page.locator('button[type="submit"]').first.click()
    page.wait_for_timeout(5000)
    
    print('URL after submit:', page.url)
    page.screenshot(path='windy/login_result.png')
    
    body_text = page.locator('body').inner_text()
    print('Page Text After Submit:')
    print(body_text)
    browser.close()
