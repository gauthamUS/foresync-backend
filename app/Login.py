from selenium import webdriver 
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.keys import Keys
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.chrome.options import Options

import getpass
import time
import base64
import json
import os
import re
import shutil  # ← ADDED

# -------------------------- CONFIG --------------------------
LOGIN_URL = "https://vtopcc.vit.ac.in/vtop/login"
ROOT = "https://vtopcc.vit.ac.in"
CONTENT_URL = f"{ROOT}/vtop/content"

# Allow up to 3 password attempts
MAX_PASSWORD_ATTEMPTS = 3

# CAPTCHA wait windows (seconds)
WAIT_NONE = 10           # no captcha
WAIT_TEXT = 60           # simple text captcha
WAIT_IMAGE = 60          # 3x3 image captcha / challenge
WAIT_RECAPTCHA = 10      # protected by reCAPTCHA badge only
# -----------------------------------------------------------


# ----------------------- HELPERS -----------------------
def wait_ready(driver, timeout=10):
    WebDriverWait(driver, timeout).until(
        lambda d: d.execute_script("return document.readyState") == "complete"
    )

def login_success(driver):
    cur_url = driver.current_url.lower()
    page_src = driver.page_source.lower()
    success_markers = [
        "/vtop/content", "/home", "/dashboard", "logout",
        "timetable", "attendance"
    ]
    return any(m in cur_url for m in success_markers) or any(m in page_src for m in success_markers)

def page_says_wrong_password(driver):
    txt = driver.page_source.lower()
    pw_keywords = [
        "invalid password", "incorrect password", "wrong password",
        "invalid credentials", "credentials are invalid",
        "authentication failed", "username or password is incorrect",
        "invalid username or password", "enter valid credentials"
    ]
    return any(k in txt for k in pw_keywords)

def page_says_wrong_captcha(driver):
    txt = driver.page_source.lower()
    cap_keywords = [
        "invalid captcha", "incorrect captcha", "wrong captcha",
        "captcha mismatch", "captcha did not match", "please enter valid captcha",
        "captcha is required"
    ]
    return any(k in txt for k in cap_keywords)

