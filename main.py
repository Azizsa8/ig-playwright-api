"""
Multi-platform Social Media API using Playwright browser automation.
Supports Instagram (login, challenge bypass, sessionid, post, media)
and LinkedIn (login, sessionid, post text, post image).
"""
import os
import json
import asyncio
import uuid
import re
import tempfile
from datetime import datetime
from typing import Optional, Dict, Any, List
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException, Form, UploadFile, File
from pydantic import BaseModel
from pydantic_settings import BaseSettings
import logging
logger = logging.getLogger("social-api")
logging.basicConfig(level=logging.INFO)

from playwright.async_api import async_playwright, Page


# ============================================================
# Configuration
# ============================================================
class Settings(BaseSettings):
    PORT: int = 8000
    HOST: str = "0.0.0.0"
    BROWSER_HEADLESS: bool = True
    BROWSER_ARGS: list = ["--no-sandbox", "--disable-setuid-sandbox",
                          "--disable-dev-shm-usage", "--disable-blink-features=AutomationControlled"]
    SESSION_DIR: str = "/app/sessions"
    SESSION_TTL_HOURS: int = 24
    IG_LOGIN_URL: str = "https://www.instagram.com/accounts/login/"
    IG_BASE_URL: str = "https://www.instagram.com"
    LI_LOGIN_URL: str = "https://www.linkedin.com/login"
    LI_BASE_URL: str = "https://www.linkedin.com"
    REQUEST_DELAY_MS: int = 2000

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"

settings = Settings()


# ============================================================
# Stealth Injection Script
# ============================================================
STEALTH_SCRIPT = """
// Override webdriver flag
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });

// Chrome runtime mock
window.chrome = { runtime: {} };

// Block notification permission query
const _origQuery = window.navigator.permissions.query;
window.navigator.permissions.query = (params) =>
    params.name === 'notifications'
        ? Promise.resolve({ state: 'denied' })
        : _origQuery(params);

// Plugins array
Object.defineProperty(navigator, 'plugins', {
    get: () => [1, 2, 3, 4, 5],
});

// Languages
Object.defineProperty(navigator, 'languages', {
    get: () => ['en-US', 'en'],
});

// Canvas noise (~0.5% of pixels)
const _toDataURL = HTMLCanvasElement.prototype.toDataURL;
HTMLCanvasElement.prototype.toDataURL = function (type) {
    const ctx = this.getContext('2d');
    if (ctx) {
        const img = ctx.getImageData(0, 0, this.width, this.height);
        for (let i = 0; i < img.data.length; i += 4) {
            if (Math.random() < 0.005) {
                img.data[i] ^= 1;
            }
        }
        ctx.putImageData(img, 0, 0);
    }
    return _toDataURL.apply(this, arguments);
};
"""


async def apply_stealth(page: Page):
    await page.add_init_script(STEALTH_SCRIPT)


# ============================================================
# Common helpers
# ============================================================
async def random_delay(ms: int = 0):
    delay = ms or settings.REQUEST_DELAY_MS
    await asyncio.sleep(delay / 1000)


async def download_image(url: str) -> str:
    async with httpx.AsyncClient() as client:
        resp = await client.get(url, timeout=30.0)
        resp.raise_for_status()
        f = tempfile.NamedTemporaryFile(delete=False, suffix=".jpg")
        f.write(resp.content)
        tmp = f.name
        f.close()
        return tmp


