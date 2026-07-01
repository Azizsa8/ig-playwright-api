"""
Instagram API service using Playwright browser automation.
Replaces aiograpi-rest with a working, maintainable implementation.
"""
import os
import json
import asyncio
import uuid
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Form, Body, BackgroundTasks
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings
from loguru import logger

from playwright.async_api import async_playwright, Browser, BrowserContext, Page

# ============================================================
# Configuration
# ============================================================
class Settings(BaseSettings):
    # Service
    PORT: int = 8000
    HOST: str = "0.0.0.0"
    
    # Playwright
    BROWSER_HEADLESS: bool = True
    BROWSER_ARGS: list = ["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"]
    
    # Session storage
    SESSION_DIR: str = "/app/sessions"
    SESSION_TTL_HOURS: int = 24
    
    # Instagram
    IG_LOGIN_URL: str = "https://www.instagram.com/accounts/login/"
    IG_BASE_URL: str = "https://www.instagram.com"
    
    # Rate limiting
    REQUEST_DELAY_MS: int = 2000
    
    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"

settings = Settings()

# ============================================================
# Session Management
# ============================================================
class SessionManager:
    """Manages persistent browser contexts per session."""
    
    def __init__(self):
        self.sessions: Dict[str, Dict] = {}
        self.browser: Optional[Browser] = None
        self.playwright = None
        os.makedirs(settings.SESSION_DIR, exist_ok=True)
    
    async def startup(self):
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(
            headless=settings.BROWSER_HEADLESS,
            args=settings.BROWSER_ARGS
        )
        logger.info("Playwright browser started")
    
    async def shutdown(self):
        if self.browser:
            await self.browser.close()
        if self.playwright:
            await self.playwright.stop()
        logger.info("Playwright browser stopped")
    
    def _session_path(self, session_id: str) -> str:
        return os.path.join(settings.SESSION_DIR, session_id)
    
    async def create_session(self, session_id: Optional[str] = None) -> str:
        """Create a new persistent browser context."""
        sid = session_id or str(uuid.uuid4())
        context = await self.browser.new_context(
            user_data_dir=self._session_path(sid),
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
            "logged_in": False,
            "username": None,
        }
        logger.info(f"Created session: {sid}")
        return sid
    
    async def get_session(self, session_id: str) -> Dict:
        if session_id not in self.sessions:
            # Try to restore from disk
            await self.restore_session(session_id)
        if session_id not in self.sessions:
            raise HTTPException(404, f"Session not found: {session_id}")
        return self.sessions[session_id]
    
    async def restore_session(self, session_id: str) -> Dict:
        """Restore a session from persistent storage."""
        context = await self.browser.new_context(
            user_data_dir=self._session_path(session_id),
            viewport={"width": 1280, "height": 720},
        )
        self.sessions[session_id] = {
            "context": context,
            "created_at": datetime.utcnow(),
            "page": None,
            "logged_in": None,  # Unknown until checked
            "username": None,
        }
        logger.info(f"Restored session: {session_id}")
        return self.sessions[session_id]
    
    async def get_page(self, session_id: str) -> Page:
        session = await self.get_session(session_id)
        if session["page"] is None or session["page"].is_closed():
            session["page"] = await session["context"].new_page()
        return session["page"]
    
    async def close_session(self, session_id: str):
        if session_id in self.sessions:
            ctx = self.sessions[session_id]["context"]
            await ctx.close()
            del self.sessions[session_id]
            logger.info(f"Closed session: {session_id}")
    
    async def cleanup_expired(self):
        now = datetime.utcnow()
        expired = [
            sid for sid, s in self.sessions.items()
            if (now - s["created_at"]).total_seconds() > settings.SESSION_TTL_HOURS * 3600
        ]
        for sid in expired:
            await self.close_session(sid)

session_manager = SessionManager()