def save_cookies(driver, username_val):
    try:
        cookies = driver.get_cookies()
        cookie_string = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
        cookie_map = {c["name"]: c["value"] for c in cookies}
        payload = {
            "url": driver.current_url,
            "regno": username_val,
            "cookie_string": cookie_string,
            "cookies": cookie_map,
            "_raw": cookies
        }
        with open("session.json", "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        os.makedirs("data", exist_ok=True)
        with open(os.path.join("data", "cookies.json"), "w", encoding="utf-8") as f:
            json.dump(cookies, f, indent=2)
        print("🗂️  Session cookies saved.")
    except Exception as e:
        print(f"⚠️ Could not save session cookies: {e}")

def fill_credentials(driver, username_val, password_val):
    user_el = WebDriverWait(driver, 12).until(EC.presence_of_element_located((By.ID, "username")))
    pass_el = WebDriverWait(driver, 12).until(EC.presence_of_element_located((By.ID, "password")))
    try: user_el.clear()
    except: pass
    user_el.send_keys(username_val)
    try: pass_el.clear()
    except: pass
    pass_el.send_keys(password_val)
# --------------------------------------------------------


# -------------------- DISMISS ALERT MODAL --------------------
def dismiss_alert_modal(driver):
    """Close the 'important info' popup if it appears."""
    try:
        close_btn = WebDriverWait(driver, 3).until(
            EC.element_to_be_clickable((By.ID, "btnClosePopup"))
        )
        try:
            close_btn.click()
        except Exception:
            driver.execute_script("document.getElementById('btnClosePopup')?.click();")
        time.sleep(0.4)
        print("ℹ️ Alert modal detected and closed.")
    except Exception:
        pass
# ------------------------------------------------------------


# =================== CAPTCHA HANDOFF TO USER ===================
def detect_captcha_case(driver):
    """
    Returns one of: 'none', 'text', 'image', 'recaptcha'
    """
    src = driver.page_source.lower()

    # reCAPTCHA badge/iframe
    try:
        if driver.find_elements(By.CSS_SELECTOR, "iframe[src*='recaptcha']") \
           or driver.find_elements(By.CSS_SELECTOR, ".g-recaptcha") \
           or "recaptcha" in src:
            if "select all images" in src or "click on all images" in src:
                return "image"
            return "recaptcha"
    except Exception:
        pass

    # text captcha signs: inline base64 image + input box
    try:
        has_img = driver.find_elements(By.CSS_SELECTOR, "img[src^='data:image']")
        has_input = driver.find_elements(By.ID, "captchaStr")
        if has_img and has_input:
            return "text"
    except Exception:
        pass

    # Image/grid captcha heuristics (wording based)
    if "select all images" in src or "click each image" in src:
        return "image"

    return "none"

def arm_submit_click_probe(driver):
    """
    Instrument the page so we can detect when the user clicks Submit / the form submits.
    Must be called BEFORE the user clicks.
    """
    js = r"""
    (function(){
      try{
        if (window.__vtopSubmitProbeArmed) return;
        window.__vtopSubmitProbeArmed = true;
        window.__userClickedSubmit = false;
        const mark = ()=>{ window.__userClickedSubmit = true; };
        // Any submit-looking button or link
        const clickers = Array.from(document.querySelectorAll("button,input, a")).filter(el=>{
          const t = (el.innerText || el.value || "").trim().toLowerCase();
          if ((el.type || "").toLowerCase() === "submit") return true;
          return /login|sign in|signin|submit|proceed/.test(t);
        });
        clickers.forEach(el=>{
          try { el.addEventListener("click", mark, {capture:true, once:false}); } catch(e){}
        });
        // Any form submit
        document.querySelectorAll("form").forEach(f=>{
          try { f.addEventListener("submit", mark, {capture:true, once:false}); } catch(e){}
        });
        // If the page navigates away right after click, catch it
        window.addEventListener("beforeunload", mark, {capture:true});
      }catch(e){}
    })();
    """
    try:
        driver.execute_script(js)
    except Exception:
        pass


def wait_for_user_submit_click_then_result(driver, *, idle_minutes=15, outcome_timeout=180):
    """
    Wait indefinitely (up to idle_minutes) until user actually clicks Submit or the page
    logs in, then wait outcome_timeout seconds for success/error.
    Returns True on successful login, False if page shows wrong password/captcha,
    otherwise False after idle timeout with no click.
    """
    print(f"🕒 Waiting for you to solve CAPTCHA and click Submit "
          f"(idle window: {idle_minutes} min)…")

    clicked_seen = False
    idle_deadline = time.time() + idle_minutes*60

    while time.time() < idle_deadline:
        time.sleep(0.5)

        # Already logged in?
        if login_success(driver):
            return True

        # Explicit server error states
        if page_says_wrong_password(driver) or page_says_wrong_captcha(driver):
            return False

        # Did user click submit (or form submit/beforeunload fired)?
        try:
            clicked_seen = bool(driver.execute_script("return !!window.__userClickedSubmit;"))
        except Exception:
            clicked_seen = False

        if clicked_seen:
            print("➡️ Submit detected. Processing…")
            end = time.time() + outcome_timeout
            while time.time() < end:
                time.sleep(0.5)
                if login_success(driver):
                    return True
                if page_says_wrong_password(driver) or page_says_wrong_captcha(driver):
                    return False
            # If outcome still unclear, keep waiting in the idle loop (page may be slow).
            clicked_seen = False  # in case page reloaded and probe reset
            arm_submit_click_probe(driver)  # re-arm after potential reload

    print("⌛ No submit detected within the idle window.")
    return False

# ===============================================================
# ------------ OVERLAYS / MODALS: HARD CLOSE ------------
def dismiss_all_overlays(driver, attempts=3):
    """
    Kill any modal/backdrop/overlay that steals focus.
    Stronger than dismiss_alert_modal(): tries buttons + CSS removal.
    """
    for _ in range(attempts):
        closed = False

        # Click obvious close buttons if present
        for how, sel in [
            (By.ID, "btnClosePopup"),
            (By.CSS_SELECTOR, "button#btnClosePopup"),
            (By.CSS_SELECTOR, "button.btn-close"),
            (By.XPATH, "//button[contains(@class,'close') or normalize-space()='Close' or normalize-space()='OK']"),
            (By.XPATH, "//div[contains(@class,'modal-footer')]//button"),
        ]:
            try:
                for el in driver.find_elements(how, sel):
                    if el.is_displayed():
                        el.click()
                        closed = True
                        time.sleep(0.2)
            except Exception:
                pass

        # ESC to close bootstrap modals
        try:
            ActionChains(driver).send_keys(Keys.ESCAPE).perform()
        except Exception:
            pass

        # Brutal fallback: hide modals & remove backdrops
        try:
            driver.execute_script("""
                document.querySelectorAll('.modal.show, .modal[style*="display: block"]').forEach(m=>{
                    m.style.display='none';
                    m.classList.remove('show');
                });
                document.querySelectorAll('.modal-backdrop, .fade.show').forEach(b=>b.remove());
                document.body.style.overflow='auto';
            """)
        except Exception:
            pass

        time.sleep(0.2)
        # If nothing visible, we are done
        try:
            still = driver.find_elements(By.CSS_SELECTOR, ".modal.show, .modal[style*='display: block'], .modal-backdrop")
            if not still:
                return True
        except Exception:
            return True
    return False

# -------------------- NAVIGATE: TIMETABLE (ROBUST) --------------------
def navigate_to_timetable(driver, max_cycles=3):
    """
    Same flow as your working version, but with retries and hard overlay cleanup.
    We *verify UI anchors* (semester select or timetable table) instead of trusting the URL.
    """
    def _kill_overlays():
        # Close known modals and remove backdrops if any block clicks
        try:
            # Clickable close buttons (best effort)
            for how, sel in [
                (By.ID, "btnClosePopup"),
                (By.CSS_SELECTOR, "button#btnClosePopup"),
                (By.CSS_SELECTOR, "button.btn-close"),
                (By.XPATH, "//button[contains(@class,'close') or normalize-space()='Close' or normalize-space()='OK']"),
                (By.XPATH, "//div[contains(@class,'modal-footer')]//button"),
            ]:
                for el in driver.find_elements(how, sel):
                    if el.is_displayed():
                        try: el.click()
                        except Exception: pass
            # Brutal fallback: hide modals & remove backdrops
            driver.execute_script("""
                document.querySelectorAll('.modal.show, .modal[style*="display: block"]').forEach(m=>{
                    m.style.display='none'; m.classList.remove('show');
                });
                document.querySelectorAll('.modal-backdrop, .fade.show').forEach(b=>b.remove());
                document.body.style.overflow='auto';
            """)
        except Exception:
            pass

    def _ui_ready(timeout=12):
        # Real timetable anchors, not just URL
        anchors = EC.any_of(
            EC.presence_of_element_located((By.ID, "semesterSubId")),
            EC.presence_of_element_located((By.XPATH, "//label[normalize-space()='Semester']/following::select[1]")),
            EC.presence_of_element_located((By.ID, "timeTableStyle"))
        )
        try:
            WebDriverWait(driver, timeout).until(anchors)
            return True
        except Exception:
            return False

    for _ in range(max_cycles):
        # 1) Go to /content and clean overlays
        driver.get(CONTENT_URL)
        try: wait_ready(driver, 12)
        except Exception: pass
        time.sleep(0.5)
        dismiss_alert_modal(driver)
        _kill_overlays()
        try: driver.execute_script("window.scrollTo(0,0); document.activeElement && document.activeElement.blur();")
        except Exception: pass

        # 2) Open the left sidebar graduation-cap (Academics)
        opened = False
        try:
            academics_btn = WebDriverWait(driver, 8).until(
                EC.element_to_be_clickable((
                    By.XPATH,
                    "//button[contains(@class,'SideBarMenuBtn')][.//i[contains(@class,'fa-graduation-cap')]]"
                ))
            )
            academics_btn.click()
            opened = True
        except Exception:
            try:
                driver.execute_script("""
                    const btn = Array.from(document.querySelectorAll("button.SideBarMenuBtn"))
                      .find(b => b.querySelector(".fa-graduation-cap"));
                    if (btn) btn.click();
                """)
                opened = True
            except Exception:
                pass

        # 3) Wait for dropdown panel; if not visible, try clicking again
        if opened:
            try:
                WebDriverWait(driver, 6).until(
                    EC.visibility_of_element_located((
                        By.CSS_SELECTOR, "div.SideBarMenuDropDown.dropdown-menu.show"
                    ))
                )
            except Exception:
                try:
                    driver.find_element(
                        By.XPATH,
                        "//button[contains(@class,'SideBarMenuBtn')][.//i[contains(@class,'fa-graduation-cap')]]"
                    ).click()
                    WebDriverWait(driver, 4).until(
                        EC.visibility_of_element_located((
                            By.CSS_SELECTOR, "div.SideBarMenuDropDown.dropdown-menu.show"
                        ))
                    )
                except Exception:
                    pass

        # 4) Click "Time Table" (multiple strategies)
        clicked_tt = False
        for how, sel in [
            (By.CSS_SELECTOR, "a.systemBtnMenu[data-url*='StudentTimeTableChn']"),
            (By.XPATH, "//a[contains(@class,'systemBtnMenu') and contains(@data-url,'StudentTimeTableChn')]"),
            (By.XPATH, "//a[normalize-space()='Time Table' or contains(., 'Time Table')]"),
        ]:
            try:
                WebDriverWait(driver, 6).until(EC.element_to_be_clickable((how, sel))).click()
                clicked_tt = True
                break
            except Exception:
                continue

        if not clicked_tt:
            # fallback direct URL
            try:
                driver.get(CONTENT_URL + "?menu=studentTimetableChn")
            except Exception:
                pass

        # 5) Clean overlays again and verify actual UI anchors
        dismiss_alert_modal(driver)
        _kill_overlays()
        if _ui_ready(timeout=12):
            return  # success

        # Retry cycle: small pause before trying again
        time.sleep(0.8)

    # If we exit the loop, we couldn't confirm the timetable UI
    print("⚠️ Could not confirm Time Table UI after retries.")
# ================= END NAVIGATE TIMETABLE ===================
def select_semester_if_needed(driver, allow_manual_seconds=30):
    """
    Timetable semester selector using shortforms S1, S2, ...
    - Waits briefly; if not found, waits up to 30s.
    - If you press ENTER, you get up to 30s to pick the semester manually in the UI.
    """
    # Try quickly first
    sel = None
    try:
        sel = WebDriverWait(driver, 4).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "select#semesterSubId"))
        )
    except Exception:
        print("ℹ️ No semester dropdown detected; waiting up to 30s for it to appear...")
        try:
            sel = WebDriverWait(driver, allow_manual_seconds).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "select#semesterSubId"))
            )
            print("✅ Semester dropdown appeared.")
        except Exception:
            print("ℹ️ Continuing with default/last used semester.")
            return None

    options = [o for o in sel.find_elements(By.TAG_NAME, "option")
               if (o.get_attribute("value") or "").strip()]
    if not options:
        print("ℹ️ Semester dropdown present but no options found.")
        return None

    print("\nAvailable Semesters (Time Table):")
    for idx, opt in enumerate(options):
        lbl = (opt.text or "").strip() or opt.get_attribute("value")
        print(f"S{idx+1}: {lbl}")

    choice = input("Enter semester shortform for Timetable (S1, S2, ... or ENTER to pick manually in browser): ").strip().upper()
    if not choice:
        # Manual selection window in the page
        try:
            before_val = sel.get_attribute("value")
        except Exception:
            before_val = ""
        print(f"⏳ You have {allow_manual_seconds}s to select a semester in the browser...")
        end = time.time() + allow_manual_seconds
        changed_label = None
        while time.time() < end:
            try:
                cur_val = sel.get_attribute("value")
                if cur_val != before_val:
                    changed_label = driver.execute_script(
                        "var s=arguments[0]; return s.options[s.selectedIndex]?.text || '';",
                        sel
                    )
                    print(f"✅ Semester changed to: {changed_label}")
                    break
            except Exception:
                pass
            time.sleep(1)
        if not changed_label:
            # Return current selection label
            try:
                changed_label = driver.execute_script(
                    "var s=arguments[0]; return s.options[s.selectedIndex]?.text || '';",
                    sel
                )
            except Exception:
                changed_label = None
        return (changed_label or "").strip() or None

    # Shortcut S#
    try:
        i = int(choice.replace("S", "")) - 1
        options[i].click()
        time.sleep(1)
        return (options[i].text or "").strip()
    except Exception:
        print("⚠️ Invalid choice or click failed; proceeding without explicit semester selection.")
        return None
