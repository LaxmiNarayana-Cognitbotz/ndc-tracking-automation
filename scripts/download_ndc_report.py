"""
Oracle Fusion - NDC Process Request Status Report Downloader
"""

import asyncio
import os
import sys
import traceback
from datetime import datetime
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright

# Redirect stdout and stderr to a log file
LOG_DIR = Path(__file__).parent.parent / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_DIR / "download_ndc_report.log"

class LoggerWriter:
    def __init__(self, file_path):
        self.terminal = sys.stdout
        self.file_path = file_path

    def write(self, message):
        try:
            with open(self.file_path, "a", encoding="utf-8") as f:
                f.write(message)
        except Exception:
            pass
        if self.terminal is not None:
            try:
                self.terminal.write(message)
            except UnicodeEncodeError:
                try:
                    self.terminal.write(message.encode("cp1252", errors="replace").decode("cp1252"))
                except Exception:
                    pass
            except Exception:
                pass

    def flush(self):
        if self.terminal is not None:
            try:
                self.terminal.flush()
            except Exception:
                pass

sys.stdout = LoggerWriter(LOG_FILE)
sys.stderr = LoggerWriter(LOG_FILE)

load_dotenv(verbose=True)

# Oracle credentials
ORACLE_URL = os.getenv("ORACLE_URL")
ORACLE_EMAIL = os.getenv("ORACLE_EMAIL")
ORACLE_PASSWORD = os.getenv("ORACLE_PASSWORD")

# Output directory
DOWNLOAD_DIR = Path(__file__).parent.parent / "uploads" / "NDC_Reports"

# Standalone automation profile (persists Oracle session between headless runs)
AUTOMATION_PROFILE_DIR = Path(__file__).parent.parent / "chrome_automation_profile"

# Headless mode
HEADLESS = os.getenv("HEADLESS", "true").lower()

# Business unit
BUSINESS_UNIT_SEARCH = "Adani Green"

def ts():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def report_save_stem() -> str:
    """e.g. NDC_Process_Request_Status_10_June_2026_12.11PM"""
    now = datetime.now()
    return (
        f"NDC_Process_Request_Status_{now.day}_"
        f"{now.strftime('%B')}_{now.year}_{now.strftime('%I.%M%p')}"
    )


def _cleanup_profile_locks():
    """Remove Chrome lock files on the automation profile only."""
    lock_files = ["SingletonLock", "SingletonSocket", "SingletonCookie", "lockfile"]
    if not AUTOMATION_PROFILE_DIR.exists():
        return
    for lock_name in lock_files:
        lock_path = AUTOMATION_PROFILE_DIR / lock_name
        try:
            if lock_path.exists():
                lock_path.unlink()
        except OSError:
            pass


async def save_debug_screenshot(page, label: str):
    path = DOWNLOAD_DIR / f"debug_{label}_{datetime.now().strftime('%H%M%S')}.png"
    try:
        await page.screenshot(path=str(path), full_page=True)
        print(f"[{ts()}]   Screenshot: {path}")
    except Exception as e:
        print(f"[{ts()}]   Could not save screenshot: {e}")
    return str(path)


async def check_for_sso_errors(page):
    """Check if Microsoft SSO shows a validation error, and raise an exception if found."""
    # Check standard error IDs used by Microsoft login
    for error_id in ["usernameError", "passwordError", "error"]:
        try:
            loc = page.locator(f"#{error_id}").first
            if await loc.is_visible(timeout=1000):
                err_text = await loc.inner_text()
                err_text = err_text.strip().replace("\n", " ")
                if err_text:
                    raise RuntimeError(f"Microsoft SSO Error: {err_text}")
        except RuntimeError:
            raise
        except Exception:
            pass

    # Check for general alert roles
    try:
        alert_loc = page.locator('[role="alert"]').first
        if await alert_loc.is_visible(timeout=1000):
            text = (await alert_loc.inner_text()).strip().replace("\n", " ")
            if text:
                raise RuntimeError(f"Microsoft SSO Alert: {text}")
    except RuntimeError:
        raise
    except Exception:
        pass


