"""
Multi-platform Social Media API using instagrapi (Instagram) and Playwright (LinkedIn).
Instagram: direct API via instagrapi — no browser needed.
LinkedIn: Playwright browser automation.
"""
import os, json, uuid, time, threading, asyncio, tempfile
from datetime import datetime
from typing import Optional, Dict, Any
from contextlib import asynccontextmanager
from concurrent.futures import ThreadPoolExecutor

import httpx
from fastapi import FastAPI, HTTPException, Form
from pydantic import BaseModel
from pydantic_settings import BaseSettings
import logging
logger = logging.getLogger("social-api")
logging.basicConfig(level=logging.INFO)

from instagrapi import Client
from instagrapi.exceptions import (
    LoginRequired, ClientError, ChallengeRequired,
    PleaseWaitFewMinutes, RecaptchaChallengeRequired,
    FeedbackRequired, BadPassword, TwoFactorRequired,
)
from playwright.async_api import async_playwright, Page


# ============================================================
# Configuration
# ============================================================
class Settings(BaseSettings):
    PORT: int = 8000
    HOST: str = "0.0.0.0"
    BROWSER_HEADLESS: bool = True
    SESSION_DIR: str = "/app/sessions"
    LI_LOGIN_URL: str = "https://www.linkedin.com/login"
    LI_BASE_URL: str = "https://www.linkedin.com"
    REQUEST_DELAY_MS: int = 2000
    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"

settings = Settings()
executor: Optional[ThreadPoolExecutor] = None


# ============================================================
# Challenge Store (thread-safe, for instagrapi challenge codes)
# ============================================================
challenge_store: Dict[str, Dict] = {}
challenge_lock = threading.Lock()

twofactor_store: Dict[str, Dict] = {}
twofactor_lock = threading.Lock()


def make_challenge_handler(username: str) -> Any:
    cid = str(uuid.uuid4())
    def handler(client, challenge):
        api_path = getattr(challenge, "api_path", None) or str(challenge)
        with challenge_lock:
            challenge_store[cid] = {"username": username, "code": None,
                "resolved": False, "timestamp": time.time(), "api_path": api_path}
        logger.info(f"Challenge required for {username}, id={cid}")
        deadline = time.time() + 600
        while time.time() < deadline:
            with challenge_lock:
                e = challenge_store.get(cid)
                if e and e["resolved"]:
                    code = e["code"]
                    del challenge_store[cid]
                    return code
            time.sleep(1)
        with challenge_lock:
            challenge_store.pop(cid, None)
        raise TimeoutError("Challenge resolution timed out")
    handler.challenge_id = cid
    return handler


def make_twofactor_handler(username: str) -> Any:
    tid = str(uuid.uuid4())
    def handler(client, code_verifier):
        with twofactor_lock:
            twofactor_store[tid] = {"username": username, "code": None,
                "resolved": False, "timestamp": time.time(), "code_verifier": code_verifier}
        logger.info(f"2FA required for {username}, id={tid}")
        deadline = time.time() + 600
        while time.time() < deadline:
            with twofactor_lock:
                e = twofactor_store.get(tid)
                if e and e["resolved"]:
                    code = e["code"]
                    del twofactor_store[tid]
                    return code, code_verifier
            time.sleep(1)
        with twofactor_lock:
            twofactor_store.pop(tid, None)
        raise TimeoutError("2FA resolution timed out")
    handler.twofactor_id = tid
    return handler