# ===================== REGISTERED COURSES (DOM) =====================
def parse_registered_courses_dom(driver, out_path=os.path.join("data", "registered_courses.json")):
    """
    Parse the upper 'Registered & Approved Courses' table using header-aware DOM parsing.
    Saves JSON and returns list[dict]. Non-destructive to your flow.
    """
    os.makedirs("data", exist_ok=True)

    cand_tables = driver.find_elements(By.XPATH, "//table[.//th]")
    target = None
    header_map = {}
    for tbl in cand_tables:
        try:
            heads = [th.text.strip() for th in tbl.find_elements(By.XPATH, ".//th")]
            norm = [h.lower().replace(" ", "") for h in heads]
            if any("course" in h for h in norm) and (any("slot" in h for h in norm) or any("venue" in h for h in norm)):
                target = tbl
                header_map = {i: heads[i].strip() for i in range(len(heads))}
                break
        except Exception:
            continue

    if target is None:
        print("⚠️ Registered Courses table not found via DOM.")
        return []

    rows = target.find_elements(By.XPATH, ".//tr[.//td]")
    out = []

    for tr in rows:
        tds = tr.find_elements(By.XPATH, "./td")
        if not tds:
            continue
        rec = {}
        for i, td in enumerate(tds):
            key = header_map.get(i, f"Col{i+1}")
            txt = td.text.strip().replace("\n", " ").replace("\r", " ")
            txt = re.sub(r"\s+", " ", txt)
            rec[key] = txt
        # normalize a few useful fields
        if "Course" in rec:
            m = re.search(r"Total\s+Number\s+Of\s+Credits:\s*([0-9]+(?:\.[0-9]+)?)", tr.text, flags=re.I)
            if m:
                rec["CourseCode"] = m.group(1)
        for k in list(rec.keys()):
            if k.lower().startswith("slot"):
                rec["Slot"] = rec[k]
                break
        out.append(rec)

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2)
    print(f"✅ Registered Courses saved to {out_path} (rows: {len(out)})")
    return out


# ===================== WEEKLY TIMETABLE (screenshot) =====================

def _screenshot_timetable(driver, out_png="data/timetable_debug.png"):
    """
    Always save a screenshot of the timetable container (#timeTableStyle).
    Returns path to PNG.
    """
    try:
        tab = WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.ID, "timeTableStyle"))
        )
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", tab)
        os.makedirs(os.path.dirname(out_png), exist_ok=True)
        tab.screenshot(out_png)
        print(f"📸 Timetable screenshot saved: {out_png}")
        return out_png
    except Exception as e:
        print(f"⚠️ Could not screenshot timetable: {e}")
        return None

# -------------------- NAVIGATE: ATTENDANCE --------------------
def navigate_to_attendance(driver):
    driver.get(CONTENT_URL)
    try:
        wait_ready(driver, 12)
    except Exception:
        pass
    time.sleep(0.5)

    dismiss_alert_modal(driver)

    # Click the left sidebar graduation-cap (Academics)
    try:
        academics_btn = WebDriverWait(driver, 8).until(
            EC.element_to_be_clickable((
                By.XPATH,
                "//button[contains(@class,'SideBarMenuBtn')][.//i[contains(@class,'fa-graduation-cap')]]"
            ))
        )
        academics_btn.click()
    except Exception:
        try:
            driver.execute_script("""
                const btn = Array.from(document.querySelectorAll("button.SideBarMenuBtn"))
                  .find(b => b.querySelector(".fa-graduation-cap"));
                if (btn) btn.click();
            """)
        except Exception:
            pass

    # Wait dropdown
    try:
        WebDriverWait(driver, 6).until(
            EC.visibility_of_element_located((
                By.CSS_SELECTOR, "div.SideBarMenuDropDown.dropdown-menu.show"
            ))
        )
    except Exception:
        try:
            driver.find_element(
                By.XPATH,
                "//button[contains(@class,'SideBarMenuBtn')][.//i[contains(@class,'fa-graduation-cap')]]"
            ).click()
            WebDriverWait(driver, 4).until(
                EC.visibility_of_element_located((
                    By.CSS_SELECTOR, "div.SideBarMenuDropDown.dropdown-menu.show"
                ))
            )
        except Exception:
            pass

    # Click "Class Attendance"
    clicked_att = False
    for how, sel in [
        (By.CSS_SELECTOR, "a.systemBtnMenu[data-url*='StudentAttendance']"),
        (By.XPATH, "//a[contains(@class,'systemBtnMenu') and contains(@data-url,'StudentAttendance')]"),
        (By.XPATH, "//a[normalize-space()='Class Attendance' or contains(., 'Class Attendance')]"),
    ]:
        try:
            WebDriverWait(driver, 6).until(EC.element_to_be_clickable((how, sel))).click()
            clicked_att = True
            break
        except Exception:
            continue

    if not clicked_att:
        try:
            driver.get(CONTENT_URL + "?menu=StudentAttendance")
        except Exception:
            pass

    dismiss_alert_modal(driver)

    try:
        WebDriverWait(driver, 12).until(
            EC.any_of(
                EC.presence_of_element_located((By.XPATH, "//div[contains(@class,'table-responsive')]//table")),
                EC.presence_of_element_located((By.XPATH, "//h5[contains(.,'Attendance')]")),
                EC.presence_of_element_located((By.ID, "semesterSubId"))
            )
        )
    except Exception:
        time.sleep(2)