async def _handle_microsoft_sso(page):
    """
    Automatically fill the Microsoft SSO login form:
      1. Enter email → click Next
      2. Enter password → click Sign In
      3. Handle "Stay signed in?" prompt
      4. Wait for RSA/MFA push approval (the real user approves on their phone)
    """
    if not ORACLE_EMAIL or not ORACLE_PASSWORD:
        print(f"[{ts()}] ⚠ No credentials in .env — waiting for manual login...")
        print(f"[{ts()}]   Create a .env file with ORACLE_EMAIL and ORACLE_PASSWORD")
        return False

    print(f"[{ts()}] SSO login detected — filling credentials automatically...")
    await page.wait_for_timeout(2000)
    await check_for_sso_errors(page)

    # ── Wait for SSO page to stabilise before touching any input ─────────
    email_filled = False
    try:
        # Wait specifically for the EMAIL field to appear (not password which may be
        # hidden in the DOM on the email screen and cause false positives)
        email_loc = page.locator('input[name="loginfmt"], input[id="i0116"]')
        await email_loc.first.wait_for(state="visible", timeout=8000)
    except Exception:
        # Email field didn't appear - check if we somehow landed on the password screen
        try:
            pw_loc = page.locator('input[name="passwd"], input[id="i0118"]')
            if await pw_loc.first.is_visible(timeout=3000):
                print(f"[{ts()}]   Password screen detected directly — skipping email entry.")
                email_filled = True
        except Exception:
            pass

    # ── Step 1: Email ─────────────────────────────────────────────────────
    if not email_filled:
        email_selectors = [
            'input[name="loginfmt"]',
            'input[type="email"]',
            'input[id="i0116"]',
        ]
        for sel in email_selectors:
            try:
                email_input = page.locator(sel).first
                if await email_input.is_visible(timeout=5000):
                    for attempt in range(1, 4):
                        await email_input.click()
                        await email_input.fill("")
                        await page.wait_for_timeout(200)
                        await email_input.type(ORACLE_EMAIL.strip(), delay=50)
                        print(f"[{ts()}]   Entered email (Attempt {attempt}/3)")
                        await page.wait_for_timeout(500)

                        # Click Next
                        next_btn = page.locator('input[type="submit"], #idSIButton9').first
                        await next_btn.click()
                        print(f"[{ts()}]   Clicked Next")
                        await page.wait_for_timeout(3000)
                        
                        # Check for errors immediately after clicking Next
                        try:
                            await check_for_sso_errors(page)
                            email_filled = True
                            break  # No errors, proceed to next step
                        except RuntimeError as e:
                            if attempt == 3:
                                raise
                            print(f"[{ts()}]   {e} - Retrying...")
                    
                    if email_filled:
                        break
            except RuntimeError:
                raise
            except Exception:
                continue

    if not email_filled:
        print(f"[{ts()}]   Email field not found — page may have changed.")
        await save_debug_screenshot(page, "email_not_found")
        return False

    # ── Step 2: Password ──────────────────────────────────────────────────
    password_selectors = [
        'input[name="passwd"]',
        'input[type="password"]',
        'input[id="i0118"]',
    ]
    password_filled = False
    for sel in password_selectors:
        try:
            pw_input = page.locator(sel).first
            if await pw_input.is_visible(timeout=5000):
                for attempt in range(1, 4):
                    await pw_input.click()
                    await pw_input.fill("")
                    await page.wait_for_timeout(200)
                    await pw_input.type(ORACLE_PASSWORD.strip(), delay=50)
                    print(f"[{ts()}]   Entered password (Attempt {attempt}/3)")
                    await page.wait_for_timeout(500)

                    # Click Sign In
                    signin_btn = page.locator('input[type="submit"], #idSIButton9').first
                    await signin_btn.click()
                    print(f"[{ts()}]   Clicked Sign In")
                    await page.wait_for_timeout(3000)
                    
                    # Check for errors immediately after clicking Sign In
                    try:
                        await check_for_sso_errors(page)
                        password_filled = True
                        break  # No errors, proceed to next step
                    except RuntimeError as e:
                        if attempt == 3:
                            raise
                        print(f"[{ts()}]   {e} - Retrying...")
                
                if password_filled:
                    break
        except RuntimeError:
            raise
        except Exception:
            continue

    if not password_filled:
        print(f"[{ts()}]   Password field not found — may need different auth flow.")
        await save_debug_screenshot(page, "password_not_found")
        return False

    # ── Step 3: Choose RSA method if prompted ─────────────────────────────
    # Sometimes Microsoft asks "Verify your identity" and you have to click the RSA button
    try:
        rsa_btn = page.locator('div[role="button"], button').filter(has_text="RSAEntraMFA").first
        if await rsa_btn.is_visible(timeout=5000):
            print(f"[{ts()}]   'Verify your identity' screen detected.")
            await rsa_btn.click()
            print(f"[{ts()}]   Clicked 'Approve with RSAEntraMFA'")
            await page.wait_for_timeout(3000)
    except Exception:
        pass  # If it doesn't appear, the push might have been sent automatically

    # ── Step 4: RSA / MFA approval ────────────────────────────────────────
    print(f"[{ts()}] Waiting for RSA/MFA approval...")
    print()
    print("  +------------------------------------------------------------+")
    print("  |  RSA push notification sent to the user's phone.           |")
    print("  |  Please ask them to APPROVE it.                            |")
    print("  |  Waiting up to 5 minutes...                                |")
    print("  +------------------------------------------------------------+")
    print()

    # ── Step 5: "Stay signed in?" prompt (may appear after MFA) ───────────
    # We handle this in parallel with waiting for redirect
    return True


