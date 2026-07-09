"""
OpenText Content Server - F&F (Full & Final) Exit Document Downloader
"""

import asyncio
import os
import re
import sys
import traceback
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from dotenv import load_dotenv
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

# ── Logging Setup ────────────────────────────────────────────────────────────
LOG_DIR = Path(__file__).parent.parent / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_DIR / "download_ff_report.log"


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

# ── Configuration ────────────────────────────────────────────────────────────

# SSO credentials (same as NDC script)
ORACLE_EMAIL = os.getenv("ORACLE_EMAIL")
ORACLE_PASSWORD = os.getenv("ORACLE_PASSWORD")

# Content Server URL
CONTENT_SERVER_URL = os.getenv("CONTENT_SERVER_URL")

# Output directory for downloaded F&F documents
DOWNLOAD_DIR = Path(__file__).parent.parent / "uploads" / "FF_Reports"

# Standalone automation profile (persists browser session between runs)
AUTOMATION_PROFILE_DIR = Path(__file__).parent.parent / "chrome_automation_profile"

# Headless mode
HEADLESS = os.getenv("HEADLESS", "true").lower()

# Default timeout for individual element interactions (ms)
ELEMENT_TIMEOUT = 15_000

# Page load / navigation timeout (ms)
NAV_TIMEOUT = 60_000

# Maximum number of concurrent browser tabs for parallel downloads
MAX_CONCURRENT_TABS = 5

# How long to wait for the search results page to fully render (ms)
SEARCH_RESULTS_WAIT = 10_000


def ts():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


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


async def save_debug_screenshot(page, label: str) -> str:
    """Save a full-page screenshot for debugging."""
    path = DOWNLOAD_DIR / f"debug_{label}_{datetime.now().strftime('%H%M%S')}.png"
    try:
        await page.screenshot(path=str(path), full_page=True)
        print(f"[{ts()}]   Screenshot: {path}")
    except Exception as e:
        print(f"[{ts()}]   Could not save screenshot: {e}")
    return str(path)


# ── SSO Handling (ported from download_ndc_report.py) ────────────────────────