def select_attendance_semester_if_needed(driver):
    """
    Attendance semester selector using shortforms S1, S2, ...
    Works on the attendance page's <select id="semesterSubId"> you provided.
    If present, lists options and allows user to choose; otherwise no-op.
    """
    try:
        dropdown = WebDriverWait(driver, 4).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "select#semesterSubId"))
        )
    except Exception:
        print("ℹ️ No attendance semester dropdown detected; continuing with default/last used semester.")
        return None

    options = dropdown.find_elements(By.TAG_NAME, "option")
    if not options:
        print("ℹ️ Attendance semester dropdown present but no options found.")
        return None

    print("\nAvailable Semesters (Attendance):")
    for idx, opt in enumerate(options):
        label = (opt.text or "").strip() or opt.get_attribute("value")
        print(f"S{idx+1}: {label}")

    choice = input("Enter semester shortform for Attendance (S1, S2, ... or ENTER to skip): ").strip().upper()
    if not choice:
        print("↪️ Skipping manual selection (using current/last).")
        return None

    chosen_label = None
    try:
        i = int(choice.replace("S", "")) - 1
        options[i].click()
        chosen_label = options[i].text
        time.sleep(0.8)

        try:
            driver.execute_script("arguments[0].dispatchEvent(new Event('change', {bubbles:true}));", dropdown)
        except Exception:
            pass

        for how, sel in [
            (By.XPATH, "//button[contains(.,'Search') or contains(.,'View') or contains(.,'Submit')]"),
            (By.CSS_SELECTOR, "button.btn-primary"),
        ]:
            try:
                btn = WebDriverWait(driver, 2).until(EC.element_to_be_clickable((how, sel)))
                btn.click()
                time.sleep(0.8)
                break
            except Exception:
                continue

        try:
            WebDriverWait(driver, 6).until(
                EC.presence_of_element_located((By.XPATH, "//div[contains(@class,'table-responsive')]//table"))
            )
        except Exception:
            pass

    except Exception:
        print("⚠️ Invalid choice or click failed; proceeding without explicit semester selection.")
    return chosen_label
# --------------------------------------------------------------
# -------------------- SCRAPE ATTENDANCE SUMMARY (supports minimal mode) --------------------
def scrape_attendance(
    driver,
    *,
    write_json=True,
    out_path=os.path.join("data", "attendance.json"),
    only_counts=False,
    counts_out_path=os.path.join("data", "attendance_counts.json")
):
    """
    Extract rows and total credits from attendance summary table.
    If only_counts=True, return just {course_code, attended, total} per course.
    """
    os.makedirs("data", exist_ok=True)

    table = WebDriverWait(driver, 12).until(
        EC.presence_of_element_located((By.XPATH, "//div[contains(@class,'table-responsive')]//table"))
    )

    # Try to read header so we aren't tied to fixed column indices.
    # We'll fall back to index positions if header isn't present.
    header_map = {}
    try:
        ths = table.find_elements(By.XPATH, ".//thead//th[normalize-space()]")
        norm = lambda s: re.sub(r"\s+", " ", s.strip().lower())
        target_names = {
            "course_code": {"course code", "course code*"},   # tolerate minor variants
            "attended": {"attended classes", "attended"},
            "total": {"total classes", "total"}
        }
        for idx, th in enumerate(ths):
            name = norm(th.text)
            for key, alts in target_names.items():
                if name in alts:
                    header_map[key] = idx
    except Exception:
        header_map = {}

    # Note text (red line)
    note = ""
    try:
        note_el = driver.find_element(By.XPATH, "//div[contains(@class,'table-responsive')]//h5/span")
        note = note_el.text.strip()
    except Exception:
        pass

    rows_out = []
    tb = table.find_element(By.TAG_NAME, "tbody")
    trs = tb.find_elements(By.TAG_NAME, "tr")

    total_credits = None

    def cell(td):
        try:
            ps = td.find_elements(By.TAG_NAME, "p")
            if ps:
                return " | ".join(p.text.strip() for p in ps if p.text.strip())
        except Exception:
            pass
        return td.text.strip()

    def to_int(x):
        try:
            return int(str(x).strip())
        except:
            return None

    for tr in trs:
        tds = tr.find_elements(By.TAG_NAME, "td")

        # Skip the credits / footer rows
        if len(tds) == 1 or "Total Number Of Credits" in tr.text:
            try:
                m = re.search(r"Total\s+Number\s+Of\s+Credits:\s*([0-9]+(?:\.[0-9]+)?)", tr.text, flags=re.I)
                if m:
                    total_credits = float(m.group(1))
            except Exception:
                pass
            continue

        if len(tds) < 11:  # need at least up to 'Total Classes'
            continue

        # -------- Minimal extraction using header first, else index fallback --------
        if only_counts:
            # Prefer header positions if found
            if header_map:
                i_code = header_map.get("course_code", 1)  # fallback to old index
                i_attd = header_map.get("attended", 9)
                i_totl = header_map.get("total", 10)
            else:
                # Original fixed indices from your existing parser
                i_code, i_attd, i_totl = 1, 9, 10

            course_code = cell(tds[i_code])
            attended    = to_int(cell(tds[i_attd]))
            total       = to_int(cell(tds[i_totl]))

            # guard: only keep well-formed rows
            if course_code and (attended is not None) and (total is not None):
                rows_out.append({
                    "course_code": course_code,
                    "attended": attended,
                    "total": total
                })
            continue

        # -------- Full row extraction (your existing logic) --------
        if len(tds) < 14:
            continue

        slno                 = cell(tds[0])
        course_code          = cell(tds[1])
        course_title         = cell(tds[2])
        course_type          = cell(tds[3])
        slot                 = cell(tds[4])
        faculty              = cell(tds[5])
        attendance_type      = cell(tds[6])
        registration_dt      = cell(tds[7])
        attendance_date      = cell(tds[8])
        attended             = cell(tds[9])
        total                = cell(tds[10])
        percentage           = cell(tds[11])
        status               = cell(tds[12])

        view_info = {"href": "", "onclick": "", "regid": "", "slot": ""}
        try:
            view_a = tds[13].find_element(By.TAG_NAME, "a")
            view_info["href"] = view_a.get_attribute("href") or ""
            view_info["onclick"] = view_a.get_attribute("onclick") or ""
            m = re.search(r"processViewAttendanceDetail\('([^']+)'\s*,\s*'([^']+)'\)", view_info["onclick"])
            if m:
                view_info["regid"] = m.group(1)
                view_info["slot"]  = m.group(2)
        except Exception:
            pass

        row = {
            "slno": to_int(slno),
            "course_code": course_code,
            "course_title": course_title,
            "course_type": course_type,
            "slot": slot,
            "faculty": faculty,
            "attendance_type": attendance_type,
            "registration_datetime": registration_dt,
            "attendance_date": attendance_date,
            "attended": to_int(attended),
            "total": to_int(total),
            "percentage": to_int(percentage) if percentage and percentage != "-" else None,
            "status": status,
            "view": view_info
        }
        rows_out.append(row)

    payload = {
        "rows": rows_out,
        "total_credits": total_credits,
        "note": note
    }

    if write_json:
        path = counts_out_path if only_counts else out_path
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
        if only_counts:
            print(f"✅ Attendance (counts) saved to {path} | Rows: {len(rows_out)}")
        else:
            print(f"✅ Attendance (summary) saved to {path} | Rows: {len(rows_out)} | Credits: {total_credits}")

    return payload