# ============================================================
# Instagram Interaction Helpers
# ============================================================
class InstagramClient:
    """High-level Instagram interactions via Playwright."""
    
    def __init__(self, page: Page):
        self.page = page
    
    async def navigate(self, url: str, wait_until: str = "networkidle"):
        await self.page.goto(url, wait_until=wait_until, timeout=60000)
        await self._random_delay()
    
    async def _random_delay(self):
        await asyncio.sleep(settings.REQUEST_DELAY_MS / 1000)
    
    async def is_logged_in(self) -> bool:
        """Check if currently logged in."""
        try:
            # Check for login form absence or profile element presence
            await self.page.wait_for_selector(
                'header [role="button"][aria-label*="Profile"], '
                'a[href*="/accounts/logout/"], '
                'svg[aria-label="Home"]',
                timeout=5000
            )
            return True
        except:
            return False
    
    async def login(self, username: str, password: str) -> Dict[str, Any]:
        """Perform login with challenge handling."""
        await self.navigate(settings.IG_LOGIN_URL)
        
        # Fill login form
        await self.page.fill('input[name="username"]', username)
        await self.page.fill('input[name="password"]', password)
        await self._random_delay()
        
        # Click login
        await self.page.click('button[type="submit"]')
        await self.page.wait_for_load_state("networkidle", timeout=30000)
        
        # Check for challenges
        challenge = await self._handle_challenges()
        if challenge:
            return {"challenge": True, **challenge}
        
        # Check login success
        logged_in = await self.is_logged_in()
        if not logged_in:
            # Check for error messages
            error = await self._get_login_error()
            return {"error": error or "Login failed"}
        
        # Get session info
        cookies = await self.page.context.cookies()
        sessionid = next((c["value"] for c in cookies if c["name"] == "sessionid"), None)
        csrftoken = next((c["value"] for c in cookies if c["name"] == "csrftoken"), None)
        
        return {
            "success": True,
            "sessionid": sessionid,
            "csrftoken": csrftoken,
            "username": username,
        }
    
    async def _handle_challenges(self) -> Optional[Dict]:
        """Detect and return challenge info if present."""
        # Check for challenge forms
        challenge_selectors = [
            'div[role="dialog"]:has-text("Challenge")',
            'div[role="dialog"]:has-text("Verify")',
            'div[role="dialog"]:has-text("Confirm")',
            'form[id*="challenge"]',
        ]
        
        for sel in challenge_selectors:
            try:
                el = await self.page.wait_for_selector(sel, timeout=3000)
                if el:
                    # Extract challenge details
                    text = await el.inner_text()
                    return {
                        "challenge_type": "manual" if "code" in text.lower() else "unknown",
                        "message": text[:500],
                        "html": await el.inner_html(),
                    }
            except:
                continue
        
        # Check for "bad_password" / IP block message
        try:
            error_el = await self.page.wait_for_selector(
                'div[role="alert"], .x1lliihq, [id*="error"]',
                timeout=3000
            )
            if error_el:
                text = await error_el.inner_text()
                if "email" in text.lower() or "block" in text.lower():
                    return {
                        "challenge_type": "ip_block",
                        "message": text[:500],
                        "requires_new_ip": True,
                    }
        except:
            pass
        
        return None
    
    async def _get_login_error(self) -> Optional[str]:
        try:
            error_selectors = [
                'div[role="alert"]',
                '.x1lliihq',
                '[id*="error"]',
                'p:has-text("Sorry")',
                'p:has-text("incorrect")',
            ]
            for sel in error_selectors:
                el = await self.page.wait_for_selector(sel, timeout=2000)
                if el:
                    text = await el.inner_text()
                    if text.strip():
                        return text.strip()
        except:
            pass
        return None
    
    async def resolve_challenge(self, security_code: str) -> Dict:
        """Submit security code for challenge resolution."""
        try:
            # Find code input
            code_input = await self.page.wait_for_selector(
                'input[name="security_code"], input[name="code"], input[autocomplete="one-time-code"]',
                timeout=5000
            )
            await code_input.fill(security_code)
            await self._random_delay()
            
            # Submit
            submit_btn = await self.page.wait_for_selector(
                'button[type="submit"], button:has-text("Confirm"), button:has-text("Submit")',
                timeout=5000
            )
            await submit_btn.click()
            await self.page.wait_for_load_state("networkidle", timeout=30000)
            
            logged_in = await self.is_logged_in()
            return {"success": logged_in, "logged_in": logged_in}
        except Exception as e:
            return {"error": str(e), "success": False}
    
    async def get_profile_info(self) -> Dict:
        """Get basic profile info."""
        await self.navigate(f"{settings.IG_BASE_URL}/")
        await self._random_delay()
        
        # Extract username from header/profile link
        try:
            profile_link = await self.page.wait_for_selector(
                'header a[href^="/"][href$="/"]',
                timeout=5000
            )
            href = await profile_link.get_attribute("href")
            username = href.strip("/") if href else None
        except:
            username = None
        
        return {"username": username, "logged_in": await self.is_logged_in()}
    
    async def upload_photo(self, image_path: str, caption: str = "") -> Dict:
        """Upload a photo post."""
        await self.navigate(f"{settings.IG_BASE_URL}/")
        await self._random_delay()
        
        # Click create/new post button
        create_selectors = [
            'svg[aria-label="New post"]',
            'a[href="/create/"]',
            'div[role="button"]:has-text("Create")',
        ]
        for sel in create_selectors:
            try:
                await self.page.click(sel, timeout=3000)
                break
            except:
                continue
        
        await self.page.wait_for_selector('input[type="file"]', timeout=10000)
        file_input = await self.page.query_selector('input[type="file"]')
        await file_input.set_input_files(image_path)
        
        await self.page.wait_for_load_state("networkidle", timeout=30000)
        
        # Next buttons
        for _ in range(2):
            next_btn = await self.page.wait_for_selector(
                'button:has-text("Next"), div[role="button"]:has-text("Next")',
                timeout=10000
            )
            await next_btn.click()
            await self._random_delay()
        
        # Caption
        if caption:
            caption_area = await self.page.wait_for_selector(
                'textarea[aria-label="Write a caption..."], textarea[placeholder*="caption"]',
                timeout=5000
            )
            await caption_area.fill(caption)
            await self._random_delay()
        
        # Share
        share_btn = await self.page.wait_for_selector(
            'button:has-text("Share"), div[role="button"]:has-text("Share")',
            timeout=10000
        )
        await share_btn.click()
        await self.page.wait_for_load_state("networkidle", timeout=30000)
        
        return {"success": True}
    
    async def get_media(self, username: str, amount: int = 12) -> List[Dict]:
        """Get recent media for a user."""
        await self.navigate(f"{settings.IG_BASE_URL}/{username}/")
        await self._random_delay()
        
        # Scroll to load more
        media_items = []
        for _ in range(3):
            posts = await self.page.query_selector_all('article a[href*="/p/"]')
            for post in posts:
                href = await post.get_attribute("href")
                if href and href not in [m.get("url") for m in media_items]:
                    media_items.append({"url": f"{settings.IG_BASE_URL}{href}"})
            if len(media_items) >= amount:
                break
            await self.page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await asyncio.sleep(2)
        
        return media_items[:amount]
    
    async def get_insights(self) -> Dict:
        """Get account insights (requires professional account)."""
        await self.navigate(f"{settings.IG_BASE_URL}/accounts/insights/")
        await self._random_delay()
        
        # Extract basic insights if available
        return {"available": await self.is_logged_in()}