async def wait_for_oracle_home(page, timeout_s: int = 300):
    """Wait for Oracle Fusion home page. Handles SSO login if needed."""
    print(f"[{ts()}] Waiting for Oracle home page (up to {timeout_s}s)...")

    home_selectors = [
        'text="HR SPOC Reports"',
        'a:has-text("HR SPOC Reports")',
        '.fuse-welcome-page',
        '.springboard',
    ]

    login_handled = False
    rsa_retry_count = 0
    deadline = asyncio.get_event_loop().time() + timeout_s

    while asyncio.get_event_loop().time() < deadline:

        # ── Check if we're on a login/SSO page ────────────────────────────
        current_url = page.url.lower()
        if any(x in current_url for x in ["login.microsoftonline", "login.microsoft", "adfs", "saml"]):
            if not login_handled:
                login_handled = await _handle_microsoft_sso(page)
            # If handled, just continue letting the loop run so it can catch "Stay signed in?"

        # ── Handle RSA / SecurID Rejection ────────────────────────────────
        try:
            if "securid.com" in current_url or "adani.auth" in current_url:
                retry_btn = page.locator('button:has-text("Retry")').first
                if await retry_btn.is_visible(timeout=1000):
                    rsa_retry_count += 1
                    print(f"[{ts()}] ⚠ RSA/MFA Authentication failed or rejected (Attempt {rsa_retry_count}/3).")
                    if rsa_retry_count >= 3:
                        await save_debug_screenshot(page, "rsa_failed_3_times")
                        raise RuntimeError("RSA/MFA Authentication failed 3 times. Please ensure you are approving the prompt.")
                    
                    print(f"[{ts()}]   Clicking 'Retry' for RSA...")
                    await retry_btn.click()
                    await page.wait_for_timeout(3000)
                    print(f"[{ts()}] Waiting for RSA/MFA approval...")
                    continue
        except RuntimeError:
            raise
        except Exception:
            pass

        # ── Handle "Stay signed in?" prompt ───────────────────────────────
        try:
            stay_signed = page.locator('#idSIButton9, input[value="Yes"]').first
            if await stay_signed.is_visible(timeout=1000):
                await stay_signed.click()
                print(f"[{ts()}]   Clicked 'Stay signed in? → Yes'")
                await page.wait_for_timeout(3000)
                continue
        except Exception:
            pass

        # ── Check for Oracle home page ────────────────────────────────────
        for sel in home_selectors:
            try:
                if await page.locator(sel).first.is_visible(timeout=2000):
                    print(f"[{ts()}]   Home page ready (matched: {sel!r})")
                    return
            except Exception:
                pass
        await page.wait_for_timeout(2000)

    await save_debug_screenshot(page, "home_load_timeout")
    raise RuntimeError(
        f"Oracle home page did not load within {timeout_s}s.\nCurrent URL: {page.url}"
    )