# -------------------- SCRAPE ATTENDANCE SUMMARY --------------------
'''def scrape_attendance(driver, *, write_json=True, out_path=os.path.join("data", "attendance.json")):
    ...
'''
# =================== END TIMETABLE FROM ATTENDANCE ===================

# -------------------- NAVIGATE: ACADEMIC CALENDAR --------------------
def navigate_to_academic_calendar(driver):
    """
    Open the Academics (graduation-cap) menu, then click 'Academic Calendar'.
    Robust to different selectors and menu states, similar to timetable nav.
    """
    # Always reset to /content so the sidebar is present
    driver.get(CONTENT_URL)
    try:
        wait_ready(driver, 12)
    except Exception:
        pass
    time.sleep(0.5)

    dismiss_alert_modal(driver)

    # Click the left sidebar graduation-cap (Academics)
    try:
        academics_btn = WebDriverWait(driver, 8).until(
            EC.element_to_be_clickable((
                By.XPATH,
                "//button[contains(@class,'SideBarMenuBtn')][.//i[contains(@class,'fa-graduation-cap')]]"
            ))
        )
        academics_btn.click()
    except Exception:
        try:
            driver.execute_script("""
                const btn = Array.from(document.querySelectorAll("button.SideBarMenuBtn"))
                  .find(b => b.querySelector(".fa-graduation-cap"));
                if (btn) btn.click();
            """)
        except Exception:
            pass

    # Wait for dropdown panel (same pattern as timetable nav)
    try:
        WebDriverWait(driver, 6).until(
            EC.visibility_of_element_located((
                By.CSS_SELECTOR, "div.SideBarMenuDropDown.dropdown-menu.show"
            ))
        )
    except Exception:
        try:
            driver.find_element(
                By.XPATH,
                "//button[contains(@class,'SideBarMenuBtn')][.//i[contains(@class,'fa-graduation-cap')]]"
            ).click()
            WebDriverWait(driver, 4).until(
                EC.visibility_of_element_located((
                    By.CSS_SELECTOR, "div.SideBarMenuDropDown.dropdown-menu.show"
                ))
            )
        except Exception:
            pass

    # Click "Academic Calendar"
    clicked = False
    for how, sel in [
        (By.CSS_SELECTOR, "a.systemBtnMenu[data-url*='academics/common/CalendarPreview']"),
        (By.XPATH, "//a[contains(@class,'systemBtnMenu') and contains(@data-url,'CalendarPreview')]"),
        (By.XPATH, "//a[normalize-space()='Academic Calendar' or contains(., 'Academic Calendar')]"),
    ]:
        try:
            WebDriverWait(driver, 8).until(EC.element_to_be_clickable((how, sel))).click()
            clicked = True
            break
        except Exception:
            continue

    if not clicked:
        # Fallback direct URL attempt (best-effort)
        try:
            driver.get(CONTENT_URL + "?menu=academics/common/CalendarPreview")
        except Exception:
            pass

    dismiss_alert_modal(driver)

    # Wait for any of the page anchors: semester dropdown, class group, or month buttons / calendar block
    try:
        WebDriverWait(driver, 12).until(
            EC.any_of(
                EC.presence_of_element_located((By.ID, "semesterSubId")),
                EC.presence_of_element_located((By.ID, "classGroupId")),
                EC.presence_of_element_located((By.XPATH, "//div[@id='list-wrapper']")),
                EC.presence_of_element_located((By.XPATH, "//a[contains(@onclick,'processViewCalendar')]"))
            )
        )
    except Exception:
        time.sleep(1.5)
# ---------------------------------------------------------------------


def _get_selected_label(select_el):
    try:
        opt = select_el.find_element(By.XPATH, "./option[@selected]")
    except Exception:
        try:
            opt = select_el.find_element(By.XPATH, "./option[@selected='selected']")
        except Exception:
            opts = select_el.find_elements(By.TAG_NAME, "option")
            opt = next((o for o in opts if o.is_selected()), opts[0] if opts else None)
    return (opt.text or opt.get_attribute("value")).strip() if opt else ""