# ============================================================
# Session Management
# ============================================================
class SessionManager:
    def __init__(self):
        self.sessions: Dict[str, Dict] = {}
        self.playwright = None
        os.makedirs(settings.SESSION_DIR, exist_ok=True)

    async def startup(self):
        self.playwright = await async_playwright().start()
        logger.info("Playwright started")

    async def shutdown(self):
        for sid, sess in list(self.sessions.items()):
            try:
                if sess.get("context"):
                    await sess["context"].close()
            except Exception:
                pass
        self.sessions.clear()
        if self.playwright:
            await self.playwright.stop()
        logger.info("Playwright stopped")

    def _session_path(self, sid: str) -> str:
        return os.path.join(settings.SESSION_DIR, sid)

    async def create_session(self, session_id: Optional[str] = None) -> str:
        sid = session_id or str(uuid.uuid4())
        context = await self.playwright.chromium.launch_persistent_context(
            user_data_dir=self._session_path(sid),
            headless=settings.BROWSER_HEADLESS,
            args=settings.BROWSER_ARGS,
            viewport={"width": 1280, "height": 720},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/130.0.0.0 Safari/537.36"
            ),
            locale="en-US",
            timezone_id="America/New_York",
        )
        self.sessions[sid] = {
            "context": context,
            "created_at": datetime.utcnow(),
            "page": None,
            "ig": {"logged_in": False, "username": None},
            "li": {"logged_in": False, "username": None},
        }
        logger.info(f"Created session: {sid}")
        return sid

    async def get_session(self, session_id: str) -> Dict:
        if session_id not in self.sessions:
            await self._restore_session(session_id)
        if session_id not in self.sessions:
            raise HTTPException(404, f"Session not found: {session_id}")
        return self.sessions[session_id]

    async def _restore_session(self, session_id: str) -> Dict:
        ctx = await self.playwright.chromium.launch_persistent_context(
            user_data_dir=self._session_path(session_id),
            headless=settings.BROWSER_HEADLESS,
            args=settings.BROWSER_ARGS,
            viewport={"width": 1280, "height": 720},
        )
        self.sessions[session_id] = {
            "context": ctx,
            "created_at": datetime.utcnow(),
            "page": None,
            "ig": {"logged_in": None, "username": None},
            "li": {"logged_in": None, "username": None},
        }
        logger.info(f"Restored session: {session_id}")
        return self.sessions[session_id]

    async def get_page(self, session_id: str) -> Page:
        sess = await self.get_session(session_id)
        if sess["page"] is None or sess["page"].is_closed():
            sess["page"] = await sess["context"].new_page()
        return sess["page"]

    async def close_session(self, session_id: str):
        if session_id in self.sessions:
            await self.sessions[session_id]["context"].close()
            del self.sessions[session_id]
            logger.info(f"Closed session: {session_id}")

    async def cleanup_expired(self):
        now = datetime.utcnow()
        expired = [sid for sid, s in self.sessions.items()
                   if (now - s["created_at"]).total_seconds() > settings.SESSION_TTL_HOURS * 3600]
        for sid in expired:
            await self.close_session(sid)


session_manager = SessionManager()