async def safe_click(locator, description: str, timeout: int = 15_000):
    try:
        await locator.wait_for(state="visible", timeout=timeout)
        await locator.scroll_into_view_if_needed()
        await locator.click(timeout=timeout)
    except PlaywrightTimeoutError:
        raise RuntimeError(f"Could not click '{description}' within {timeout}ms")


async def wait_for_loading(frame, timeout_ms: int = 90_000):
    """Wait for Oracle ADF loading overlays to disappear (skip if none present)."""
    mask = frame.locator('div.modalMask')
    try:
        if await mask.count() == 0:
            return
        if not await mask.first.is_visible(timeout=2000):
            return
        await mask.first.wait_for(state="hidden", timeout=timeout_ms)
    except Exception as e:
        print(f"[{ts()}] Loading mask error: {traceback.format_exc()}")


async def click_with_fallback(page_or_frame, selectors: list, description: str,
                              timeout_each: int = 10_000):
    errors = []
    for sel in selectors:
        try:
            loc = page_or_frame.locator(sel).first
            await loc.wait_for(state="visible", timeout=timeout_each)
            await loc.scroll_into_view_if_needed()
            await loc.click()
            return
        except Exception as e:
            errors.append(f"    {sel!r}: {e}")
    raise RuntimeError(
        f"No working selector for '{description}'.\nTried:\n" + "\n".join(errors)
    )


def _is_report_page(url: str) -> bool:
    return "bipublisherEntry" in url or (
        "saw.dll" in url and "NDC" in url
    )


async def wait_for_report_tab(context, timeout_s: int = 90):
    """
    The NDC report opens in a NEW browser tab (not the Fusion home tab).
    Wait for that tab to appear and return it.
    """
    print(f"[{ts()}] Waiting for BI Publisher report tab...")
    deadline = asyncio.get_event_loop().time() + timeout_s
    last_print = 0

    while asyncio.get_event_loop().time() < deadline:
        for p in context.pages:
            if _is_report_page(p.url):
                await p.bring_to_front()
                try:
                    await p.wait_for_load_state("domcontentloaded", timeout=10_000)
                except Exception:
                    pass
                print(f"[{ts()}]   Report tab found: {p.url[:80]}...")
                return p

        now = asyncio.get_event_loop().time()
        if now - last_print > 10:
            last_print = now

        await asyncio.sleep(1.0)

    urls = [p.url for p in context.pages]
    raise RuntimeError(
        f"BI Publisher report tab not found within {timeout_s}s.\n"
        f"Open tabs:\n  " + "\n  ".join(urls)
    )


async def get_bipublisher_frame(report_page, timeout_s: int = 90):
    """
    Report parameters live inside the first child iframe of the main frame
    (matches Puppeteer recording: mainFrame().childFrames()[0]).
    """

    bu_input = '#xdo\\:xdo\\:_paramsP_Business_Unit_div_input'
    bu_input_loose = '[id*="Business_Unit_div_input"]'
    apply_btn = '#reportViewApply'
    deadline = asyncio.get_event_loop().time() + timeout_s

    while asyncio.get_event_loop().time() < deadline:
        # Primary: first child frame of main (from screen recording)
        children = report_page.main_frame.child_frames
        if children:
            frame = children[0]
            try:
                loc = frame.locator(bu_input)
                if await loc.count() > 0 and await loc.first.is_visible(timeout=2000):

                    return frame
            except Exception:
                pass

        # Fallback: scan every frame on the report tab
        for frame in report_page.frames:
            try:
                loc = frame.locator(bu_input_loose)
                if await loc.count() > 0 and await loc.first.is_visible(timeout=1000):

                    return frame
            except Exception:
                pass

        # Page shell may be ready before the iframe — wait for Apply as readiness signal
        try:
            await report_page.locator(apply_btn).first.wait_for(
                state="visible", timeout=1500
            )
        except Exception:
            pass

        await asyncio.sleep(1.0)

    await save_debug_screenshot(report_page, "report_frame_not_found")
    raise RuntimeError(
        f"BI Publisher parameter iframe not found within {timeout_s}s.\n"
        f"Page URL: {report_page.url}"
    )