def select_acad_semester_with_shortcuts(driver, allow_manual_seconds=30):
    """
    Academic Calendar semester selector (S1, S2, ...). 
    - If user presses ENTER (no input), we wait up to `allow_manual_seconds` for manual selection in the page.
    - If dropdown missing at first, we give the page time to render & retry.
    """
    # First try to find it quickly
    dropdown = None
    for _ in range(2):  # short retry to handle late render
        try:
            dropdown = WebDriverWait(driver, 4).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "select#semesterSubId"))
            )
            break
        except Exception:
            time.sleep(0.8)

    if dropdown is None:
        print("ℹ️ No semester dropdown detected; waiting up to 30s for it to appear (or for default to load).")
        end = time.time() + allow_manual_seconds
        while time.time() < end:
            try:
                dropdown = driver.find_element(By.CSS_SELECTOR, "select#semesterSubId")
                if dropdown:
                    break
            except Exception:
                pass
            time.sleep(1.0)

    if dropdown is None:
        print("ℹ️ Semester dropdown still not present; continuing with default/last used semester on Academic Calendar.")
        return None

    options = dropdown.find_elements(By.TAG_NAME, "option")
    if not options:
        print("ℹ️ Semester dropdown present but no options found (Academic Calendar).")
        return None

    print("\nAvailable Semesters (Academic Calendar):")
    for idx, opt in enumerate(options):
        label = (opt.text or "").strip() or opt.get_attribute("value")
        print(f"S{idx+1}: {label}")

    choice = input(f"Enter semester shortform for Academic Calendar (S1, S2, ... or ENTER to wait {allow_manual_seconds}s for manual page selection): ").strip().upper()

    if not choice:
        # Let the user pick directly in the webpage (watch for value change)
        try:
            initial_val = dropdown.get_attribute("value")
        except Exception:
            initial_val = ""
        print(f"🕒 Waiting up to {allow_manual_seconds}s for manual semester selection in the page...")
        end = time.time() + allow_manual_seconds
        while time.time() < end:
            try:
                cur_val = dropdown.get_attribute("value")
                if cur_val != initial_val:
                    time.sleep(0.6)  # let the page update dependent UI
                    chosen = _get_selected_label(dropdown)
                    print(f"✅ Semester selected (manual): {chosen}")
                    return chosen
            except Exception:
                pass
            time.sleep(0.5)
        print("↪️ No manual change detected; using current selection.")
        return _get_selected_label(dropdown)

    # Shortcut path
    try:
        idx = int(choice.replace("S", "")) - 1
        options[idx].click()
        try:
            driver.execute_script("arguments[0].dispatchEvent(new Event('change', {bubbles:true}));", dropdown)
        except Exception:
            pass
        time.sleep(0.8)
        chosen = _get_selected_label(dropdown)
        print(f"✅ Semester selected: {chosen}")
        return chosen
    except Exception:
        print("⚠️ Invalid choice or click failed; proceeding with current selection.")
        return _get_selected_label(dropdown)


def select_acad_class_group_with_shortcuts(driver, allow_manual_seconds=15):
    """
    Academic Calendar 'Class Group' selector (S1, S2, ...) for #classGroupId.
    Works like the semester picker; default is often 'COMB' (Combined).
    """
    dropdown = None
    try:
        dropdown = WebDriverWait(driver, 5).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "select#classGroupId"))
        )
    except Exception:
        pass

    if dropdown is None:
        print("ℹ️ No class group dropdown detected; waiting briefly for it to appear...")
        end = time.time() + allow_manual_seconds
        while time.time() < end:
            try:
                dropdown = driver.find_element(By.CSS_SELECTOR, "select#classGroupId")
                if dropdown:
                    break
            except Exception:
                pass
            time.sleep(0.6)

    if dropdown is None:
        print("ℹ️ Class group dropdown still not present; continuing with default.")
        return None

    options = dropdown.find_elements(By.TAG_NAME, "option")
    if not options:
        print("ℹ️ Class group dropdown present but no options found.")
        return None

    print("\nAvailable Class Groups (Academic Calendar):")
    for idx, opt in enumerate(options):
        label = (opt.text or "").strip() or opt.get_attribute("value")
        print(f"S{idx+1}: {label}")

    choice = input(f"Enter class group shortform (S1, S2, ... or ENTER to wait {allow_manual_seconds}s for manual): ").strip().upper()
    if not choice:
        # Let user manually select in UI
        try:
            initial_val = dropdown.get_attribute("value")
        except Exception:
            initial_val = ""
        print(f"🕒 Waiting up to {allow_manual_seconds}s for manual class group selection...")
        end = time.time() + allow_manual_seconds
        while time.time() < end:
            try:
                cur_val = dropdown.get_attribute("value")
                if cur_val != initial_val:
                    time.sleep(0.6)
                    chosen = _get_selected_label(dropdown)
                    print(f"✅ Class group selected (manual): {chosen}")
                    return chosen
            except Exception:
                pass
            time.sleep(0.5)
        print("↪️ No manual change detected; using current selection.")
        return _get_selected_label(dropdown)

    try:
        idx = int(choice.replace("S", "")) - 1
        options[idx].click()
        try:
            driver.execute_script("arguments[0].dispatchEvent(new Event('change', {bubbles:true}));", dropdown)
        except Exception:
            pass
        time.sleep(0.6)
        chosen = _get_selected_label(dropdown)
        print(f"✅ Class group selected: {chosen}")
        return chosen
    except Exception:
        print("⚠️ Invalid choice or click failed; proceeding with current selection.")
        return _get_selected_label(dropdown)
# ===== Full-page, high-DPI screenshot via Chrome DevTools =====
def _fullpage_png(driver):
    # Ensure we have CDP
    try:
        driver.execute_cdp_cmd("Page.enable", {})
    except Exception:
        pass

    # Get layout metrics to compute full content size
    metrics = driver.execute_cdp_cmd("Page.getLayoutMetrics", {})
    # Some Chrome versions expose different keys; be tolerant
    content_size = metrics.get("contentSize") or metrics.get("cssLayoutViewport") or {}
    width  = int(content_size.get("width", 1920))
    height = int(content_size.get("height", 1080))

    # Temporarily set a large viewport so capture covers the page
    try:
        driver.execute_cdp_cmd("Emulation.setDeviceMetricsOverride", {
            "mobile": False,
            "width": width,
            "height": height,
            "deviceScaleFactor": 2,  # 2x for crispness
        })
    except Exception:
        pass

    # Capture screenshot beyond viewport (Chrome supports this)
    png_b64 = driver.execute_cdp_cmd("Page.captureScreenshot", {
        "format": "png",
        "fromSurface": True,
        "captureBeyondViewport": True
    })["data"]

    # Reset metrics (best effort)
    try:
        driver.execute_cdp_cmd("Emulation.clearDeviceMetricsOverride", {})
    except Exception:
        pass

    return base64.b64decode(png_b64)
# ===== Academic Calendar: click through months and save full-page PNGs =====
MONTH_NAMES = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]

def _kill_overlays_soft(driver):
    try:
        driver.execute_script("""
            document.querySelectorAll('.modal.show, .modal[style*="display: block"]').forEach(m=>{ 
                m.style.display='none'; m.classList.remove('show'); 
            });
            document.querySelectorAll('.modal-backdrop, .fade.show').forEach(b=>b.remove());
            document.body.style.overflow='auto';
        """)
    except Exception:
        pass

MONTH_NAMES = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]

def _norm_month_label(label: str):
    """
    Normalize various month label shapes into ('Jan', '2025') or ('Jan', '')
    Accepts: 'JUL-2025', 'July 2025', 'Jul_2025', 'Jul', 'JUL', etc.
    Returns (mon3, year_str) or (None, None) if not a month.
    """
    s = (label or "").strip()
    if not s:
        return None, None

    # unify separators and case
    s = s.replace("_", "-").replace("/", "-").replace("  ", " ")
    # Try "Mon-YYYY" or "Mon YYYY" or "Month YYYY"
    m = re.match(r"^\s*([A-Za-z]{3,9})[-\s]?(\d{4})?\s*$", s)
    if not m:
        return None, None
    mon = m.group(1)[:3].title()
    yr  = (m.group(2) or "").strip()
    if mon not in MONTH_NAMES:
        # Try mapping long month names -> short
        long_to_short = {
            "January":"Jan","February":"Feb","March":"Mar","April":"Apr","May":"May","June":"Jun",
            "July":"Jul","August":"Aug","September":"Sep","October":"Oct","November":"Nov","December":"Dec"
        }
        mon = long_to_short.get(m.group(1).title(), mon)
    if mon not in MONTH_NAMES:
        return None, None
    return mon, yr