# ============================================================
# Instagram Client
# ============================================================
class InstagramClient:
    def __init__(self, page: Page):
        self.page = page

    async def goto(self, url: str):
        await self.page.goto(url, wait_until="networkidle", timeout=60000)
        await random_delay()

    async def is_logged_in(self) -> bool:
        try:
            await self.page.wait_for_selector(
                'header [role="button"][aria-label*="Profile"], '
                'a[href*="/accounts/logout/"], '
                'svg[aria-label="Home"]',
                timeout=5000,
            )
            return True
        except Exception:
            return False

    async def login_by_sessionid(self, sessionid: str) -> Dict[str, Any]:
        """Inject sessionid cookie to restore login without password."""
        await self.goto("https://www.instagram.com/")
        await self.page.context.add_cookies([
            {"name": "sessionid", "value": sessionid, "domain": ".instagram.com", "path": "/"},
            {"name": "ig_did", "value": str(uuid.uuid4()).replace("-", "")[:16],
             "domain": ".instagram.com", "path": "/"},
        ])
        # Refresh to pick up cookies
        await self.goto("https://www.instagram.com/")
        logged_in = await self.is_logged_in()
        if logged_in:
            csrftoken = None
            cookies = await self.page.context.cookies()
            for c in cookies:
                if c["name"] == "csrftoken":
                    csrftoken = c["value"]
                    break
            return {"success": True, "sessionid": sessionid, "csrftoken": csrftoken}
        return {"error": "Sessionid expired or invalid", "success": False}

    async def login(self, username: str, password: str) -> Dict[str, Any]:
        await self.goto(settings.IG_LOGIN_URL)

        await self.page.fill('input[name="email"]', username)
        await self.page.fill('input[name="pass"]', password)
        await random_delay()

        await self.page.click('div[role="button"]:has-text("Log in")')
        await self.page.wait_for_load_state("networkidle", timeout=30000)

        challenge = await self._detect_challenge()
        if challenge:
            bloks = await self._try_bloks_bypass()
            if bloks:
                logger.info("Bloks challenge bypass succeeded")
                await self.page.wait_for_load_state("networkidle", timeout=15000)
            else:
                return {"challenge": True, **challenge}

        logged_in = await self.is_logged_in()
        if not logged_in:
            error = await self._get_login_error()
            return {"error": error or "Login failed", "success": False}

        cookies = await self.page.context.cookies()
        sessionid = next((c["value"] for c in cookies if c["name"] == "sessionid"), None)
        csrftoken = next((c["value"] for c in cookies if c["name"] == "csrftoken"), None)
        return {"success": True, "sessionid": sessionid, "csrftoken": csrftoken, "username": username}

    async def _detect_challenge(self) -> Optional[Dict]:
        selectors = [
            'div[role="dialog"]:has-text("Challenge")',
            'div[role="dialog"]:has-text("Verify")',
            'div[role="dialog"]:has-text("Confirm")',
            'form[id*="challenge"]',
        ]
        for sel in selectors:
            try:
                el = await self.page.wait_for_selector(sel, timeout=3000)
                if el:
                    text = await el.inner_text()
                    html = await el.inner_html()
                    return {"challenge_type": "manual" if "code" in text.lower() else "unknown",
                            "message": text[:500], "html": html[:1000]}
            except Exception:
                continue

        # Check IP-block messages
        try:
            el = await self.page.wait_for_selector(
                'div[role="alert"], .x1lliihq, [id*="error"]', timeout=3000)
            if el:
                text = await el.inner_text()
                if "email" in text.lower() or "block" in text.lower():
                    return {"challenge_type": "ip_block", "message": text[:500],
                            "requires_new_ip": True}
        except Exception:
            pass
        return None

    async def _try_bloks_bypass(self) -> bool:
        """
        Attempt programmatic Bloks challenge bypass (PR #2652 technique).
        Extracts challenge_context from the page and POSTs choice:0.
        """
        try:
            # Look for challenge context in page content
            html = await self.page.content()
            match = re.search(r'"challenge_context"\s*:\s*"([^"]+)"', html)
            if not match:
                match = re.search(r'challengeContext\s*=\s*["\']([^"\']+)["\']', html)
            context = match.group(1) if match else None
            if not context:
                logger.info("No Bloks challenge context found on page")
                return False

            # Extract csrftoken from cookies or meta
            csrf = None
            cookies = await self.page.context.cookies()
            for c in cookies:
                if c["name"] == "csrftoken":
                    csrf = c["value"]
                    break
            if not csrf:
                meta = await self.page.query_selector('meta[name="csrf-token"]')
                if meta:
                    csrf = await meta.get_attribute("content")
            if not csrf:
                csrf = "missing"

            # POST to Bloks challenge take_challenge endpoint
            from playwright.async_api import expect
            url = "https://i.instagram.com/api/v1/bloks/apps/com.instagram.challenge.navigation.take_challenge/"
            payload = {"challenge_context": context, "choice": "0"}

            resp = await self.page.evaluate(
                """async (url, payload, csrf) => {
                    const r = await fetch(url, {
                        method: 'POST',
                        headers: {
                            'Content-Type': 'application/x-www-form-urlencoded',
                            'X-CSRFToken': csrf,
                            'X-Requested-With': 'XMLHttpRequest',
                        },
                        body: new URLSearchParams(payload).toString(),
                    });
                    const text = await r.text();
                    return { status: r.status, body: text.substring(0, 500) };
                }""",
                url, payload, csrf
            )
            logger.info(f"Bloks bypass response: {resp}")
            return resp.get("status") in (200, 201)
        except Exception as e:
            logger.warning(f"Bloks bypass failed: {e}")
            return False

    async def _get_login_error(self) -> Optional[str]:
        for sel in ['div[role="alert"]', '.x1lliihq', '[id*="error"]',
                     'p:has-text("Sorry")', 'p:has-text("incorrect")']:
            try:
                el = await self.page.wait_for_selector(sel, timeout=2000)
                if el:
                    text = (await el.inner_text()).strip()
                    if text:
                        return text
            except Exception:
                pass
        return None

    async def resolve_challenge(self, security_code: str) -> Dict:
        try:
            inp = await self.page.wait_for_selector(
                'input[name="security_code"], input[name="code"], '
                'input[autocomplete="one-time-code"]', timeout=5000)
            await inp.fill(security_code)
            await random_delay()
            btn = await self.page.wait_for_selector(
                'div[role="button"]:has-text("Confirm"), '
                'div[role="button"]:has-text("Submit"), '
                'button[type="submit"]', timeout=5000)
            await btn.click()
            await self.page.wait_for_load_state("networkidle", timeout=30000)
            ok = await self.is_logged_in()
            return {"success": ok, "logged_in": ok}
        except Exception as e:
            return {"error": str(e), "success": False}

    async def upload_photo(self, image_path: str, caption: str = "") -> Dict:
        await self.goto(settings.IG_BASE_URL + "/")
        for sel in ['svg[aria-label="New post"]', 'a[href="/create/"]',
                     'div[role="button"]:has-text("Create")']:
            try:
                await self.page.click(sel, timeout=3000)
                break
            except Exception:
                continue
        await self.page.wait_for_selector('input[type="file"]', timeout=10000)
        await (await self.page.query_selector('input[type="file"]')).set_input_files(image_path)
        await self.page.wait_for_load_state("networkidle", timeout=30000)
        for _ in range(2):
            btn = await self.page.wait_for_selector(
                'button:has-text("Next"), div[role="button"]:has-text("Next")', timeout=10000)
            await btn.click()
            await random_delay()
        if caption:
            ta = await self.page.wait_for_selector(
                'textarea[aria-label="Write a caption..."], '
                'textarea[placeholder*="caption"]', timeout=5000)
            await ta.fill(caption)
            await random_delay()
        share = await self.page.wait_for_selector(
            'button:has-text("Share"), div[role="button"]:has-text("Share")', timeout=10000)
        await share.click()
        await self.page.wait_for_load_state("networkidle", timeout=30000)
        return {"success": True}

    async def get_profile_info(self) -> Dict:
        await self.goto(settings.IG_BASE_URL + "/")
        try:
            link = await self.page.wait_for_selector('header a[href^="/"][href$="/"]', timeout=5000)
            href = await link.get_attribute("href")
            username = href.strip("/") if href else None
        except Exception:
            username = None
        return {"username": username, "logged_in": await self.is_logged_in()}

    async def get_media(self, username: str, amount: int = 12) -> List[Dict]:
        await self.goto(f"{settings.IG_BASE_URL}/{username}/")
        items = []
        for _ in range(3):
            posts = await self.page.query_selector_all('article a[href*="/p/"]')
            for p in posts:
                href = await p.get_attribute("href")
                if href and href not in [m.get("url") for m in items]:
                    items.append({"url": f"{settings.IG_BASE_URL}{href}"})
            if len(items) >= amount:
                break
            await self.page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await asyncio.sleep(2)
        return items[:amount]