# ============================================================
# FastAPI Models
# ============================================================
class LoginRequest(BaseModel):
    username: str
    password: str
    session_id: Optional[str] = None

class ChallengeResolveRequest(BaseModel):
    session_id: str
    security_code: str

class PhotoUploadRequest(BaseModel):
    session_id: str
    image_url: str  # We'll download it first
    caption: str = ""

class SessionResponse(BaseModel):
    session_id: str
    logged_in: bool = False
    username: Optional[str] = None
    challenge: Optional[Dict] = None
    error: Optional[str] = None
    sessionid: Optional[str] = None



# ============================================================
# FastAPI App
# ============================================================
@asynccontextmanager
async def lifespan(app: FastAPI):
    await session_manager.startup()
    yield
    await session_manager.shutdown()

app = FastAPI(
    title="Instagram Playwright API",
    description="Drop-in replacement for aiograpi-rest using Playwright browser automation",
    version="1.0.0",
    lifespan=lifespan,
)

# ============================================================
# Endpoints
# ============================================================
@app.get("/health")
async def health():
    return {"status": "ok"}

@app.post("/auth/login", response_model=SessionResponse)
async def login(
    username: str = Form(...),
    password: str = Form(...),
    session_id: Optional[str] = Form(None),
):
    """Login to Instagram. Creates session if needed."""
    try:
        # Create or restore session
        if session_id:
            session = await session_manager.get_session(session_id)
        else:
            session_id = await session_manager.create_session()
            session = session_manager.sessions[session_id]
        
        page = await session_manager.get_page(session_id)
        ig = InstagramClient(page)
        
        result = await ig.login(username, password)
        
        if result.get("challenge"):
            session["challenge_data"] = result
            return SessionResponse(
                session_id=session_id,
                challenge=result,
                logged_in=False,
            )
        
        if result.get("success"):
            session["logged_in"] = True
            session["username"] = username
            return SessionResponse(
                session_id=session_id,
                logged_in=True,
                username=username,
                sessionid=result.get("sessionid"),
            )
        
        return SessionResponse(
            session_id=session_id,
            logged_in=False,
            error=result.get("error", "Login failed"),
        )
        
    except Exception as e:
        logger.exception("Login error")
        raise HTTPException(500, str(e))