def _find_month_controls(driver):
    """
    Return a list of tuples: [(label, element, onclick_js), ...]
    Label is normalized like 'Jul 2025' (year optional).
    """
    controls, seen = [], set()
    candidates = []

    # Common clickable candidates
    candidates += driver.find_elements(By.XPATH, "//a[contains(@onclick,'processViewCalendar')]")
    candidates += driver.find_elements(By.XPATH, "//div[@id='list-wrapper']//a | //ul//a | //a")
    candidates += driver.find_elements(By.XPATH, "//button[normalize-space()]")

    def push(lbl, el, js):
        mon, yr = _norm_month_label(lbl)
        if not mon: 
            return
        label = f"{mon} {yr}".strip()
        if label in seen:
            return
        seen.add(label)
        controls.append((label, el, js))

    for el in candidates:
        try:
            lbl = (el.text or "").strip() or (el.get_attribute("title") or "").strip() or (el.get_attribute("data-month") or "").strip()
            if lbl:
                push(lbl, el, None)
            # If it has explicit onclick, keep as JS fallback too
            js = el.get_attribute("onclick") or ""
            if "processViewCalendar" in js and not lbl:
                # Try to pull month from JS: processViewCalendar('Jul','2025',...)
                m1 = re.search(r"processViewCalendar\('([A-Za-z]{3,9})'", js)
                y1 = re.search(r"processViewCalendar\('[A-Za-z]{3,9}'\s*,\s*'(\d{4})'", js)
                if m1:
                    push(f"{m1.group(1)} {y1.group(1) if y1 else ''}", None, js)
        except Exception:
            continue

    # Sort safely; if parsing fails for some, keep original order
    def month_key(lbl):
        mon, yr = _norm_month_label(lbl)
        m = MONTH_NAMES.index(mon) if mon in MONTH_NAMES else 99
        y = int(yr) if (yr and yr.isdigit()) else 0
        return (y, m)

    try:
        controls.sort(key=lambda t: month_key(t[0]))
    except Exception:
        # keep DOM order if any unexpected label sneaks in
        pass
    return controls
def _wait_calendar_render(driver, timeout=8):
    """
    Wait until the academic calendar page has some visible content 
    (month list, calendar grid, or header).
    """
    try:
        WebDriverWait(driver, timeout).until(
            EC.any_of(
                EC.presence_of_element_located((By.ID, "list-wrapper")),
                EC.presence_of_element_located((By.XPATH, "//div[contains(@class,'calendar') or contains(@id,'calendar')]")),
                EC.presence_of_element_located((By.XPATH, "//h4[contains(.,'Academic Calendar')]|//h5[contains(.,'Academic Calendar')]"))
            )
        )
    except Exception:
        time.sleep(1)
def _boost_dpi(driver, scale=2):
    """Increase deviceScaleFactor for crisp text (no visual zoom for user)."""
    try:
        driver.execute_cdp_cmd("Emulation.setDeviceMetricsOverride", {
            "mobile": False,
            "width": 1920,          # wide enough for calendar
            "height": 1080,
            "deviceScaleFactor": float(scale),
        })
    except Exception:
        pass

def _reset_dpi(driver):
    try:
        driver.execute_cdp_cmd("Emulation.clearDeviceMetricsOverride", {})
    except Exception:
        pass

def _hide_chrome_chrome(driver):
    """Hide left sidebar/top nav to maximize calendar area in screenshots."""
    try:
        driver.execute_script("""
            const hide = sel => document.querySelectorAll(sel).forEach(n=>n && (n.style.display='none'));
            hide('div.SideBarMenu, nav.navbar, header, .navbar, .header');  // site header-ish
            // shrink body paddings
            document.body.style.paddingTop = '0px';
            document.body.style.marginTop = '0px';
            // make calendar area wider if wrapped
            const wrap = document.querySelector('#list-wrapper')?.closest('.container, .container-fluid');
            if (wrap) wrap.style.maxWidth = '100%';
        """)
    except Exception:
        pass

def _find_calendar_container(driver):
    """Return the scrollable calendar grid element."""
    selectors = [
        "#list-wrapper",                      # common on VTOP
        "div[id*='calendar']",
        "div.calendar, div.Calendar"
    ]
    for sel in selectors:
        try:
            el = driver.find_element(By.CSS_SELECTOR, sel)
            if el and el.size["height"] > 0 and el.is_displayed():
                return el
        except Exception:
            continue
    # fallback: central main content
    try:
        return driver.find_element(By.XPATH, "//div[contains(@class,'col') and .//table] | //main")
    except Exception:
        return None

def _element_scroll_slices(driver, el, out_dir, base_name, overlap_px=80):
    """
    Scroll a tall, scrollable element and save multiple PNGs of the visible portion.
    Files: {base_name}_part01.png, part02.png, ...
    """
    # ensure element at top
    driver.execute_script("arguments[0].scrollIntoView({block:'start'});", el)
    # compute scrollable metrics
    metrics = driver.execute_script("""
        const el = arguments[0];
        return {
          scrollHeight: el.scrollHeight,
          clientHeight: el.clientHeight
        };
    """, el)
    scroll_h = int(metrics.get("scrollHeight", 0))
    view_h   = int(metrics.get("clientHeight", 0))
    if not scroll_h or not view_h:
        # just screenshot once
        path = os.path.join(out_dir, f"{base_name}_part01.png")
        el.screenshot(path)
        return [path]

    step = max(1, view_h - int(overlap_px))
    paths, y = [], 0
    i = 1
    while True:
        driver.execute_script("arguments[0].scrollTop = arguments[1];", el, y)
        time.sleep(0.35)  # allow repaint
        path = os.path.join(out_dir, f"{base_name}_part{i:02d}.png")
        el.screenshot(path)     # captures only the visible slice of the element
        paths.append(path)
        i += 1
        if y + view_h >= scroll_h - 2:   # reached bottom
            break
        y = min(y + step, scroll_h - view_h)
    return paths