# ============================================================
# LinkedIn Client
# ============================================================
class LinkedInClient:
    def __init__(self, page: Page):
        self.page = page

    async def goto(self, url: str):
        await self.page.goto(url, wait_until="networkidle", timeout=60000)
        await random_delay()

    async def is_logged_in(self) -> bool:
        try:
            await self.page.wait_for_selector(
                '#feed-nav, .feed-identity-module, '
                'a[href*="/feed/"], .global-nav__me', timeout=5000)
            return True
        except Exception:
            return False

    async def login_by_sessionid(self, li_at: str) -> Dict[str, Any]:
        """Inject li_at cookie to restore LinkedIn session."""
        await self.goto(settings.LI_BASE_URL + "/")
        await self.page.context.add_cookies([
            {"name": "li_at", "value": li_at, "domain": ".linkedin.com", "path": "/"},
            {"name": "lang", "value": "v=2&lang=en-us", "domain": ".linkedin.com", "path": "/"},
        ])
        await self.goto(settings.LI_BASE_URL + "/feed/")
        logged_in = await self.is_logged_in()
        if logged_in:
            return {"success": True}
        return {"error": "li_at cookie expired or invalid", "success": False}

    async def login(self, email: str, password: str) -> Dict[str, Any]:
        await self.goto(settings.LI_LOGIN_URL)
        await self.page.fill('input[name="session_key"]', email)
        await self.page.fill('input[name="session_password"]', password)
        await random_delay()
        await self.page.click('button[type="submit"]')
        await self.page.wait_for_load_state("networkidle", timeout=30000)

        # Check for challenge
        try:
            pin = await self.page.wait_for_selector(
                'input[name="pin"], input[autocomplete="one-time-code"]', timeout=3000)
            if pin:
                return {"challenge": True, "challenge_type": "2fa",
                        "message": "PIN verification required"}
        except Exception:
            pass

        logged_in = await self.is_logged_in()
        if not logged_in:
            error = await self._get_login_error()
            return {"error": error or "Login failed", "success": False}

        cookies = await self.page.context.cookies()
        li_at = next((c["value"] for c in cookies if c["name"] == "li_at"), None)
        return {"success": True, "li_at": li_at}

    async def _get_login_error(self) -> Optional[str]:
        for sel in ['#error-for-password', '.form__error', '[role="alert"]']:
            try:
                el = await self.page.wait_for_selector(sel, timeout=2000)
                if el:
                    text = (await el.inner_text()).strip()
                    if text:
                        return text
            except Exception:
                pass
        return None

    async def resolve_2fa(self, pin: str) -> Dict:
        try:
            inp = await self.page.wait_for_selector(
                'input[name="pin"], input[autocomplete="one-time-code"]', timeout=10000)
            await inp.fill(pin)
            await random_delay()
            btn = await self.page.wait_for_selector(
                'button[type="submit"]:has-text("Submit"), '
                'button[type="submit"]:has-text("Verify")', timeout=5000)
            await btn.click()
            await self.page.wait_for_load_state("networkidle", timeout=30000)
            ok = await self.is_logged_in()
            if ok:
                cookies = await self.page.context.cookies()
                li_at = next((c["value"] for c in cookies if c["name"] == "li_at"), None)
                return {"success": True, "logged_in": True, "li_at": li_at}
            return {"error": "2FA verification failed", "success": False}
        except Exception as e:
            return {"error": str(e), "success": False}

    async def post_text(self, text: str) -> Dict:
        """Post a text update to LinkedIn feed."""
        await self.goto(settings.LI_BASE_URL + "/feed/")
        try:
            # Click "Start a post" / share box
            share_box = await self.page.wait_for_selector(
                'div[role="button"]:has-text("Start a post"), '
                '.share-box__open, div[data-control-name="create_post"]', timeout=10000)
            await share_box.click()
            await random_delay(1000)

            # Wait for the editor and type text
            editor = await self.page.wait_for_selector(
                'div[role="textbox"][aria-label*="What do you want"], '
                'div[role="textbox"][contenteditable="true"]', timeout=10000)
            await editor.fill(text)
            await random_delay()

            # Click Post
            post_btn = await self.page.wait_for_selector(
                'button[type="submit"]:has-text("Post"), '
                'button:has-text("Post")', timeout=5000)
            await post_btn.click()
            await self.page.wait_for_load_state("networkidle", timeout=15000)
            return {"success": True}
        except Exception as e:
            return {"error": str(e), "success": False}

    async def post_image(self, image_path: str, text: str = "") -> Dict:
        """Post an image to LinkedIn feed."""
        await self.goto(settings.LI_BASE_URL + "/feed/")
        try:
            share_box = await self.page.wait_for_selector(
                'div[role="button"]:has-text("Start a post"), '
                '.share-box__open, div[data-control-name="create_post"]', timeout=10000)
            await share_box.click()
            await random_delay(1000)

            # Click image icon
            img_btn = await self.page.wait_for_selector(
                'button[aria-label*="image"], button[aria-label*="photo"], '
                'button[aria-label*="media"], li[data-control-name="media_upload"] button',
                timeout=10000)
            await img_btn.click()
            await random_delay()

            # Upload file
            file_input = await self.page.wait_for_selector(
                'input[type="file"][accept*="image"]', timeout=10000)
            await file_input.set_input_files(image_path)
            await self.page.wait_for_load_state("networkidle", timeout=30000)

            # Add text if provided
            if text:
                editor = await self.page.wait_for_selector(
                    'div[role="textbox"][aria-label*="What do you want"], '
                    'div[role="textbox"][contenteditable="true"]', timeout=10000)
                await editor.fill(text)
                await random_delay()

            # Post
            post_btn = await self.page.wait_for_selector(
                'button[type="submit"]:has-text("Post"), '
                'button:has-text("Post")', timeout=5000)
            await post_btn.click()
            await self.page.wait_for_load_state("networkidle", timeout=30000)
            return {"success": True}
        except Exception as e:
            return {"error": str(e), "success": False}

    async def get_profile_info(self) -> Dict:
        await self.goto(settings.LI_BASE_URL + "/feed/")
        try:
            me = await self.page.wait_for_selector(
                '.global-nav__me, a[href*="/in/"]', timeout=5000)
            text = await me.inner_text()
            return {"username": text.strip()[:100], "logged_in": await self.is_logged_in()}
        except Exception:
            return {"username": None, "logged_in": await self.is_logged_in()}