async def wait_for_report_completed(report_page, report_frame, timeout_ms: int = 300_000):
    """Wait until the report area shows 'Report Completed'."""
    print(f"[{ts()}] Waiting for 'Report Completed' (up to {timeout_ms // 1000}s)...")
    deadline = asyncio.get_event_loop().time() + timeout_ms / 1000
    while asyncio.get_event_loop().time() < deadline:
        for frame in report_page.frames:
            try:
                loc = frame.get_by_text("Report Completed", exact=True)
                if await loc.count() > 0 and await loc.first.is_visible(timeout=500):
                    print(f"[{ts()}]   Report Completed.")
                    return True
            except Exception:
                pass
        await asyncio.sleep(2)
    print(f"[{ts()}]   'Report Completed' not seen — still waiting for download...")
    return False


def _download_extension(suggested_filename: str) -> str:
    """Use the extension Oracle sends — this report is .xls, not .xlsx."""
    ext = Path(suggested_filename).suffix.lower() if suggested_filename else ""
    if ext in (".xls", ".xlsx", ".xlsm", ".csv"):
        return ext
    return ".xls"


async def click_apply_and_wait_for_download(
    report_page, report_frame, download_dir: Path, timeout_ms: int = 300_000
):
    """
    Apply triggers an automatic Excel download — no Export button needed.
    Wait for the download event and save with a timestamped filename.
    """
    print(f"[{ts()}] Applying report & downloading...")
    try:
        async with report_page.expect_download(timeout=timeout_ms) as dl_info:
            await safe_click(report_frame.locator('#reportViewApply'), "Apply button")
            await wait_for_loading(report_frame, timeout_ms=90_000)
            await wait_for_report_completed(report_page, report_frame, timeout_ms=timeout_ms)

        download = await dl_info.value
        failure = await download.failure()
        if failure:
            raise RuntimeError(f"Browser download failed: {failure}")

        suggested = download.suggested_filename or ""
        ext = _download_extension(suggested)
        save_path = download_dir / f"{report_save_stem()}{ext}"

        await download.save_as(save_path)

        size = save_path.stat().st_size
        if size < 512:
            raise RuntimeError(
                f"Downloaded file is too small ({size} bytes) — likely incomplete."
            )

        print(f"[{ts()}] Saved: {save_path.name} ({size:,} bytes)")
        return str(save_path)

    except PlaywrightTimeoutError:
        await save_debug_screenshot(report_page, "download_timeout")
        return None


