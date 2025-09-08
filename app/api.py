# api.py
import os, re, time, uuid, shutil
from pathlib import Path
from typing import List, Optional, Dict
import sys
import os
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# Selenium bits (we'll build Chrome in code here; login.py stays as-is)
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

# import your helpers
import Login  # <- your file in the same folder

APP_ROOT       = Path(__file__).parent.resolve()
SESSIONS_ROOT  = APP_ROOT / "sessions"
SESSIONS_ROOT.mkdir(parents=True, exist_ok=True)

ALLOWED_ORIGIN = os.getenv("ALLOWED_ORIGIN", "*")
HEADLESS       = os.getenv("HEADLESS", "1") == "1"

# ---------------------- FastAPI ----------------------
app = FastAPI(title="ForeSync Backend", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[ALLOWED_ORIGIN] if ALLOWED_ORIGIN != "*" else ["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------- Session store ----------------------
class Session:
    def __init__(self, sid: str, driver: webdriver.Chrome, root: Path):
        self.id = sid
        self.driver = driver
        self.root = root
        self.created_at = time.time()

SESSIONS: Dict[str, Session] = {}

def _new_session() -> Session:
    sid = uuid.uuid4().hex
    root = SESSIONS_ROOT / sid
    root.mkdir(parents=True, exist_ok=True)
    driver = _make_driver()
    return Session(sid, driver, root)

def _get_session(sid: str) -> Session:
    s = SESSIONS.get(sid)
    if not s:
        raise HTTPException(status_code=404, detail="Invalid or expired session_id")
    return s

def _cleanup_if_needed(max_age_sec: int = 45 * 60):
    now = time.time()
    to_drop = [sid for sid, s in SESSIONS.items() if now - s.created_at > max_age_sec]
    for sid in to_drop:
        try:
            SESSIONS[sid].driver.quit()
        except Exception:
            pass
        try:
            shutil.rmtree(SESSIONS[sid].root, ignore_errors=True)
        except Exception:
            pass
        SESSIONS.pop(sid, None)

# ---------------------- Chrome builder ----------------------
def _make_driver() -> webdriver.Chrome:
    opts = Options()
    # IMPORTANT flags for Railway / headless Linux
    if HEADLESS:
        opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--window-size=1920,1080")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    # optional: quieter logs
    opts.add_argument("--log-level=3")

    driver = webdriver.Chrome(options=opts)
    driver.set_page_load_timeout(60)
    return driver

# ---------------------- small DOM helpers ----------------------
def _wait_ready(driver, timeout=15):
    WebDriverWait(driver, timeout).until(
        lambda d: d.execute_script("return document.readyState") == "complete"
    )

def _select_dropdown_by_text(driver, css: str, value_text: Optional[str]) -> bool:
    """Pick option by visible text (exact or contains)."""
    if not value_text:
        return False
    try:
        el = WebDriverWait(driver, 6).until(EC.presence_of_element_located((By.CSS_SELECTOR, css)))
        opts = el.find_elements(By.TAG_NAME, "option")
        for o in opts:
            t = (o.text or "").strip()
            if t == value_text or (value_text.lower() in t.lower()):
                o.click()
                try:
                    driver.execute_script("arguments[0].dispatchEvent(new Event('change', {bubbles:true}));", el)
                except Exception:
                    pass
                time.sleep(0.6)
                return True
    except Exception:
        pass
    return False

def _click_submit_login(driver):
    for how, sel in [
        (By.CSS_SELECTOR, "button[type='submit']"),
        (By.XPATH, "//button[contains(.,'Login') or contains(.,'Sign in') or @type='submit']"),
        (By.CSS_SELECTOR, "input[type='submit']"),
    ]:
        try:
            WebDriverWait(driver, 5).until(EC.element_to_be_clickable((how, sel))).click()
            return True
        except Exception:
            continue
    # fallback: try submit first form
    try:
        driver.execute_script("document.querySelector('form')?.submit()")
        return True
    except Exception:
        return False

def _safe_relpath(p: Path) -> str:
    return str(p.relative_to(SESSIONS_ROOT))

# ---------------------- Schemas ----------------------
class StartOut(BaseModel):
    session_id: str
    captcha_case: str
    captcha_png_b64: Optional[str] = None

class RunIn(BaseModel):
    session_id: str
    username: str
    password: str
    captcha_text: Optional[str] = None
    timetable_sem: Optional[str] = None
    attendance_sem: Optional[str] = None
    calendar_sem: Optional[str] = None
    class_group: Optional[str] = None

class AssetsOut(BaseModel):
    ok: bool
    session_id: str
    timetable_png: Optional[str] = None
    attendance_counts_json: Optional[str] = None
    calendar_pngs: List[str] = []
    registered_courses_json: Optional[str] = None
    message: Optional[str] = None

# ---------------------- Endpoints ----------------------
@app.post("/start", response_model=StartOut)
def start():
    _cleanup_if_needed()
    s = _new_session()
    SESSIONS[s.id] = s

    d = s.driver
    d.get(Login.LOGIN_URL)
    d.maximize_window()
    _wait_ready(d)

    # pick student role (same strategy as your login.py)
    for how, sel in [
        (By.XPATH, "//button[contains(., 'Student')]"),
        (By.XPATH, "//a[contains(., 'Student')]"),
        (By.CSS_SELECTOR, "button#student, a#student, button[data-role='student']"),
    ]:
        try:
            WebDriverWait(d, 2).until(EC.element_to_be_clickable((how, sel))).click()
            break
        except Exception:
            pass

    cap = Login.detect_captcha_case(d)
    b64 = None
    if cap == "text":
        try:
            img = d.find_element(By.CSS_SELECTOR, "img[src^='data:image']")
            src = img.get_attribute("src") or ""
            if "," in src:
                b64 = src.split(",", 1)[1]
        except Exception:
            pass

    # NOTE on 3x3/recaptcha: we cannot “click images” from your frontend.
    # We simply report 'image'/'recaptcha'. The UI should ask user to retry later
    # or continue when VTOP shows a text/no captcha.
    return StartOut(session_id=s.id, captcha_case=cap, captcha_png_b64=b64)

def _do_login_and_assets(
    s: Session,
    username: str,
    password: str,
    captcha_text: Optional[str],
    timetable_sem: Optional[str],
    attendance_sem: Optional[str],
    calendar_sem: Optional[str],
    class_group: Optional[str]
) -> AssetsOut:
    d = s.driver
    root = s.root
    (root / "data").mkdir(exist_ok=True, parents=True)
    (root / "academic_calendar").mkdir(exist_ok=True, parents=True)

    # ---- fill credentials ----
    Login.fill_credentials(d, username, password)
    if captcha_text:
        try:
            cap_el = d.find_element(By.ID, "captchaStr")
            cap_el.clear()
            cap_el.send_keys(captcha_text)
        except Exception:
            pass

    _click_submit_login(d)

    # wait for outcome
    end = time.time() + 60
    while time.time() < end:
        time.sleep(0.5)
        if Login.login_success(d): break
        if Login.page_says_wrong_password(d):
            return AssetsOut(ok=False, session_id=s.id, message="Invalid username or password")
        if Login.page_says_wrong_captcha(d):
            return AssetsOut(ok=False, session_id=s.id, message="Invalid captcha")
    if not Login.login_success(d):
        return AssetsOut(ok=False, session_id=s.id, message="Login not confirmed")

    # save session cookies (then copy into this session folder)
    try:
        Login.save_cookies(d, username)
        # move/copy artifacts into session folder
        src = Path("data") / "cookies.json"
        if src.exists():
            shutil.copy2(src, root / "cookies.json")
        src = Path("session.json")
        if src.exists():
            shutil.copy2(src, root / "session.json")
    except Exception:
        pass

    # -------- TIMETABLE ----------
    Login.navigate_to_timetable(d)
    if timetable_sem:
        _select_dropdown_by_text(d, "select#semesterSubId", timetable_sem)
        time.sleep(0.6)
    timetable_png_path = root / "timetable.png"
    Login._screenshot_timetable(d, out_png=str(timetable_png_path))

    # Registered courses (to populate Course Code field in UI)
    reg_json_path = root / "registered_courses.json"
    try:
        Login.parse_registered_courses_dom(d, out_path=str(reg_json_path))
    except Exception:
        pass

    # -------- ATTENDANCE ----------
    Login.navigate_to_attendance(d)
    if attendance_sem:
        _select_dropdown_by_text(d, "select#semesterSubId", attendance_sem)
        # trigger search if a button exists
        for how, sel in [
            (By.XPATH, "//button[contains(.,'Search') or contains(.,'View') or contains(.,'Submit')]"),
            (By.CSS_SELECTOR, "button.btn-primary"),
        ]:
            try:
                WebDriverWait(d, 2).until(EC.element_to_be_clickable((how, sel))).click()
                time.sleep(0.6)
                break
            except Exception:
                continue

    att_counts_path = root / "attendance_counts.json"
    payload = Login.scrape_attendance(
        d, only_counts=True, write_json=True,
        counts_out_path=str(att_counts_path)
    )

    # -------- ACADEMIC CALENDAR ----------
    Login.navigate_to_academic_calendar(d)
    if calendar_sem:
        _select_dropdown_by_text(d, "select#semesterSubId", calendar_sem)
    if class_group:
        _select_dropdown_by_text(d, "select#classGroupId", class_group)

    cal_dir = root / "academic_calendar"
    Login.screenshot_academic_calendar_months(d, out_dir=str(cal_dir))

    # collect calendar images
    cal_pngs = sorted([p for p in cal_dir.glob("*.png")])
    cal_rel = [ _safe_relpath(p) for p in cal_pngs ]

    return AssetsOut(
        ok=True,
        session_id=s.id,
        timetable_png=_safe_relpath(timetable_png_path),
        attendance_counts_json=_safe_relpath(att_counts_path),
        calendar_pngs=cal_rel,
        registered_courses_json=_safe_relpath(reg_json_path) if reg_json_path.exists() else None
    )

@app.post("/run", response_model=AssetsOut)
def run(body: RunIn):
    s = _get_session(body.session_id)
    return _do_login_and_assets(
        s,
        body.username, body.password, body.captcha_text,
        body.timetable_sem, body.attendance_sem, body.calendar_sem, body.class_group
    )

@app.post("/resync", response_model=AssetsOut)
def resync(body: RunIn):
    """Re-run navigations/screenshots using an already logged-in session.
       Username/password are ignored here; only semester/class group picks are used.
    """
    s = _get_session(body.session_id)
    return _do_login_and_assets(
        s,
        username="", password="", captcha_text=None,  # ignored after login
        timetable_sem=body.timetable_sem,
        attendance_sem=body.attendance_sem,
        calendar_sem=body.calendar_sem,
        class_group=body.class_group
    )

@app.get("/file")
def file(path: str = Query(..., description="Relative path under sessions/")):
    # prevent path traversal
    target = (SESSIONS_ROOT / path).resolve()
    if not str(target).startswith(str(SESSIONS_ROOT.resolve())) or not target.exists():
        raise HTTPException(status_code=404, detail="File not found")
    # Disable caching on images/JSON
    resp = FileResponse(target)
    resp.headers["Cache-Control"] = "no-store"
    return resp

@app.get("/courses")
def courses(session_id: str, semester: Optional[str] = None):
    s = _get_session(session_id)
    reg = s.root / "registered_courses.json"
    if not reg.exists():
        return {"courses": []}
    import json
    rows = json.loads(reg.read_text(encoding="utf-8"))
    # Try to extract codes robustly
    codes = set()
    for r in rows:
        # typical keys vary; scan all values
        vals = " ".join([str(v) for v in r.values() if v]).upper()
        for m in re.finditer(r"[A-Z]{2,4}\d{3}[A-Z]?", vals):
            codes.add(m.group(0))
    return {"courses": sorted(codes)}

@app.get("/")
def root():
    return {"ok": True, "msg": "ForeSync Backend running"}