# ============================================================
# Pydantic Models
# ============================================================
class SessionResponse(BaseModel):
    session_id: str
    platform: str = "instagram"
    logged_in: bool = False
    username: Optional[str] = None
    challenge: Optional[Dict] = None
    error: Optional[str] = None
    sessionid: Optional[str] = None
    li_at: Optional[str] = None


# ============================================================
# FastAPI App
# ============================================================
@asynccontextmanager
async def lifespan(app: FastAPI):
    await session_manager.startup()
    yield
    await session_manager.shutdown()

app = FastAPI(
    title="Social Media Playwright API",
    description="Playwright-based automation for Instagram and LinkedIn.",
    version="2.0.0",
    lifespan=lifespan,
)


# ============================================================
# Common Endpoints
# ============================================================
@app.get("/health")
async def health():
    return {"status": "ok"}

@app.get("/sessions")
async def list_sessions():
    return {
        "sessions": [
            {
                "session_id": sid,
                "ig": s.get("ig"),
                "li": s.get("li"),
                "created_at": s["created_at"].isoformat(),
            }
            for sid, s in session_manager.sessions.items()
        ]
    }


# ============================================================
# Instagram Endpoints
# ============================================================

@app.post("/auth/login", response_model=SessionResponse)
async def ig_login(
    username: str = Form(...),
    password: str = Form(...),
    session_id: Optional[str] = Form(None),
):
    """Login to Instagram with username/password."""
    try:
        if session_id:
            session = await session_manager.get_session(session_id)
        else:
            session_id = await session_manager.create_session()
            session = session_manager.sessions[session_id]

        page = await session_manager.get_page(session_id)
        await apply_stealth(page)
        ig = InstagramClient(page)
        result = await ig.login(username, password)

        if result.get("challenge"):
            session["ig"]["challenge_data"] = result
            return SessionResponse(session_id=session_id, challenge=result, logged_in=False)

        if result.get("success"):
            session["ig"]["logged_in"] = True
            session["ig"]["username"] = result.get("username")
            return SessionResponse(
                session_id=session_id, logged_in=True,
                username=result.get("username"),
                sessionid=result.get("sessionid"),
            )

        return SessionResponse(
            session_id=session_id, logged_in=False,
            error=result.get("error", "Login failed"))
    except Exception as e:
        logger.exception("IG login error")
        raise HTTPException(500, str(e))