def filter_downloaded_report(file_path: Path):
    """
    If the downloaded report's Business Unit metadata contains 'All',
    filter the data rows to only keep Business Units starting with 'Adani Green'.
    """
    print(f"[{ts()}] Checking if Excel report requires post-download filtering...")
    
    # Try reading the file. Since Oracle BIP exports can be HTML-based,
    # we support both binary/XML Excel and HTML tables.
    dfs = []
    file_type = "excel"
    
    try:
        # Check if the file is HTML-based
        is_html = False
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            head = f.read(1000)
            if "<html" in head.lower() or "<table" in head.lower():
                is_html = True
                
        if is_html:
            dfs = pd.read_html(str(file_path))
            file_type = "html"
        else:
            df = pd.read_excel(file_path, header=None)
            dfs = [df]
            file_type = "excel"
            
    except Exception as e:
        print(f"[{ts()}]   Error: Could not parse Excel file {file_path.name}: {e}")
        return

    # Search for the Business Unit metadata row and table index
    bu_meta_tbl_idx = None
    bu_meta_row = None
    bu_meta_col = None
    bu_meta_val = None
    
    for idx, df in enumerate(dfs):
        for r_idx in range(min(30, len(df))):
            row_vals = [str(x).strip() for x in df.iloc[r_idx].tolist()]
            # Also check column names as fallback
            col_names = [str(c).strip() for c in df.columns]
            
            # Check row values
            for c_idx, val in enumerate(row_vals):
                if "Business Unit" in val:
                    # Check if the value is in the SAME cell (e.g. "Business Unit :All")
                    # We can assume it's in the same cell if the cell contains ":" and has value after it.
                    if ":" in val and len(val.split(":", 1)[1].strip()) > 0:
                        bu_meta_tbl_idx = idx
                        bu_meta_row = r_idx
                        bu_meta_col = c_idx
                        bu_meta_val = val.split(":", 1)[1].strip()
                        break
                    else:
                        # Find next non-empty cell in the row
                        for next_col_idx, next_val in enumerate(row_vals[c_idx+1:], start=c_idx+1):
                            if next_val and next_val != "nan":
                                bu_meta_tbl_idx = idx
                                bu_meta_row = r_idx
                                bu_meta_col = next_col_idx
                                bu_meta_val = next_val
                                break
                    if bu_meta_tbl_idx is not None:
                        break
            
            if bu_meta_tbl_idx is not None:
                break
                
            # Check column headers (sometimes parsed as headers)
            for c_idx, val in enumerate(col_names):
                if "Business Unit" in val:
                    if ":" in val and len(val.split(":", 1)[1].strip()) > 0:
                        bu_meta_tbl_idx = idx
                        bu_meta_row = -1  # indicates header
                        bu_meta_col = c_idx
                        bu_meta_val = val.split(":", 1)[1].strip()
                        break
                    else:
                        # The value might be in the first row of that column
                        if len(df) > 0:
                            bu_meta_tbl_idx = idx
                            bu_meta_row = -1  # indicates header
                            bu_meta_col = c_idx
                            bu_meta_val = str(df.iloc[0, c_idx]).strip()
                            break
            
            if bu_meta_tbl_idx is not None:
                break

    if bu_meta_tbl_idx is None:
        print(f"[{ts()}]   Could not locate 'Business Unit' metadata cell in any table.")
        return

    # Check if the value is "All" (case-insensitive)
    is_all = False
    if bu_meta_val:
        cleaned_val = bu_meta_val.lower().replace(":", "").strip()
        if cleaned_val == "all":
            is_all = True

    if not is_all:
        print(f"[{ts()}]   Business Unit is already filtered ('{bu_meta_val}'). No local filtering needed.")
        return

    # Locate the table and column headers containing the actual employee records.
    # We scan all tables for a row containing "Business Unit" and at least 4 filled columns.
    data_tbl_idx = None
    header_row_idx = None
    bu_col_idx = None
    
    for idx, df in enumerate(dfs):
        # We start from row 0 if it's a different table, or row 5 if it's the metadata table
        start_row = 5 if idx == bu_meta_tbl_idx else 0
        for r_idx in range(start_row, len(df)):
            row_vals = [str(x).strip() for x in df.iloc[r_idx].tolist()]
            if "Business Unit" in row_vals:
                non_empty = sum(1 for x in row_vals if x and x != "nan")
                if non_empty >= 4:
                    data_tbl_idx = idx
                    header_row_idx = r_idx
                    bu_col_idx = row_vals.index("Business Unit")
                    break
        if data_tbl_idx is not None:
            break

    if data_tbl_idx is None:
        print(f"[{ts()}]   Error: Could not locate table header row containing 'Business Unit' column.")
        return

    # Perform the filtering on the target table
    target_df = dfs[data_tbl_idx]
    metadata_df = target_df.iloc[:header_row_idx].copy()
    header_row = target_df.iloc[header_row_idx].copy()
    data_df = target_df.iloc[header_row_idx + 1:].copy()

    original_row_count = len(data_df)
    
    def should_keep(row):
        val = str(row.iloc[bu_col_idx]).strip()
        if not val or val == "nan":
            return True
        return val.lower().startswith("adani green")

    filtered_data_df = data_df[data_df.apply(should_keep, axis=1)]
    filtered_row_count = len(filtered_data_df)
    removed_count = original_row_count - filtered_row_count

    # Update metadata Business Unit cell value to reflect the filter
    new_meta_val = ":[Adani Green > Renewable > Hydro O&M,Adani Green > Renewable > Hydro Projects,Adani Green > Renewable > Solar O&M,Adani Green > Renewable > Solar Projects,Adani Green > Renewable > Wind O&M,Adani Green > Renewable > Wind Projects] (Filtered)"
    
    meta_df = dfs[bu_meta_tbl_idx]
    if bu_meta_row == -1:
        # It was in headers, update column name
        new_cols = list(meta_df.columns)
        new_cols[bu_meta_col] = "Business Unit " + new_meta_val
        meta_df.columns = new_cols
    else:
        meta_df.iloc[bu_meta_row, bu_meta_col] = new_meta_val

    # Reconstruct and save the Excel sheet
    try:
        # If there's only 1 table, combine and save
        if len(dfs) == 1:
            final_df = pd.concat([metadata_df, pd.DataFrame([header_row]), filtered_data_df], ignore_index=True)
            if file_path.suffix.lower() == ".xls":
                final_df.to_excel(file_path, header=False, index=False)
            else:
                final_df.to_excel(file_path, header=False, index=False, engine='openpyxl')
        else:
            # If multiple tables, we update target_df inside dfs
            dfs[data_tbl_idx] = pd.concat([metadata_df, pd.DataFrame([header_row]), filtered_data_df], ignore_index=True)
            # Write all tables to Excel sequentially in a single sheet
            with pd.ExcelWriter(file_path, engine='openpyxl') as writer:
                start_row = 0
                for df_to_write in dfs:
                    df_to_write.to_excel(writer, sheet_name="Sheet1", startrow=start_row, index=False, header=False)
                    start_row += len(df_to_write) + 1  # 1 row space
                    
        print(f"[{ts()}] SUCCESS: Excel filter applied! Kept {filtered_row_count}/{original_row_count} rows starting with 'Adani Green'.")
        
    except Exception as e:
        print(f"[{ts()}]   Error saving filtered Excel file: {e}")


