import asyncio, json
from playwright.async_api import async_playwright

async def debug():
    pw = await async_playwright().start()
    browser = await pw.chromium.launch(headless=True, args=['--no-sandbox'])
    page = await browser.new_page(viewport={'width': 1280, 'height': 720})
    await page.goto('https://www.instagram.com/accounts/login/', wait_until='networkidle', timeout=60000)
    await asyncio.sleep(3)
    print(f'URL: {page.url}')
    print(f'Title: {await page.title()}')
    html = await page.content()
    print(f'HTML length: {len(html)}')
    inputs = await page.evaluate('() => { return Array.from(document.querySelectorAll("input")).map(i => ({name: i.name, type: i.type, placeholder: i.placeholder, aria_label: i.getAttribute("aria-label"), autocomplete: i.autocomplete, visible: i.offsetParent !== null, id: i.id})) }')
    print(f'Inputs: {json.dumps(inputs, indent=2, ensure_ascii=False)}')
    buttons = await page.evaluate('() => { return Array.from(document.querySelectorAll("button, [role=button]")).map(b => ({text: (b.innerText||"").substring(0, 50), type: b.type || b.getAttribute("role"), visible: b.offsetParent !== null})).filter(b => b.visible) }')
    print(f'Buttons: {json.dumps(buttons, indent=2, ensure_ascii=False)}')
    await page.screenshot(path='C:\\Users\\AISAR\\AppData\\Local\\Temp\\opencode\\ig_login.png')
    print('Screenshot saved')
    await browser.close()
    await pw.stop()

asyncio.run(debug())