@app.post("/auth/sessionid", response_model=SessionResponse)
async def ig_sessionid(
    sessionid: str = Form(...),
    session_id: Optional[str] = Form(None),
):
    """Login to Instagram by injecting sessionid cookie (no password)."""
    try:
        if session_id:
            session = await session_manager.get_session(session_id)
        else:
            session_id = await session_manager.create_session()
            session = session_manager.sessions[session_id]

        page = await session_manager.get_page(session_id)
        await apply_stealth(page)
        ig = InstagramClient(page)
        result = await ig.login_by_sessionid(sessionid)

        if result.get("success"):
            session["ig"]["logged_in"] = True
            return SessionResponse(
                session_id=session_id, logged_in=True,
                sessionid=sessionid)
        return SessionResponse(
            session_id=session_id, logged_in=False,
            error=result.get("error", "Invalid sessionid"))
    except Exception as e:
        logger.exception("IG sessionid error")
        raise HTTPException(500, str(e))


@app.post("/auth/challenge/resolve", response_model=SessionResponse)
async def ig_challenge_resolve(
    session_id: str = Form(...),
    security_code: str = Form(...),
):
    """Resolve Instagram challenge with security code."""
    try:
        session = await session_manager.get_session(session_id)
        page = await session_manager.get_page(session_id)
        ig = InstagramClient(page)
        result = await ig.resolve_challenge(security_code)

        if result.get("success") and result.get("logged_in"):
            session["ig"]["logged_in"] = True
            session["ig"]["challenge_data"] = None
            return SessionResponse(session_id=session_id, logged_in=True,
                                   username=session["ig"].get("username"))
        return SessionResponse(session_id=session_id, logged_in=False,
                               error=result.get("error", "Challenge failed"))
    except Exception as e:
        logger.exception("IG challenge error")
        raise HTTPException(500, str(e))