# ─── MAIN ────────────────────────────────────────────────────────────────────

async def download_ndc_report():
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    print(f"[{ts()}] Starting NDC report download ({'headless' if HEADLESS == 'true' else 'visible'})")

    # Clean up old files locally before downloading the new one
    try:
        for f in DOWNLOAD_DIR.iterdir():
            if f.is_file():
                try:
                    f.unlink()
                    print(f"[{ts()}] [CLEANUP] Deleted old local file: {f.name}")
                except Exception as e:
                    print(f"[{ts()}] [CLEANUP] Warning: could not delete {f.name}: {e}")
    except Exception as e:
        print(f"[{ts()}] [CLEANUP] Local cleanup error: {e}")


    _cleanup_profile_locks()

    try:
        async with async_playwright() as p:
            print(f"[{ts()}] Launching browser...")
            context = await p.chromium.launch_persistent_context(
                user_data_dir=str(AUTOMATION_PROFILE_DIR),
                channel="chrome",
                headless=(HEADLESS == "true"),
                args=[
                    "--no-first-run",
                    "--no-default-browser-check",
                    "--disable-blink-features=AutomationControlled",
                    "--disable-session-crashed-bubble",
                    "--hide-crash-restore-bubble",
                    "--window-size=1517,900",
                ]
            )


            page = context.pages[0] if context.pages else await context.new_page()

            print(f"[{ts()}] Navigating to Oracle Fusion...")
            await page.goto(ORACLE_URL, wait_until="domcontentloaded", timeout=90_000)
            await page.wait_for_timeout(3000)

            await wait_for_oracle_home(page, timeout_s=120)
            await page.wait_for_timeout(2000)

            print(f"[{ts()}] Opening NDC Process Request Status report...")
            await click_with_fallback(page, [
                'a:has-text("HR SPOC Reports")',
                'text="HR SPOC Reports"',
                '#c_64db9d54bc7c456d958c987c0cc83fcf',
                'xpath=//a[normalize-space()="HR SPOC Reports"]',
                'xpath=//*[contains(text(),"HR SPOC Reports")]',
            ], "HR SPOC Reports")
            await page.wait_for_timeout(3000)

            ndc_selectors = [
                'a:has-text("NDC Process Request Status")',
                'text="NDC Process Request Status"',
                '#c_fc8798b561de4e0bb05f5b982200a547_0',
                'text="NDC Process Request"',
                'xpath=//*[contains(text(),"NDC Process Request")]',
            ]
            try:
                async with context.expect_page(timeout=60_000) as new_page_info:
                    await click_with_fallback(page, ndc_selectors, "NDC Process Request Status")
                report_page = await new_page_info.value
            except PlaywrightTimeoutError:
                report_page = await wait_for_report_tab(context, timeout_s=30)

            await report_page.bring_to_front()
            try:
                await report_page.wait_for_load_state("networkidle", timeout=30_000)
            except PlaywrightTimeoutError:
                await report_page.wait_for_load_state("domcontentloaded", timeout=15_000)
            await report_page.wait_for_timeout(3000)

            report_frame = await get_bipublisher_frame(report_page, timeout_s=90)
            await wait_for_loading(report_frame, timeout_ms=30_000)
            print(f"[{ts()}] Report parameters loaded")

            from_date_selectors = [
                '#_paramsP_FROMDATE',
                'xpath=//*[@id="_paramsP_FROMDATE"]',
                'input[id="_paramsP_FROMDATE"]',
            ]
            from_date_filled = False
            for sel in from_date_selectors:
                try:
                    loc = report_frame.locator(sel).first
                    if await loc.is_visible(timeout=5000):
                        await loc.click(click_count=3)
                        await report_page.wait_for_timeout(500)
                        await loc.fill("02-09-2026")
                        await report_page.wait_for_timeout(500)
                        await loc.click()
                        await report_page.wait_for_timeout(1000)
                        from_date_filled = True
                        break
                except Exception as e:
                    print(f"[{ts()}] From Date selector failed: {traceback.format_exc()}")
                    continue
            if from_date_filled:
                print(f"[{ts()}] From Date set: 02-09-2026")
            else:
                await save_debug_screenshot(report_page, "from_date_not_found")
                print(f"[{ts()}] WARNING: Could not fill From Date field!")
            await report_page.wait_for_timeout(2000)

            print(f"[{ts()}] Selecting Business Unit: {BUSINESS_UNIT_SEARCH}...")
            await safe_click(
                report_frame.locator('#xdo\\:xdo\\:_paramsP_Business_Unit_div_input'),
                "Business Unit field",
            )
            await safe_click(
                report_frame.locator(
                    '#xdo\\:xdo\\:_paramsP_Business_Unit_div_b span.dialogFloatLeft'
                ),
                "Business Unit search icon",
            )
            await report_page.wait_for_timeout(2000)

            dialog_input = report_frame.locator(
                '#xdo\\:xdo\\:_paramsP_Business_Unit_div_input_searchDialog_input'
            )
            await dialog_input.wait_for(state="visible", timeout=15_000)
            await dialog_input.fill(BUSINESS_UNIT_SEARCH)

            await safe_click(
                report_frame.locator(
                    '#xdo\\:xdo\\:_paramsP_Business_Unit_div_input_searchDialog_button'
                ),
                "Search button",
            )
            await report_page.wait_for_timeout(5000)

            await safe_click(
                report_frame.locator(
                    '#xdo\\:xdo\\:_paramsP_Business_Unit_div_input_searchDialog_moveAllImg'
                ),
                "Move All button",
            )
            await report_page.wait_for_timeout(2000)

            ok_selectors = [
                'xpath=//*[@id="xdo:xdo:_paramsP_Business_Unit_div_input_searchDialog_dialogTable"]/tbody/tr[3]/td[2]/table/tbody/tr/td[2]/button[1]',
                'button.button_mo',
            ]
            await click_with_fallback(report_frame, ok_selectors, "OK button")
            await report_page.wait_for_timeout(2000)

            result = await click_apply_and_wait_for_download(
                report_page, report_frame, DOWNLOAD_DIR, timeout_ms=300_000
            )

            if result:
                filter_downloaded_report(Path(result))
                print(f"\nSUCCESS! Report saved to:\n   {result}")
            else:
                print(f"\nDownload did not complete in time. Check screenshot in {DOWNLOAD_DIR}")

            # Explicitly close context to release any file locks
            await context.close()

    except Exception as e:
        print(f"[{ts()}] FATAL: {e}\n{traceback.format_exc()}")


if __name__ == "__main__":
    asyncio.run(download_ndc_report())