def screenshot_academic_calendar_months(driver, out_dir=os.path.join("data", "academic_calendar")):
    # ====== RESET OUTPUT FOLDER EACH RUN (ADDED) ======
    if os.path.exists(out_dir):
        try:
            shutil.rmtree(out_dir)
        except Exception:
            pass
    os.makedirs(out_dir, exist_ok=True)
    # ==================================================

    _kill_overlays_soft(driver)
    _wait_calendar_render(driver)
    _hide_chrome_chrome(driver)
    _boost_dpi(driver, scale=2.5)   # crisp text without shrinking UI

    controls = _find_month_controls(driver)
    if not controls:
        print("⚠️ Month controls not found; saving current calendar area.")
        cont = _find_calendar_container(driver)
        if cont:
            p = os.path.join(out_dir, "calendar_part01.png")
            cont.screenshot(p)
            print(f"✅ Saved: {p}")
            _reset_dpi(driver)
            return 1
        else:
            png = _fullpage_png(driver)
            p = os.path.join(out_dir, "calendar.png")
            with open(p, "wb") as f: f.write(png)
            print(f"✅ Saved: {p}")
            _reset_dpi(driver)
            return 1

    saved = 0
    for idx, (label, el, js) in enumerate(controls, 1):
        # click month button/link
        try:
            if el and el.is_displayed():
                driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
                try:
                    WebDriverWait(driver, 2).until(EC.element_to_be_clickable(el)).click()
                except Exception:
                    driver.execute_script("arguments[0].click();", el)
            elif js:
                driver.execute_script(js)
        except Exception:
            pass

        time.sleep(0.8)
        _kill_overlays_soft(driver)
        _wait_calendar_render(driver)

        # find the scrollable calendar grid
        cont = _find_calendar_container(driver)
        if not cont:
            print(f"⚠️ Calendar container not found for {label}; falling back to full-page capture.")
            png = _fullpage_png(driver)
            fp = os.path.join(out_dir, f"{idx:02d}_{label.replace(' ','_')}.png")
            with open(fp, "wb") as f: f.write(png)
            print(f"🖼️  Saved month (fullpage): {fp}")
            saved += 1
            continue

        # slice screenshots down the month
        base = f"{idx:02d}_{label.replace(' ','_').replace('/','-')}"
        parts = _element_scroll_slices(driver, cont, out_dir, base_name=base, overlap_px=100)
        print(f"🖼️  Saved {len(parts)} slices for {label}:")
        for p in parts:
            print(f"     - {p}")
        saved += len(parts)

    print(f"✅ Academic Calendar screenshots saved ({saved} files) → {out_dir}")

    
# --------------------------- MAIN ---------------------------
def main():
    # ---- Credentials in terminal ----
    username_val = input("Enter your VTOP username (e.g., Reg No): ").strip()
    password_val = getpass.getpass("Enter your VTOP password: ")

    # ---- Launch Chrome ----
    chrome_options = Options()
    chrome_options.set_capability("goog:loggingPrefs", {"performance": "ALL"})  # enable network logs

    driver = webdriver.Chrome(
        service=Service(ChromeDriverManager().install()),
        options=chrome_options
    )

    driver.get(LOGIN_URL)
    driver.maximize_window()
    wait_ready(driver, 15)

    # (Optional: extra safety – ensure Network domain is on)
    try:
        driver.execute_cdp_cmd("Network.enable", {})
    except Exception:
        pass

    # Click Student role if present
    for how, sel in [
        (By.XPATH, "//button[contains(., 'Student')]"),
        (By.XPATH, "//a[contains(., 'Student')]"),
        (By.CSS_SELECTOR, "button#student, a#student, button[data-role='student']"),
    ]:
        try:
            WebDriverWait(driver, 2).until(EC.element_to_be_clickable((how, sel))).click()
            print("ℹ️ Selected 'Student' role.")
            break
        except Exception:
            pass

    # Outer loop: allow up to 3 password attempts if credentials are wrong
    for pwd_try in range(1, MAX_PASSWORD_ATTEMPTS + 1):
        if pwd_try > 1:
            print(f"\n🔁 Password attempt {pwd_try}/{MAX_PASSWORD_ATTEMPTS}")
            password_val = getpass.getpass("Re-enter your VTOP password: ")

        # ---- Fill credentials (user will handle any captcha & press Submit) ----
        print("🔐 Filling username and password…")
        fill_credentials(driver, username_val, password_val)

        # Detect captcha type, arm submit probe, then wait until you actually click Submit
        cap_case = detect_captcha_case(driver)
        arm_submit_click_probe(driver)
        ok = wait_for_user_submit_click_then_result(
            driver,
            idle_minutes=(30 if cap_case in ("image", "text", "recaptcha") else 10),
            outcome_timeout=180
        )

        # After waiting, check outcomes
        if ok and login_success(driver):
            print("✅ Login successful!")
            save_cookies(driver, username_val)

        # --------- TIMETABLE PAGE ----------
        navigate_to_timetable(driver)
        chosen = select_semester_if_needed(driver)
        _screenshot_timetable(driver, out_png=os.path.join("data","timetable_debug.png"))
        # (A) Registered courses (DOM, upper table)
        try:
           parse_registered_courses_dom(
               driver,
               out_path=os.path.join("data", "registered_courses.json")
           )
           print("✅ Registered Courses saved to data/registered_courses.json")
        except Exception as e:
           print(f"⚠️ Registered Courses parse failed: {e}")

        # --------- ATTENDANCE (SUMMARY COUNTS) ----------
        navigate_to_attendance(driver)
        att_sem = select_attendance_semester_if_needed(driver)
        if att_sem:
            print(f"(Attendance semester selected: {att_sem})")

        att_counts_payload = scrape_attendance(driver, only_counts=True, write_json=True)
        # Optional: build a quick dict for O(1) lookups by course
        counts_by_course = {r["course_code"]: {"attended": r["attended"], "total": r["total"]}
                           for r in att_counts_payload["rows"]}
        print("📊 Attendance counts:", counts_by_course)
        # ------------------------------------------------

        # --------- ACADEMIC CALENDAR (just screenshots, high quality) ----------
        navigate_to_academic_calendar(driver)
        acad_sem = select_acad_semester_with_shortcuts(driver)
        if acad_sem: print(f"(Academic Calendar semester selected: {acad_sem})")
        acad_grp = select_acad_class_group_with_shortcuts(driver)
        if acad_grp: print(f"(Academic Calendar class group selected: {acad_grp})")

        screenshot_academic_calendar_months(
            driver,
            out_dir=os.path.join("data", "academic_calendar")
       )
       # -----------------------------------------------------------------------


        
        input("\nPress ENTER to close the browser...")
        driver.quit()
        return

        # If not ok, check specific failure reasons
        if page_says_wrong_password(driver):
            print("❌ The page indicates the password/credentials are invalid.")
            if pwd_try < MAX_PASSWORD_ATTEMPTS:
                continue
            else:
                print("❌ Maximum password attempts reached.")
                break
        elif page_says_wrong_captcha(driver):
            print("❌ CAPTCHA seems incorrect. Please try again on the page.")
            continue
        else:
            print("ℹ️ Login not confirmed yet. If you submitted, check for errors on the page (alerts).")
            if pwd_try < MAX_PASSWORD_ATTEMPTS:
                continue
            else:
                print("❌ Giving up after maximum attempts.")
                break

    input("\nPress ENTER to close the browser...")
    driver.quit()


if __name__ == "__main__":
    main()