@app.get("/auth/settings")
async def ig_auth_settings(session_id: str):
    """Check Instagram login status."""
    try:
        session = await session_manager.get_session(session_id)
        page = await session_manager.get_page(session_id)
        ig = InstagramClient(page)
        profile = await ig.get_profile_info()
        return {
            "logged_in": profile["logged_in"],
            "username": profile.get("username") or session["ig"].get("username"),
            "session_id": session_id,
            "platform": "instagram",
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/photo/upload")
async def ig_photo_upload(
    session_id: str = Form(...),
    caption: str = Form(""),
    image_url: str = Form(...),
):
    """Upload a photo to Instagram."""
    try:
        session = await session_manager.get_session(session_id)
        if not session["ig"].get("logged_in"):
            raise HTTPException(401, "Not logged in to Instagram")
        page = await session_manager.get_page(session_id)
        ig = InstagramClient(page)
        tmp = await download_image(image_url)
        try:
            return await ig.upload_photo(tmp, caption)
        finally:
            try:
                os.unlink(tmp)
            except Exception:
                pass
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("IG photo upload error")
        raise HTTPException(500, str(e))


@app.get("/user/posts")
async def ig_user_posts(
    session_id: str,
    username: Optional[str] = None,
    amount: int = 12,
):
    """Get recent Instagram posts."""
    try:
        session = await session_manager.get_session(session_id)
        if not session["ig"].get("logged_in"):
            raise HTTPException(401, "Not logged in to Instagram")
        page = await session_manager.get_page(session_id)
        ig = InstagramClient(page)
        target = username or session["ig"].get("username")
        if not target:
            raise HTTPException(400, "Username required")
        media = await ig.get_media(target, amount)
        return {"items": media}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))