# ============================================================
# Session Manager (Instagram via instagrapi)
# ============================================================
class SessionManager:
    def __init__(self):
        self.sessions: Dict[str, Dict] = {}
        os.makedirs(settings.SESSION_DIR, exist_ok=True)

    def create_session(self, session_id: Optional[str] = None) -> str:
        sid = session_id or str(uuid.uuid4())
        client = Client()
        client.set_user_agent(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/130.0.0.0 Safari/537.36")
        client.delay_range = [1, 3]
        self.sessions[sid] = {"client": client, "created_at": datetime.utcnow(),
            "ig_username": None, "challenge_id": None, "twofactor_id": None}
        logger.info(f"Created IG session: {sid}")
        return sid

    def get_session(self, session_id: str) -> Dict:
        if session_id not in self.sessions:
            raise HTTPException(404, f"Session not found: {session_id}")
        return self.sessions[session_id]

    def close_session(self, session_id: str):
        if session_id in self.sessions:
            try: self.sessions[session_id]["client"].logout()
            except: pass
            del self.sessions[session_id]

    def _save_settings(self, session_id: str):
        s = self.get_session(session_id)
        p = os.path.join(settings.SESSION_DIR, f"{session_id}.json")
        try:
            with open(p, "w") as f: json.dump(s["client"].get_settings(), f)
        except: pass

    def restore_session(self, session_id: str) -> bool:
        p = os.path.join(settings.SESSION_DIR, f"{session_id}.json")
        if not os.path.exists(p): return False
        try:
            with open(p) as f: saved = json.load(f)
            client = Client()
            client.set_settings(saved)
            client.set_user_agent(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")
            client.delay_range = [1, 3]
            client.get_timeline_feed()
            self.sessions[session_id] = {"client": client, "created_at": datetime.utcnow(),
                "ig_username": None, "challenge_id": None, "twofactor_id": None}
            return True
        except:
            return False

session_manager = SessionManager()


# ============================================================
# Helper: run sync instagrapi calls in thread
# ============================================================
async def run_sync(fn, *args, **kwargs):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(executor, fn, *args, **kwargs)


# ============================================================
# FastAPI App
# ============================================================
class SessionResponse(BaseModel):
    session_id: str
    platform: str = "instagram"
    logged_in: bool = False
    username: Optional[str] = None
    challenge: Optional[Dict] = None
    twofactor: Optional[Dict] = None
    error: Optional[str] = None
    sessionid: Optional[str] = None
    li_at: Optional[str] = None


app = FastAPI(
    title="Social Media API",
    description="Instagram via instagrapi (direct API). LinkedIn via Playwright (browser).",
    version="3.0.0",
)


# ============================================================
# Common Endpoints
# ============================================================

@app.get("/health")
async def health():
    return {"status": "ok"}

@app.get("/sessions")
async def list_sessions():
    ig = [{"session_id": sid, "username": s.get("ig_username"),
           "created_at": s["created_at"].isoformat()}
          for sid, s in session_manager.sessions.items()]
    li = [{"session_id": sid, "username": s.get("username"),
           "logged_in": s.get("logged_in"),
           "created_at": s["created_at"].isoformat()}
          for sid, s in li_sessions.items()]
    return {"instagram": ig, "linkedin": li}


# ============================================================
# Instagram Endpoints (instagrapi)
# ============================================================

@app.post("/auth/login", response_model=SessionResponse)
async def ig_login(
    username: str = Form(...), password: str = Form(...),
    session_id: Optional[str] = Form(None),
):
    try:
        if session_id:
            session = session_manager.get_session(session_id)
        else:
            session_id = session_manager.create_session()
            session = session_manager.sessions[session_id]
        client = session["client"]
        ch = make_challenge_handler(username)
        tf = make_twofactor_handler(username)
        client.challenge_code_handler = ch
        client.twofactor_handler = tf
        session["challenge_id"] = ch.challenge_id
        session["twofactor_id"] = tf.twofactor_id

        def do_login():
            client.login(username, password)
            return client.account_info().dict()

        try:
            info = await run_sync(do_login)
        except ChallengeRequired:
            return SessionResponse(session_id=session_id,
                challenge={"challenge_id": ch.challenge_id, "message": "Security code required. Check email/SMS."},
                logged_in=False)
        except TwoFactorRequired:
            return SessionResponse(session_id=session_id,
                twofactor={"twofactor_id": tf.twofactor_id, "message": "2FA code required."},
                logged_in=False)
        except BadPassword:
            return SessionResponse(session_id=session_id, logged_in=False,
                error="Incorrect password.")
        except FeedbackRequired as e:
            return SessionResponse(session_id=session_id, logged_in=False,
                error=f"Account temporarily restricted: {e}")
        except PleaseWaitFewMinutes as e:
            return SessionResponse(session_id=session_id, logged_in=False,
                error=f"Please wait a few minutes before trying again: {e}")
        except RecaptchaChallengeRequired:
            return SessionResponse(session_id=session_id, logged_in=False,
                error="Recaptcha challenge required. Try again later or use sessionid login.")
        except ClientError as e:
            return SessionResponse(session_id=session_id, logged_in=False,
                error=f"Instagram error: {e}")
        except Exception as e:
            logger.exception("IG login error")
            return SessionResponse(session_id=session_id, logged_in=False, error=str(e))

        session["ig_username"] = info.get("username") or username
        session_manager._save_settings(session_id)
        sessionid = _extract_sessionid(client)
        return SessionResponse(session_id=session_id, logged_in=True,
            username=session["ig_username"], sessionid=sessionid)
    except HTTPException: raise
    except Exception as e:
        raise HTTPException(500, str(e))


def _extract_sessionid(client) -> Optional[str]:
    try:
        ck = client.get_settings().get("cookies", {})
        for domain, cookies in ck.items():
            if "instagram.com" in domain or "i.instagram.com" in domain:
                if isinstance(cookies, dict) and "sessionid" in cookies:
                    return cookies["sessionid"]
                if isinstance(cookies, list):
                    for c in cookies:
                        if c.get("name") == "sessionid": return c["value"]
    except: pass
    return None


@app.post("/auth/sessionid", response_model=SessionResponse)
async def ig_sessionid(
    sessionid: str = Form(...),
    session_id: Optional[str] = Form(None),
):
    try:
        if session_id:
            session = session_manager.get_session(session_id)
        else:
            session_id = session_manager.create_session()
            session = session_manager.sessions[session_id]
        client = session["client"]

        def do_login():
            client.login_by_sessionid(sessionid)
            return client.account_info().dict()

        try:
            info = await run_sync(do_login)
        except LoginRequired:
            return SessionResponse(session_id=session_id, logged_in=False,
                error="Session expired or invalid.")
        except Exception as e:
            return SessionResponse(session_id=session_id, logged_in=False, error=str(e))

        session["ig_username"] = info.get("username")
        session_manager._save_settings(session_id)
        return SessionResponse(session_id=session_id, logged_in=True,
            username=session["ig_username"], sessionid=sessionid)
    except HTTPException: raise
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/auth/challenge/resolve", response_model=SessionResponse)
async def ig_challenge_resolve(
    session_id: str = Form(...), challenge_id: str = Form(...), code: str = Form(...),
):
    try:
        session_manager.get_session(session_id)
        with challenge_lock:
            e = challenge_store.get(challenge_id)
            if not e: raise HTTPException(400, "Challenge expired or invalid. Try logging in again.")
            e["code"] = code; e["resolved"] = True
        await asyncio.sleep(5)
        client = session_manager.get_session(session_id)["client"]
        def check():
            try: return client.account_info().dict()
            except: return None
        info = await run_sync(check)
        if info and info.get("username"):
            session_manager.sessions[session_id]["ig_username"] = info["username"]
            session_manager._save_settings(session_id)
            return SessionResponse(session_id=session_id, logged_in=True, username=info["username"])
        return SessionResponse(session_id=session_id, logged_in=False,
            error="Login failed after challenge resolution.")
    except HTTPException: raise
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/auth/twofactor/resolve", response_model=SessionResponse)
async def ig_twofactor_resolve(
    session_id: str = Form(...), twofactor_id: str = Form(...), code: str = Form(...),
):
    try:
        session_manager.get_session(session_id)
        with twofactor_lock:
            e = twofactor_store.get(twofactor_id)
            if not e: raise HTTPException(400, "2FA expired or invalid.")
            e["code"] = code; e["resolved"] = True
        await asyncio.sleep(5)
        client = session_manager.get_session(session_id)["client"]
        def check():
            try: return client.account_info().dict()
            except: return None
        info = await run_sync(check)
        if info and info.get("username"):
            session_manager.sessions[session_id]["ig_username"] = info["username"]
            session_manager._save_settings(session_id)
            return SessionResponse(session_id=session_id, logged_in=True, username=info["username"])
        return SessionResponse(session_id=session_id, logged_in=False, error="2FA failed.")
    except HTTPException: raise
    except Exception as e:
        raise HTTPException(500, str(e))


@app.get("/auth/settings")
async def ig_auth_settings(session_id: str):
    try:
        session = session_manager.get_session(session_id)
        def get_info():
            try:
                i = session["client"].account_info()
                return {"username": i.username, "full_name": i.full_name}
            except LoginRequired: return None
        info = await run_sync(get_info)
        if info:
            return {"logged_in": True, "username": info["username"],
                "full_name": info["full_name"], "session_id": session_id, "platform": "instagram"}
        return {"logged_in": False, "session_id": session_id, "platform": "instagram"}
    except HTTPException: raise
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/photo/upload")
async def ig_photo_upload(
    session_id: str = Form(...), caption: str = Form(""), image_url: str = Form(...),
):
    try:
        client = session_manager.get_session(session_id)["client"]
        async with httpx.AsyncClient() as hc:
            r = await hc.get(image_url, timeout=30.0)
            r.raise_for_status()
            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".jpg")
            tmp.write(r.content); tmp.close()
        def do():
            return client.photo_upload(tmp.name, caption)
        try:
            result = await run_sync(do)
            return {"success": True, "id": result.id, "pk": result.pk}
        finally:
            try: os.unlink(tmp.name)
            except: pass
    except HTTPException: raise
    except Exception as e:
        raise HTTPException(500, str(e))


@app.get("/user/posts")
async def ig_user_posts(session_id: str, username: Optional[str] = None, amount: int = 12):
    try:
        client = session_manager.get_session(session_id)["client"]
        def do():
            target = username or session_manager.sessions[session_id].get("ig_username")
            if not target: raise ValueError("Username required")
            uid = client.user_id_from_username(target)
            return [{"id": m.id, "pk": m.pk, "code": m.code, "taken_at": str(m.taken_at),
                     "media_type": m.media_type, "caption": getattr(m, "caption_text", ""),
                     "thumbnail_url": m.thumbnail_url}
                    for m in client.user_medias(uid, amount)]
        return {"items": await run_sync(do)}
    except HTTPException: raise
    except Exception as e:
        raise HTTPException(500, str(e))


@app.get("/account/info")
async def ig_account_info(session_id: str):
    try:
        client = session_manager.get_session(session_id)["client"]
        def do():
            i = client.account_info()
            return {"username": i.username, "full_name": i.full_name,
                "biography": i.biography, "pk": i.pk, "is_private": i.is_private,
                "is_verified": i.is_verified, "follower_count": i.follower_count,
                "following_count": i.following_count, "media_count": i.media_count}
        info = await run_sync(do)
        return {"logged_in": True, **info}
    except HTTPException: raise
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/auth/logout")
async def ig_logout(session_id: str):
    session_manager.close_session(session_id)
    return {"success": True}


# ============================================================
# LinkedIn Endpoints (Playwright)
# ============================================================

STEALTH_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
window.chrome = { runtime: {} };
const _o = window.navigator.permissions.query;
window.navigator.permissions.query = (p) => p.name === 'notifications' ? Promise.resolve({state:'denied'}) : _o(p);
Object.defineProperty(navigator, 'plugins', {get:()=>[1,2,3,4,5]});
Object.defineProperty(navigator, 'languages', {get:()=>['en-US','en']});
"""

li_playwright: Any = None
li_sessions: Dict[str, Dict] = {}

LI_SESSION_DIR = os.path.join(settings.SESSION_DIR, "li")
os.makedirs(LI_SESSION_DIR, exist_ok=True)


async def li_get_browser():
    global li_playwright
    if li_playwright is None:
        li_playwright = await async_playwright().start()
    return li_playwright


async def li_create_session(session_id: Optional[str] = None) -> str:
    sid = session_id or str(uuid.uuid4())
    pw = await li_get_browser()
    ctx = await pw.chromium.launch_persistent_context(
        user_data_dir=os.path.join(LI_SESSION_DIR, sid),
        headless=settings.BROWSER_HEADLESS,
        args=["--no-sandbox","--disable-setuid-sandbox","--disable-dev-shm-usage",
              "--disable-blink-features=AutomationControlled"],
        viewport={"width":1280,"height":720},
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
        locale="en-US", timezone_id="America/New_York")
    li_sessions[sid] = {"context": ctx, "page": None,
        "created_at": datetime.utcnow(), "logged_in": False, "username": None}
    return sid


async def li_get_session(session_id: str) -> Dict:
    if session_id not in li_sessions:
        raise HTTPException(404, f"LinkedIn session not found: {session_id}")
    return li_sessions[session_id]


async def li_get_page(session_id: str) -> Page:
    s = await li_get_session(session_id)
    if s["page"] is None or s["page"].is_closed():
        s["page"] = await s["context"].new_page()
        await s["page"].add_init_script(STEALTH_SCRIPT)
    return s["page"]


async def li_goto(page: Page, url: str):
    await page.goto(url, wait_until="networkidle", timeout=60000)
    await asyncio.sleep(settings.REQUEST_DELAY_MS / 1000)


async def li_is_logged_in(page: Page) -> bool:
    try:
        await page.wait_for_selector('#feed-nav,.feed-identity-module,a[href*="/feed/"],.global-nav__me', timeout=5000)
        return True
    except: return False


@app.post("/linkedin/auth/login", response_model=SessionResponse)
async def li_login(
    email: str = Form(...), password: str = Form(...),
    session_id: Optional[str] = Form(None),
):
    try:
        if session_id:
            await li_get_session(session_id)
        else:
            session_id = await li_create_session()
        page = await li_get_page(session_id)
        await li_goto(page, settings.LI_LOGIN_URL)
        await page.fill('input[name="session_key"]', email)
        await page.fill('input[name="session_password"]', password)
        await asyncio.sleep(1)
        await page.click('button[type="submit"]')
        await page.wait_for_load_state("networkidle", timeout=30000)
        try:
            pin = await page.wait_for_selector('input[name="pin"],input[autocomplete="one-time-code"]', timeout=3000)
            if pin:
                return SessionResponse(session_id=session_id, platform="linkedin",
                    challenge={"challenge_type":"2fa","message":"PIN required"}, logged_in=False)
        except: pass
        ok = await li_is_logged_in(page)
        if not ok:
            try:
                err = await page.wait_for_selector('#error-for-password', timeout=2000)
                msg = await err.inner_text() if err else "Login failed"
            except: msg = "Login failed"
            return SessionResponse(session_id=session_id, platform="linkedin", logged_in=False, error=msg)
        cookies = await page.context.cookies()
        li_at = next((c["value"] for c in cookies if c["name"]=="li_at"), None)
        li_sessions[session_id]["logged_in"] = True
        return SessionResponse(session_id=session_id, platform="linkedin", logged_in=True, li_at=li_at)
    except HTTPException: raise
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/linkedin/auth/sessionid", response_model=SessionResponse)
async def li_sessionid(
    li_at: str = Form(...), session_id: Optional[str] = Form(None),
):
    try:
        if session_id: await li_get_session(session_id)
        else: session_id = await li_create_session()
        page = await li_get_page(session_id)
        await li_goto(page, settings.LI_BASE_URL+"/")
        await page.context.add_cookies([
            {"name":"li_at","value":li_at,"domain":".linkedin.com","path":"/"},
            {"name":"lang","value":"v=2&lang=en-us","domain":".linkedin.com","path":"/"}])
        await li_goto(page, settings.LI_BASE_URL+"/feed/")
        ok = await li_is_logged_in(page)
        if ok:
            li_sessions[session_id]["logged_in"] = True
            return SessionResponse(session_id=session_id, platform="linkedin", logged_in=True, li_at=li_at)
        return SessionResponse(session_id=session_id, platform="linkedin", logged_in=False,
            error="li_at cookie expired or invalid")
    except HTTPException: raise
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/linkedin/auth/2fa")
async def li_resolve_2fa(session_id: str = Form(...), pin: str = Form(...)):
    try:
        page = await li_get_page(session_id)
        inp = await page.wait_for_selector('input[name="pin"],input[autocomplete="one-time-code"]', timeout=10000)
        await inp.fill(pin); await asyncio.sleep(1)
        btn = await page.wait_for_selector(
            'button[type="submit"]:has-text("Submit"),button[type="submit"]:has-text("Verify")', timeout=5000)
        await btn.click()
        await page.wait_for_load_state("networkidle", timeout=30000)
        ok = await li_is_logged_in(page)
        if ok:
            li_sessions[session_id]["logged_in"] = True
            cookies = await page.context.cookies()
            li_at = next((c["value"] for c in cookies if c["name"]=="li_at"), None)
            return SessionResponse(session_id=session_id, platform="linkedin", logged_in=True, li_at=li_at)
        return SessionResponse(session_id=session_id, platform="linkedin", logged_in=False, error="2FA failed")
    except Exception as e:
        raise HTTPException(500, str(e))


@app.get("/linkedin/auth/settings")
async def li_auth_settings(session_id: str):
    try:
        page = await li_get_page(session_id)
        await li_goto(page, settings.LI_BASE_URL+"/feed/")
        try:
            el = await page.wait_for_selector('.global-nav__me,a[href*="/in/"]', timeout=5000)
            username = (await el.inner_text()).strip()[:100]
        except: username = None
        ok = await li_is_logged_in(page)
        return {"logged_in": ok, "username": username or li_sessions.get(session_id,{}).get("username"),
            "session_id": session_id, "platform": "linkedin"}
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/linkedin/post/text")
async def li_post_text(session_id: str = Form(...), text: str = Form(...)):
    try:
        s = await li_get_session(session_id)
        if not s["logged_in"]: raise HTTPException(401, "Not logged in")
        page = await li_get_page(session_id)
        await li_goto(page, settings.LI_BASE_URL+"/feed/")
        share = await page.wait_for_selector(
            'div[role="button"]:has-text("Start a post"),.share-box__open,div[data-control-name="create_post"]', timeout=10000)
        await share.click(); await asyncio.sleep(1)
        editor = await page.wait_for_selector(
            'div[role="textbox"][aria-label*="What do you want"],div[role="textbox"][contenteditable="true"]', timeout=10000)
        await editor.fill(text); await asyncio.sleep(1)
        btn = await page.wait_for_selector('button[type="submit"]:has-text("Post"),button:has-text("Post")', timeout=5000)
        await btn.click()
        await page.wait_for_load_state("networkidle", timeout=15000)
        return {"success": True}
    except HTTPException: raise
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/linkedin/post/image")
async def li_post_image(
    session_id: str = Form(...), text: str = Form(""), image_url: str = Form(...),
):
    try:
        s = await li_get_session(session_id)
        if not s["logged_in"]: raise HTTPException(401, "Not logged in")
        async with httpx.AsyncClient() as hc:
            r = await hc.get(image_url, timeout=30.0); r.raise_for_status()
            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".jpg")
            tmp.write(r.content); tmp.close()
        try:
            page = await li_get_page(session_id)
            await li_goto(page, settings.LI_BASE_URL+"/feed/")
            share = await page.wait_for_selector(
                'div[role="button"]:has-text("Start a post"),.share-box__open,div[data-control-name="create_post"]', timeout=10000)
            await share.click(); await asyncio.sleep(1)
            img = await page.wait_for_selector(
                'button[aria-label*="image"],button[aria-label*="photo"],button[aria-label*="media"],li[data-control-name="media_upload"] button', timeout=10000)
            await img.click(); await asyncio.sleep(1)
            fi = await page.wait_for_selector('input[type="file"][accept*="image"]', timeout=10000)
            await fi.set_input_files(tmp.name)
            await page.wait_for_load_state("networkidle", timeout=30000)
            if text:
                editor = await page.wait_for_selector(
                    'div[role="textbox"][aria-label*="What do you want"],div[role="textbox"][contenteditable="true"]', timeout=10000)
                await editor.fill(text); await asyncio.sleep(1)
            btn = await page.wait_for_selector('button[type="submit"]:has-text("Post"),button:has-text("Post")', timeout=5000)
            await btn.click()
            await page.wait_for_load_state("networkidle", timeout=30000)
            return {"success": True}
        finally:
            try: os.unlink(tmp.name)
            except: pass
    except HTTPException: raise
    except Exception as e:
        raise HTTPException(500, str(e))


# ============================================================
# Lifecycle
# ============================================================
@app.on_event("startup")
async def startup():
    global executor
    executor = ThreadPoolExecutor(max_workers=4)
    logger.info("Started")

@app.on_event("shutdown")
async def shutdown():
    global executor, li_playwright
    if executor: executor.shutdown(wait=True)
    for sid in list(session_manager.sessions): session_manager.close_session(sid)
    for sid in list(li_sessions):
        try: await li_sessions[sid]["context"].close()
        except: pass
    li_sessions.clear()
    if li_playwright:
        await li_playwright.stop()
        li_playwright = None


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=settings.HOST, port=settings.PORT)