async def check_for_sso_errors(page):
    """Check if Microsoft SSO shows a validation error, and raise an exception if found."""
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
      1. Enter email → click Next (3 retries)
      2. Enter password → click Sign In (3 retries)
      3. Handle RSA/MFA method selection
      4. Wait for RSA/MFA push approval
    """
    if not ORACLE_EMAIL or not ORACLE_PASSWORD:
        print(f"[{ts()}] ⚠ No credentials in .env — waiting for manual login...")
        print(f"[{ts()}]   Create a .env file with ORACLE_EMAIL and ORACLE_PASSWORD")
        return False

    print(f"[{ts()}] SSO login detected — filling credentials automatically...")
    await page.wait_for_timeout(2000)
    await check_for_sso_errors(page)

    # ── Step 1: Email ─────────────────────────────────────────────────────
    email_selectors = [
        'input[name="loginfmt"]',
        'input[type="email"]',
        'input[id="i0116"]',
    ]
    email_filled = False
    for sel in email_selectors:
        try:
            email_input = page.locator(sel).first
            if await email_input.is_visible(timeout=5000):
                for attempt in range(1, 4):
                    await email_input.fill("")
                    await page.wait_for_timeout(200)
                    await email_input.fill(ORACLE_EMAIL)
                    print(f"[{ts()}]   Entered email: {ORACLE_EMAIL} (Attempt {attempt}/3)")
                    await page.wait_for_timeout(500)

                    next_btn = page.locator('input[type="submit"], #idSIButton9').first
                    await next_btn.click()
                    print(f"[{ts()}]   Clicked Next")
                    await page.wait_for_timeout(3000)

                    try:
                        await check_for_sso_errors(page)
                        email_filled = True
                        break
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
                    await pw_input.fill("")
                    await page.wait_for_timeout(200)
                    await pw_input.fill(ORACLE_PASSWORD)
                    print(f"[{ts()}]   Entered password (Attempt {attempt}/3)")
                    await page.wait_for_timeout(500)

                    signin_btn = page.locator('input[type="submit"], #idSIButton9').first
                    await signin_btn.click()
                    print(f"[{ts()}]   Clicked Sign In")
                    await page.wait_for_timeout(3000)

                    try:
                        await check_for_sso_errors(page)
                        password_filled = True
                        break
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
    try:
        rsa_btn = page.locator('div[role="button"], button').filter(has_text="RSAEntraMFA").first
        if await rsa_btn.is_visible(timeout=5000):
            print(f"[{ts()}]   'Verify your identity' screen detected.")
            await rsa_btn.click()
            print(f"[{ts()}]   Clicked 'Approve with RSAEntraMFA'")
            await page.wait_for_timeout(3000)
    except Exception:
        pass

    # ── Step 4: RSA / MFA approval ────────────────────────────────────────
    print(f"[{ts()}] Waiting for RSA/MFA approval...")
    print()
    print("  +------------------------------------------------------------+")
    print("  |  RSA push notification sent to the user's phone.           |")
    print("  |  Please ask them to APPROVE it.                            |")
    print("  |  Waiting up to 5 minutes...                                |")
    print("  +------------------------------------------------------------+")
    print()

    return True


# ── Helpers ──────────────────────────────────────────────────────────────────

async def safe_click(locator, description: str, timeout: int = ELEMENT_TIMEOUT):
    """Wait for an element to be visible, scroll into view, then click it."""
    try:
        await locator.wait_for(state="visible", timeout=timeout)
        await locator.scroll_into_view_if_needed()
        await locator.click(timeout=timeout)
    except PlaywrightTimeoutError:
        raise RuntimeError(f"Could not click '{description}' within {timeout}ms")


async def click_with_fallback(
    page_or_frame, selectors: list, description: str, timeout_each: int = 10_000
):
    """Try multiple selectors until one clicks successfully."""
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


async def wait_for_content_server_home(page, timeout_s: int = 300):
    """
    Wait for Content Server home page. Handles Microsoft SSO login if redirected,
    RSA/MFA retry (up to 3 attempts), and 'Stay signed in?' prompt.
    """
    print(f"[{ts()}] Waiting for Content Server home page (up to {timeout_s}s)...")
    home_selectors = [
        "div.csui-search a",
        'a[title="Search in Content Server"]',
        "div.csui-home-item",
        ".csui-nodestable",
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

        # ── Handle RSA / SecurID Rejection ────────────────────────────────
        try:
            if "securid.com" in current_url or "adani.auth" in current_url:
                retry_btn = page.locator('button:has-text("Retry")').first
                if await retry_btn.is_visible(timeout=1000):
                    rsa_retry_count += 1
                    print(f"[{ts()}] ⚠ RSA/MFA Authentication failed or rejected (Attempt {rsa_retry_count}/3).")
                    if rsa_retry_count >= 3:
                        await save_debug_screenshot(page, "rsa_failed_3_times")
                        raise RuntimeError("RSA/MFA Authentication failed 3 times.")

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

        # ── Check for Content Server home page ────────────────────────────
        for sel in home_selectors:
            try:
                if await page.locator(sel).first.is_visible(timeout=2000):
                    print(f"[{ts()}] Content Server home page ready")
                    return
            except Exception:
                pass
        await page.wait_for_timeout(2000)

    await save_debug_screenshot(page, "home_load_timeout")
    raise RuntimeError(
        f"Content Server home page did not load within {timeout_s}s.\n"
        f"Current URL: {page.url}"
    )


async def search_employee(page, employee_number: str):
    """
    Perform a search in Content Server for the given employee number.
    Flow: Click search icon → type in search box → click search button.
    """
    print(f"[{ts()}] Searching for employee: {employee_number}")

    # Step 1: Click the Search icon in the navbar
    await click_with_fallback(
        page,
        [
            'a[title="Search in Content Server"]',
            "div.csui-search a",
            'xpath=/html/body/div[2]/nav/div/div[2]/div[2]/div/div[2]/a',
        ],
        "Search icon",
        timeout_each=ELEMENT_TIMEOUT,
    )
    await page.wait_for_timeout(1500)

    # Step 2: Click the search input to focus it
    search_input_selectors = [
        'input[placeholder="Enter your search term"]',
        "body > div:nth-of-type(2) input",
        'xpath=/html/body/div[2]/nav/div/div[2]/div[2]/div/div[1]/div/div/input',
    ]
    search_input = None
    for sel in search_input_selectors:
        try:
            loc = page.locator(sel).first
            if await loc.is_visible(timeout=5000):
                search_input = loc
                break
        except Exception:
            continue

    if not search_input:
        raise RuntimeError("Could not find the search input field")

    await search_input.click()
    await page.wait_for_timeout(500)

    # Step 3: Clear any existing text and type the employee number
    await search_input.fill("")
    await page.wait_for_timeout(300)
    await search_input.fill(employee_number)
    await page.wait_for_timeout(500)

    # Step 4: Click the Search submit button
    await click_with_fallback(
        page,
        [
            'a[title="Search in Content Server"]',
            "div.csui-search a",
            'xpath=/html/body/div[2]/nav/div/div[2]/div[2]/div/div[2]/a',
        ],
        "Search submit button",
        timeout_each=ELEMENT_TIMEOUT,
    )
    await page.wait_for_timeout(3000)

    # Step 5: Wait for search results to appear
    try:
        await page.locator("div.binf-list-group").first.wait_for(
            state="visible", timeout=SEARCH_RESULTS_WAIT
        )
    except PlaywrightTimeoutError:
        await save_debug_screenshot(page, f"no_results_{employee_number}")
        raise RuntimeError(
            f"Search results did not appear for employee {employee_number}"
        )


async def click_employee_master_folder(page, employee_number: str):
    """
    Finds and clicks the employee's master folder in the search results.
    The folder's name should match the employee number exactly.
    """
    # Define selectors for the search result title links
    selectors = [
        ".csui-search-item a",
        "div.csui-search-item a",
        "div.binf-list-group a",
        "#results a",
    ]

    for sel in selectors:
        try:
            locators = page.locator(sel)
            count = await locators.count()
            for i in range(count):
                loc = locators.nth(i)
                text = await loc.inner_text()
                if text:
                    cleaned_text = text.strip()
                    # Check for exact match of the employee number
                    if cleaned_text == employee_number:
                        await loc.scroll_into_view_if_needed()
                        await loc.click()
                        await page.wait_for_timeout(3000)
                        return
        except Exception as e:
            pass

    # Fallback: if we couldn't find an exact text match, let's try searching specifically for a folder
    # matching the employee number, or fall back to the first search result.
    print(f"[{ts()}] ⚠ Exact match not found. Trying fallback locator for text={employee_number}...")
    try:
        # Playwright exact text matching link
        loc = page.get_by_role("link", name=employee_number, exact=True).first
        if await loc.is_visible(timeout=5000):
            await loc.click()
            await page.wait_for_timeout(3000)
            return
    except Exception:
        pass

    # If all else fails, click the first search result (as in original script)
    print(f"[{ts()}] ⚠ Could not find exact master folder match. Falling back to first search result.")
    await click_with_fallback(
        page,
        [
            "div.binf-list-group > div:nth-of-type(1) div.csui-search-item a",
            'xpath=//*[@id="results"]/div[1]/div[1]/div/div/div[3]/div[1]/div[1]/div/div/a',
        ],
        "First search result (fallback)",
        timeout_each=ELEMENT_TIMEOUT,
    )
    await page.wait_for_timeout(3000)


async def click_adani_exit_documents(page):
    """
    Click the 'Adani Exit Documents' folder link inside the employee folder.
    This navigates into the subfolder containing the F&F documents.
    """
    # Wait for the table to render first
    try:
        await page.locator("td.csui-table-cell-name").first.wait_for(
            state="visible", timeout=ELEMENT_TIMEOUT
        )
    except PlaywrightTimeoutError:
        await save_debug_screenshot(page, "no_folder_table")
        raise RuntimeError("Employee folder table did not load")

    await page.wait_for_timeout(1500)

    # Click the "Adani Exit Documents" link
    await click_with_fallback(
        page,
        [
            'text="Adani Exit Documents"',
            'a:has-text("Adani Exit Documents")',
            "tr.csui-state-hover > td.csui-table-cell-name a > span",
            'xpath=//span[contains(text(),"Adani Exit Documents")]',
            'xpath=//a[contains(.,"Adani Exit Documents")]',
        ],
        "Adani Exit Documents folder",
        timeout_each=ELEMENT_TIMEOUT,
    )
    await page.wait_for_timeout(3000)


async def get_document_count(page) -> int:
    """Count the number of document rows in the current folder table."""
    try:
        # Wait for the table to be visible
        await page.locator("tbody tr").first.wait_for(
            state="visible", timeout=ELEMENT_TIMEOUT
        )
        await page.wait_for_timeout(1000)

        # Count rows that have a name cell (actual document rows)
        rows = page.locator("tbody tr td.csui-table-cell-name")
        count = await rows.count()
        print(f"[{ts()}] Found {count} document(s)")
        return count
    except PlaywrightTimeoutError:
        print(f"[{ts()}]   No document rows found in the folder")
        return 0


# ── F&F Document Name Filter ─────────────────────────────────────────────────
# Strong regex to match ONLY F&F (Full & Final) sheet documents.
# Matches:  "F AND F", "F&F", "FNF", "F_AND_F", "FULL AND FINAL", "FULL_AND_FINAL"
# Case-insensitive. Rejects experience letters, relieving letters, resignation
# letters, NDC forms, undertakings, and any other non-F&F documents.
FF_DOCUMENT_PATTERN = re.compile(
    r"(?i)"
    r"("
    r"F\s*AND\s*F"
    r"|F\s*&\s*F"
    r"|F[_\-\s]*N[_\-\s]*F"
    r"|F_AND_F"
    r"|FULL\s*AND\s*FINAL"
    r"|FULL[_\-]AND[_\-]FINAL"
    r")"
)


def _is_ff_document(name: str) -> bool:
    """Return True only if the document name matches the F&F pattern."""
    return bool(FF_DOCUMENT_PATTERN.search(name))


async def download_document_at_row(
    page, row_index: int, total_rows: int, download_dir: Path
) -> Optional[str]:
    """
    Download a single document from the folder table by its 1-based row index.
    Flow: hover row → check if folder → check F&F name → click "More actions" → click "Download".

    Only downloads documents whose name matches the F&F regex pattern.
    The row_index is 1-based matching the <tr> nth-of-type in the DOM.
    """
    row_selector = f"tbody tr:nth-of-type({row_index})"
    try:
        row = page.locator(row_selector).first
        await row.wait_for(state="visible", timeout=ELEMENT_TIMEOUT)

        # Get the row name for logging
        row_name = f"Row {row_index}"
        name_cell = row.locator("td.csui-table-cell-name a, td.csui-table-cell-name").first
        if await name_cell.count() > 0:
            row_name = (await name_cell.inner_text()).strip()

        # Check if the row represents a folder instead of a file
        is_folder = False
        # Get the second column (icon column)
        icon_cell = row.locator("td:nth-of-type(2), td.csui-table-cell-icon, td.csui-table-cell-type, td:nth-child(2)").first
        if await icon_cell.count() > 0:
            html = await icon_cell.inner_html()
            html_lower = html.lower()
            if "folder" in html_lower or "csui-icon-node-folder" in html_lower:
                is_folder = True

        if is_folder:
            print(f"[{ts()}] Skipping folder: '{row_name}' (row {row_index}/{total_rows})")
            return None

        # ── F&F Filter: Only download F&F sheet documents ─────────────────
        if not _is_ff_document(row_name):
            print(f"[{ts()}] Skipping non-F&F document: '{row_name}' (row {row_index}/{total_rows})")
            return None

        print(f"[{ts()}] Downloading F&F document: '{row_name}' (row {row_index}/{total_rows})...")
        await row.hover()
        await page.wait_for_timeout(1000)
    except Exception as e:
        print(f"[{ts()}]   Could not analyze or hover row {row_index}: {e}")
        return None

    # Click "More actions" (the ... dropdown menu) for this row
    more_actions_selectors = [
        f"tbody tr:nth-of-type({row_index}) li.binf-dropdown > a",
        f'xpath=//tbody/tr[{row_index}]//li[contains(@class,"binf-dropdown")]/a',
        f'xpath=//tbody/tr[{row_index}]//a[@title="More actions"]',
    ]

    try:
        await click_with_fallback(
            page,
            more_actions_selectors,
            f"More actions (row {row_index})",
            timeout_each=8000,
        )
    except RuntimeError:
        # Fallback: try the generic "More actions" locator after hovering
        try:
            more_btn = page.locator('a[title="More actions"]').first
            if await more_btn.is_visible(timeout=5000):
                await more_btn.click()
                print(f"[{ts()}]   Clicked 'More actions' via fallback")
        except Exception as e2:
            print(f"[{ts()}]   Could not click 'More actions' for row {row_index}: {e2}")
            await save_debug_screenshot(page, f"more_actions_fail_row{row_index}")
            return None

    await page.wait_for_timeout(1500)

    # ── Click "Download" from the dropdown menu ──────────────────────────
    # The dropdown items can be hidden until the menu is fully expanded.
    # Strategy: try visible click first, then force-click on hidden elements.
    try:
        async with page.expect_download(timeout=60_000) as dl_info:
            download_clicked = False

            # Attempt 1: Try standard visible selectors
            download_selectors = [
                'a[title="Download"]',
                'ul.binf-dropdown-menu a[title="Download"]',
                'li.binf-dropdown ul.binf-dropdown-menu a[title="Download"]',
                f"li.binf-dropdown li:nth-of-type(1) > a",
                'xpath=//ul[contains(@class,"binf-dropdown-menu")]//a[@title="Download"]',
            ]
            for sel in download_selectors:
                try:
                    loc = page.locator(sel).first
                    if await loc.is_visible(timeout=3000):
                        await loc.scroll_into_view_if_needed()
                        await loc.click()
                        download_clicked = True
                        break
                except Exception:
                    continue

            # Attempt 2: Force-click on hidden Download link (common in this UI)
            if not download_clicked:
                force_selectors = [
                    'a[title="Download"]',
                    'xpath=//a[@title="Download"]',
                    'xpath=//a[contains(.,"Download")]',
                ]
                for sel in force_selectors:
                    try:
                        loc = page.locator(sel).first
                        # Check element exists in DOM even if hidden
                        if await loc.count() > 0:
                            await loc.click(force=True, timeout=8000)
                            download_clicked = True
                            print(f"[{ts()}]   Used force-click on Download button")
                            break
                    except Exception:
                        continue

            if not download_clicked:
                # Attempt 3: Use keyboard shortcut or dispatch click event via JS
                try:
                    await page.evaluate("""
                        () => {
                            const link = document.querySelector('a[title="Download"]');
                            if (link) { link.click(); return true; }
                            return false;
                        }
                    """)
                    download_clicked = True
                    print(f"[{ts()}]   Used JavaScript click on Download button")
                except Exception:
                    pass

            if not download_clicked:
                raise RuntimeError(
                    f"Could not click Download for row {row_index} after all attempts"
                )

        download = await dl_info.value
        failure = await download.failure()
        if failure:
            print(f"[{ts()}]   Download failed for row {row_index}: {failure}")
            return None

        suggested = download.suggested_filename or f"ff_document_{row_index}"
        
        # ── Sanitize filename and correct/force extensions ───────────────────
        suggested_clean = suggested.strip()
        # Pattern matching: ". <digits>" suffix (e.g. ". 2")
        match = re.search(r"\.\s*(\d+)$", suggested_clean)
        if match:
            suffix_num = match.group(1)
            base = suggested_clean[:match.start()].strip().rstrip(".")
            suggested_clean = f"{base}_{suffix_num}.pdf"
        else:
            ext = Path(suggested_clean).suffix.lower()
            if ext not in [".pdf", ".xlsx", ".xls", ".doc", ".docx", ".png", ".jpg"]:
                suggested_clean = suggested_clean.rstrip(".")
                suggested_clean = f"{suggested_clean}.pdf"

        save_path = download_dir / suggested_clean

        # Handle duplicate filenames by appending a counter
        if save_path.exists():
            stem = save_path.stem
            suffix = save_path.suffix
            counter = 1
            while save_path.exists():
                save_path = download_dir / f"{stem}_{counter}{suffix}"
                counter += 1

        await download.save_as(save_path)
        size = save_path.stat().st_size
        print(f"[{ts()}] Saved: {save_path.name} ({size:,} bytes)")
        return str(save_path)

    except PlaywrightTimeoutError:
        print(f"[{ts()}]   Download timed out for row {row_index}")
        await save_debug_screenshot(page, f"download_timeout_row{row_index}")
        return None
    except Exception as e:
        print(f"[{ts()}]   Error downloading row {row_index}: {e}")
        return None


async def navigate_home(page):
    """Click the Home icon to return to the Content Server home page."""
    await click_with_fallback(
        page,
        [
            "div.csui-home-item div",
            'a[title="Home page"]',
            "div.csui-home-item a",
            'xpath=/html/body/div[2]/nav/div/div[1]/div[2]/a/div',
        ],
        "Home page icon",
        timeout_each=ELEMENT_TIMEOUT,
    )
    await page.wait_for_timeout(3000)


async def download_ff_for_employee(
    page, employee_number: str, download_dir: Path
) -> List[str]:
    """
    Full flow to download all F&F documents for one employee:
      1. Search by employee number
      2. Click first search result
      3. Click "Adani Exit Documents" folder
      4. Download each document
      5. Navigate back home
    Returns a list of downloaded file paths.
    """
    # Create a subfolder for this employee
    emp_dir = download_dir / employee_number
    emp_dir.mkdir(parents=True, exist_ok=True)

    downloaded_files = []

    try:
        # Step 1: Search
        await search_employee(page, employee_number)

        # Step 2: Click the exact employee folder matching the ID
        await click_employee_master_folder(page, employee_number)

        # Step 3: Navigate into "Adani Exit Documents"
        await click_adani_exit_documents(page)

        # Step 4: Get document count and download each
        doc_count = await get_document_count(page)

        if doc_count == 0:
            print(f"[{ts()}]   No documents found for employee {employee_number}")
        else:
            for row_idx in range(1, doc_count + 1):
                result = await download_document_at_row(
                    page, row_idx, doc_count, emp_dir
                )
                if result:
                    downloaded_files.append(result)
                # Small delay between downloads
                await page.wait_for_timeout(2000)

        pass

    except Exception as e:
        print(f"[{ts()}] Error processing employee {employee_number}: {e}")
        await save_debug_screenshot(page, f"error_{employee_number}")

    return downloaded_files


# ─── MAIN ────────────────────────────────────────────────────────────────────

def prompt_employee_numbers() -> List[str]:
    """
    Interactive terminal prompt — collects employee ID(s) from the user on a single line.
    Strips any unwanted leading and trailing whitespaces.
    """
    # Use the raw terminal stream so the prompt is visible even with LoggerWriter
    terminal = sys.__stdout__

    terminal.write("\n")
    terminal.write("  ╔═══════════════════════════════════════════════════════╗\n")
    terminal.write("  ║        F&F Exit Document Downloader                  ║\n")
    terminal.write("  ╚═══════════════════════════════════════════════════════╝\n")
    terminal.write("\n")
    terminal.flush()

    try:
        terminal.write("  Enter Employee ID (or multiple comma-separated): ")
        terminal.flush()
        raw = sys.__stdin__.readline()
    except (EOFError, KeyboardInterrupt):
        terminal.write("\n")
        return []

    # Strip leading/trailing whitespaces from the whole input line
    raw = raw.strip()
    if not raw:
        terminal.write("\n  → No employee numbers entered.\n\n")
        terminal.flush()
        return []

    # Split by comma or whitespace, and strip each individual token
    collected = []
    for token in raw.replace(",", " ").split():
        token = token.strip()
        if token:
            collected.append(token)

    # De-duplicate while preserving order
    seen = set()
    unique = []
    for num in collected:
        if num not in seen:
            seen.add(num)
            unique.append(num)

    if unique:
        terminal.write(f"\n  → {len(unique)} employee(s) queued: {', '.join(unique)}\n\n")
        terminal.flush()
    else:
        terminal.write("\n  → No employee numbers entered.\n\n")
        terminal.flush()

    return unique


async def download_ff_reports(employee_numbers: List[str]):
    """
    Main entry point. Downloads F&F exit documents for one or more employees.
    Uses concurrent browser tabs (up to MAX_CONCURRENT_TABS) for parallel processing.

    Args:
        employee_numbers: List of employee numbers to process.
    """
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

    if not employee_numbers:
        print(f"[{ts()}] No employee numbers provided. Exiting.")
        return
        
    if not CONTENT_SERVER_URL:
        print(f"[{ts()}] [FATAL ERROR] CONTENT_SERVER_URL is completely blank in your .env file!")
        print(f"[{ts()}] Please open the .env file and add the URL like: CONTENT_SERVER_URL=https://...")
        return

    concurrency = min(MAX_CONCURRENT_TABS, len(employee_numbers))
    print(f"[{ts()}] Starting F&F report download ({'headless' if HEADLESS == 'true' else 'visible'})")
    print(f"[{ts()}] Concurrency: {concurrency} tab(s) for {len(employee_numbers)} employee(s)")

    _cleanup_profile_locks()

    all_results: dict[str, List[str]] = {}

    try:
        async with async_playwright() as p:
            print(f"[{ts()}] Launching browser...")
            context = await p.chromium.launch_persistent_context(
                user_data_dir=str(AUTOMATION_PROFILE_DIR),
                channel="chrome",
                headless=(HEADLESS == "true"),
                accept_downloads=True,
                args=[
                    "--no-first-run",
                    "--no-default-browser-check",
                    "--disable-blink-features=AutomationControlled",
                    "--disable-session-crashed-bubble",
                    "--hide-crash-restore-bubble",
                    "--window-size=1517,900",
                ],
            )

            # ── Step 1: Use the first tab to establish login / SSO ────────
            first_page = context.pages[0] if context.pages else await context.new_page()

            print(f"[{ts()}] Navigating to Content Server...")
            await first_page.goto(
                CONTENT_SERVER_URL, wait_until="domcontentloaded", timeout=NAV_TIMEOUT
            )
            await first_page.wait_for_timeout(3000)

            # Wait for the page to load (handles SSO login if needed)
            await wait_for_content_server_home(first_page, timeout_s=120)
            await first_page.wait_for_timeout(2000)
            print(f"[{ts()}] Login confirmed. Preparing {concurrency} tab(s)...\n")

            # ── Step 2: Pre-create all tabs sequentially ──────────────────
            # Chrome cannot handle multiple simultaneous Target.createTarget
            # calls, so we open tabs one-at-a-time before running work.
            worker_pages = [first_page]  # Reuse first page for worker 1
            for i in range(1, concurrency):
                try:
                    new_tab = await context.new_page()
                    worker_pages.append(new_tab)
                    print(f"[{ts()}]   Opened tab {i + 1}/{concurrency}")
                    await asyncio.sleep(0.5)  # Small delay between tab creation
                except Exception as e:
                    print(f"[{ts()}]   Could not open tab {i + 1}: {e}")
                    break

            actual_concurrency = len(worker_pages)
            print(f"[{ts()}] {actual_concurrency} tab(s) ready. Starting downloads...\n")

            # ── Step 3: Worker coroutine for one employee ─────────────────
            async def _worker(worker_page, emp_num: str, idx: int):
                """Process a single employee in its own (pre-created) browser tab."""
                try:
                    print(f"[{ts()}] ── [Tab] Employee {idx}/{len(employee_numbers)}: {emp_num} ──")

                    # Navigate this tab to Content Server home
                    await worker_page.goto(
                        CONTENT_SERVER_URL,
                        wait_until="domcontentloaded",
                        timeout=NAV_TIMEOUT,
                    )
                    await worker_page.wait_for_timeout(2000)
                    await wait_for_content_server_home(worker_page, timeout_s=30)

                    # Download F&F docs for this employee
                    downloaded = await download_ff_for_employee(
                        worker_page, emp_num, DOWNLOAD_DIR
                    )
                    all_results[emp_num] = downloaded
                except Exception as e:
                    print(f"[{ts()}] [Tab] Error for {emp_num}: {e}")
                    all_results[emp_num] = []

            # ── Step 4: Process employees in batches ──────────────────────
            # Split employee list into batches matching available tabs.
            # Each batch runs concurrently; batches run sequentially.
            for batch_start in range(0, len(employee_numbers), actual_concurrency):
                batch = employee_numbers[batch_start : batch_start + actual_concurrency]
                batch_tasks = []
                for i, emp_num in enumerate(batch):
                    page = worker_pages[i]
                    idx = batch_start + i + 1
                    batch_tasks.append(_worker(page, emp_num, idx))

                await asyncio.gather(*batch_tasks)

            # Close all tabs
            for wp in worker_pages:
                try:
                    await wp.close()
                except Exception:
                    pass

            await context.close()

    except Exception as e:
        print(f"[{ts()}] FATAL: {e}\n{traceback.format_exc()}")

    # ── Summary ───────────────────────────────────────────────────────────────
    print()
    print(f"[{ts()}] ═══════════════════════════════════════════════════════════")
    print(f"[{ts()}] DOWNLOAD SUMMARY")
    print(f"[{ts()}] ═══════════════════════════════════════════════════════════")
    total_files = 0
    for emp_num, files in all_results.items():
        status = f"{len(files)} file(s)" if files else "FAILED / No documents"
        print(f"[{ts()}]   {emp_num}: {status}")
        total_files += len(files)
    print(f"[{ts()}] ───────────────────────────────────────────────────────────")
    print(f"[{ts()}]   Total: {total_files} file(s) downloaded for {len(all_results)} employee(s)")
    print(f"[{ts()}] ═══════════════════════════════════════════════════════════")


if __name__ == "__main__":
    numbers = prompt_employee_numbers()
    if numbers:
        asyncio.run(download_ff_reports(numbers))
    else:
        print("No employee numbers entered. Exiting.")