@app.get("/account/info")
async def ig_account_info(session_id: str):
    """Get Instagram account info."""
    try:
        session = await session_manager.get_session(session_id)
        if not session["ig"].get("logged_in"):
            raise HTTPException(401, "Not logged in to Instagram")
        page = await session_manager.get_page(session_id)
        ig = InstagramClient(page)
        profile = await ig.get_profile_info()
        return {
            "username": profile.get("username") or session["ig"].get("username"),
            "logged_in": True,
            "platform": "instagram",
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/auth/logout")
async def ig_logout(session_id: str):
    """Close a session."""
    await session_manager.close_session(session_id)
    return {"success": True}


# ============================================================
# LinkedIn Endpoints
# ============================================================

@app.post("/linkedin/auth/login", response_model=SessionResponse)
async def li_login(
    email: str = Form(...),
    password: str = Form(...),
    session_id: Optional[str] = Form(None),
):
    """Login to LinkedIn with email/password."""
    try:
        if session_id:
            session = await session_manager.get_session(session_id)
        else:
            session_id = await session_manager.create_session()
            session = session_manager.sessions[session_id]

        page = await session_manager.get_page(session_id)
        await apply_stealth(page)
        li = LinkedInClient(page)
        result = await li.login(email, password)

        if result.get("challenge"):
            session["li"]["challenge_data"] = result
            return SessionResponse(
                session_id=session_id, platform="linkedin",
                challenge=result, logged_in=False)

        if result.get("success"):
            session["li"]["logged_in"] = True
            return SessionResponse(
                session_id=session_id, platform="linkedin",
                logged_in=True, li_at=result.get("li_at"))

        return SessionResponse(
            session_id=session_id, platform="linkedin",
            logged_in=False, error=result.get("error", "Login failed"))
    except Exception as e:
        logger.exception("LI login error")
        raise HTTPException(500, str(e))


@app.post("/linkedin/auth/sessionid", response_model=SessionResponse)
async def li_sessionid(
    li_at: str = Form(...),
    session_id: Optional[str] = Form(None),
):
    """Login to LinkedIn by injecting li_at cookie."""
    try:
        if session_id:
            session = await session_manager.get_session(session_id)
        else:
            session_id = await session_manager.create_session()
            session = session_manager.sessions[session_id]

        page = await session_manager.get_page(session_id)
        await apply_stealth(page)
        li = LinkedInClient(page)
        result = await li.login_by_sessionid(li_at)

        if result.get("success"):
            session["li"]["logged_in"] = True
            return SessionResponse(
                session_id=session_id, platform="linkedin",
                logged_in=True, li_at=li_at)
        return SessionResponse(
            session_id=session_id, platform="linkedin",
            logged_in=False, error=result.get("error", "Invalid li_at"))
    except Exception as e:
        logger.exception("LI sessionid error")
        raise HTTPException(500, str(e))


@app.post("/linkedin/auth/2fa")
async def li_resolve_2fa(
    session_id: str = Form(...),
    pin: str = Form(...),
):
    """Resolve LinkedIn 2FA challenge."""
    try:
        session = await session_manager.get_session(session_id)
        page = await session_manager.get_page(session_id)
        li = LinkedInClient(page)
        result = await li.resolve_2fa(pin)
        if result.get("success"):
            session["li"]["logged_in"] = True
            return SessionResponse(
                session_id=session_id, platform="linkedin",
                logged_in=True, li_at=result.get("li_at"))
        return SessionResponse(
            session_id=session_id, platform="linkedin",
            logged_in=False, error=result.get("error"))
    except Exception as e:
        raise HTTPException(500, str(e))


@app.get("/linkedin/auth/settings")
async def li_auth_settings(session_id: str):
    """Check LinkedIn login status."""
    try:
        session = await session_manager.get_session(session_id)
        page = await session_manager.get_page(session_id)
        li = LinkedInClient(page)
        profile = await li.get_profile_info()
        return {
            "logged_in": profile["logged_in"],
            "username": profile.get("username") or session["li"].get("username"),
            "session_id": session_id,
            "platform": "linkedin",
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/linkedin/post/text")
async def li_post_text(
    session_id: str = Form(...),
    text: str = Form(...),
):
    """Post a text update to LinkedIn."""
    try:
        session = await session_manager.get_session(session_id)
        if not session["li"].get("logged_in"):
            raise HTTPException(401, "Not logged in to LinkedIn")
        page = await session_manager.get_page(session_id)
        li = LinkedInClient(page)
        result = await li.post_text(text)
        if result.get("success"):
            return {"success": True}
        raise HTTPException(500, result.get("error", "Post failed"))
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/linkedin/post/image")
async def li_post_image(
    session_id: str = Form(...),
    text: str = Form(""),
    image_url: str = Form(...),
):
    """Post an image to LinkedIn."""
    try:
        session = await session_manager.get_session(session_id)
        if not session["li"].get("logged_in"):
            raise HTTPException(401, "Not logged in to LinkedIn")
        page = await session_manager.get_page(session_id)
        li = LinkedInClient(page)
        tmp = await download_image(image_url)
        try:
            result = await li.post_image(tmp, text)
            if result.get("success"):
                return {"success": True}
            raise HTTPException(500, result.get("error", "Post failed"))
        finally:
            try:
                os.unlink(tmp)
            except Exception:
                pass
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))


# ============================================================
# Main
# ============================================================
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=settings.HOST, port=settings.PORT)