@app.post("/auth/challenge/resolve", response_model=SessionResponse)
async def challenge_resolve(data: ChallengeResolveRequest):
    """Resolve a login challenge with security code."""
    try:
        session = await session_manager.get_session(data.session_id)
        page = await session_manager.get_page(data.session_id)
        ig = InstagramClient(page)
        
        result = await ig.resolve_challenge(data.security_code)
        
        if result.get("success") and result.get("logged_in"):
            session["logged_in"] = True
            session["challenge_data"] = None
            return SessionResponse(
                session_id=data.session_id,
                logged_in=True,
                username=session.get("username"),
            )
        
        return SessionResponse(
            session_id=data.session_id,
            logged_in=False,
            error=result.get("error", "Challenge resolution failed"),
        )
    except Exception as e:
        logger.exception("Challenge resolve error")
        raise HTTPException(500, str(e))

@app.get("/auth/settings")
async def auth_settings(session_id: str):
    """Check auth status / get session info."""
    try:
        session = await session_manager.get_session(session_id)
        page = await session_manager.get_page(session_id)
        ig = InstagramClient(page)
        
        profile = await ig.get_profile_info()
        return {
            "logged_in": profile["logged_in"],
            "username": profile.get("username") or session.get("username"),
            "session_id": session_id,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Auth settings error")
        raise HTTPException(500, str(e))

@app.post("/photo/upload")
async def photo_upload(
    session_id: str = Form(...),
    caption: str = Form(""),
    image_url: str = Form(...),  # We'll download the image
):
    """Upload a photo post."""
    try:
        session = await session_manager.get_session(session_id)
        if not session.get("logged_in"):
            raise HTTPException(401, "Not logged in")
        
        page = await session_manager.get_page(session_id)
        ig = InstagramClient(page)
        
        # Download image to temp file
        import tempfile
        import httpx
        
        async with httpx.AsyncClient() as client:
            resp = await client.get(image_url, timeout=30.0)
            resp.raise_for_status()
            
            with tempfile.NamedTemporaryFile(delete=False, suffix=".jpg") as f:
                f.write(resp.content)
                temp_path = f.name
        
        try:
            result = await ig.upload_photo(temp_path, caption)
            return result
        finally:
            try:
                os.unlink(temp_path)
            except:
                pass
                
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Photo upload error")
        raise HTTPException(500, str(e))

@app.get("/user/posts")
async def user_posts(
    session_id: str,
    username: Optional[str] = None,
    amount: int = 12,
):
    """Get recent posts for a user."""
    try:
        session = await session_manager.get_session(session_id)
        if not session.get("logged_in"):
            raise HTTPException(401, "Not logged in")
        
        page = await session_manager.get_page(session_id)
        ig = InstagramClient(page)
        
        target = username or session.get("username")
        if not target:
            raise HTTPException(400, "Username required")
        
        media = await ig.get_media(target, amount)
        return {"items": media}
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("User posts error")
        raise HTTPException(500, str(e))

@app.get("/account/info")
async def account_info(session_id: str):
    """Get account info."""
    try:
        session = await session_manager.get_session(session_id)
        if not session.get("logged_in"):
            raise HTTPException(401, "Not logged in")
        
        page = await session_manager.get_page(session_id)
        ig = InstagramClient(page)
        profile = await ig.get_profile_info()
        
        return {
            "username": profile.get("username") or session.get("username"),
            "logged_in": True,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Account info error")
        raise HTTPException(500, str(e))

@app.get("/account/insights")
async def account_insights(session_id: str):
    """Get account insights."""
    try:
        session = await session_manager.get_session(session_id)
        if not session.get("logged_in"):
            raise HTTPException(401, "Not logged in")
        
        page = await session_manager.get_page(session_id)
        ig = InstagramClient(page)
        insights = await ig.get_insights()
        
        return insights
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Insights error")
        raise HTTPException(500, str(e))

@app.post("/auth/logout")
async def logout(session_id: str):
    """Logout and close session."""
    await session_manager.close_session(session_id)
    return {"success": True}

@app.get("/sessions")
async def list_sessions():
    """List active sessions."""
    return {
        "sessions": [
            {
                "session_id": sid,
                "logged_in": s.get("logged_in"),
                "username": s.get("username"),
                "created_at": s["created_at"].isoformat(),
            }
            for sid, s in session_manager.sessions.items()
        ]
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=settings.HOST, port=settings.PORT)