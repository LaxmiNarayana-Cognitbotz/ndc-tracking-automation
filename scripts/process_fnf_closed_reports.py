"""
Downloads F&F report from Sharepoint, runs F&F extraction, and uploads results.
"""

import asyncio
import importlib
import os
import sys
import traceback
from pathlib import Path

# Add project root to sys.path so we can import from scripts.*
sys.path.insert(0, str(Path(__file__).parent.parent))

import urllib.parse
from datetime import datetime
from pathlib import Path

import httpx
import pandas as pd
from dotenv import load_dotenv

# Load environment variables
load_dotenv(verbose=True)

from scripts.upload_to_sharepoint import (
    get_access_token,
    get_site_id,
    get_drive_details,
    encode_path_segments,
    TARGET_FOLDER,
    upload_file,
    FF_REPORTS_DIR
)

# Dynamically import the module since it contains an ampersand
download_module = importlib.import_module("scripts.download_f&f_report")
download_ff_reports = download_module.download_ff_reports

# ── Logging Setup ────────────────────────────────────────────────────────────
LOG_DIR = Path(__file__).parent.parent / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_DIR / "process_fnf_closed_reports.log"

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

UPLOADS_DIR = Path(__file__).parent.parent / "uploads"
LOCAL_EXCEL_DIR = UPLOADS_DIR / "FNF_Active_Reports"
LOCAL_EXCEL_DIR.mkdir(parents=True, exist_ok=True)

def ts():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

async def process_fnf_active_reports():
    print(f"[{ts()}] Starting F&F Active Reports processing...")
    

    
    async with httpx.AsyncClient(timeout=60.0, verify=False) as client:
        try:
            print(f"[{ts()}] Connecting to SharePoint...")
            site_id = await get_site_id(client)
            drive_id, folder_path = await get_drive_details(client, site_id)
            token = await get_access_token()
            headers = {"Authorization": f"Bearer {token}"}
            
            # 1. List files in the F&F_Active_Report folder
            # The folder_path returned by get_drive_details is relative to the Drive root.
            fnf_folder_in_drive = f"{folder_path}/F&F_Active_Report".strip("/")
            encoded_path = encode_path_segments(fnf_folder_in_drive)
            
            url = f"https://graph.microsoft.com/v1.0/sites/{site_id}/drives/{drive_id}/root:/{encoded_path}:/children"
            print(f"[{ts()}] Checking SharePoint directory: {fnf_folder_in_drive}")
            
            response = await client.get(url, headers=headers)
            if response.status_code != 200:
                print(f"[{ts()}] [ERROR] Failed to list SharePoint folder: {response.status_code} - {response.text}")
                return

            children = response.json().get("value", [])
            excel_files = []
            for item in children:
                if "file" in item:
                    name = item.get("name", "")
                    if name.lower().endswith(".xlsx"):
                        excel_files.append(item)
            
            print(f"[{ts()}] Found {len(excel_files)} Excel file(s) in '{fnf_folder_in_drive}'.")
            
            if not excel_files:
                return

            for file_item in excel_files:
                file_name = file_item.get("name")
                download_url = file_item.get("@microsoft.graph.downloadUrl")
                if not download_url:
                    print(f"[{ts()}] [ERROR] File '{file_name}' does not have a download URL.")
                    continue
                
                local_file_path = LOCAL_EXCEL_DIR / file_name
                print(f"[{ts()}] Downloading '{file_name}'...")
                
                dl_response = await client.get(download_url)
                if dl_response.status_code != 200:
                    print(f"[{ts()}] [ERROR] Failed to download '{file_name}' (status {dl_response.status_code}).")
                    continue
                
                with open(local_file_path, "wb") as f:
                    f.write(dl_response.content)
                
                print(f"[{ts()}] Successfully downloaded '{file_name}'. Extracting Person Numbers...")
                
                # 2. Read the Excel file and extract IDs
                try:
                    df = pd.read_excel(local_file_path)
                    # Support 'Person Number' or 'Employee ID' variations
                    person_col = None
                    for col in df.columns:
                        col_str = str(col).strip().lower()
                        if "person number" in col_str or "employee id" in col_str:
                            person_col = col
                            break
                    
                    if not person_col:
                        print(f"[{ts()}] [ERROR] Could not find 'Person Number' column in {file_name}. Columns found: {list(df.columns)}")
                        continue
                        
                    # Extract IDs, ensuring they are strings and removing NAs
                    employee_numbers = df[person_col].dropna().astype(str).str.replace(r'\.0$', '', regex=True).str.strip().tolist()
                    employee_numbers = [e for e in employee_numbers if e and e.lower() != "nan"]
                    
                    # Deduplicate
                    unique_employees = list(dict.fromkeys(employee_numbers))
                    print(f"[{ts()}] Found {len(unique_employees)} unique employee(s) in {file_name}: {unique_employees}")
                    
                    if not unique_employees:
                        print(f"[{ts()}] No valid employee IDs found in {file_name}.")
                        continue
                        
                    # 3. Define the upload callback for parallel upload
                    target_base_url = f"{folder_path}/F&F_Documents"
                    
                    async def on_employee_downloaded(emp_id: str, downloaded_paths: list):
                        emp_target_url = f"{target_base_url}/{emp_id}"
                        for file_path_str in downloaded_paths:
                            f_path = Path(file_path_str)
                            if f_path.exists():
                                try:
                                    await upload_file(client, site_id, drive_id, emp_target_url, f_path)
                                    print(f"[{ts()}] Uploaded successfully: {f_path.name} for employee {emp_id}")
                                    f_path.unlink()  # delete after successful upload
                                except Exception as e:
                                    print(f"[{ts()}] [ERROR] Failed to upload {f_path.name} for {emp_id}: {e}")
                                    
                        # Try to remove the employee folder if empty
                        emp_dir = FF_REPORTS_DIR / emp_id
                        if emp_dir.exists() and emp_dir.is_dir():
                            try:
                                emp_dir.rmdir()
                            except:
                                pass

                    # 4. Call the F&F download script with the callback
                    print(f"[{ts()}] Initiating F&F document download for {len(unique_employees)} employee(s)...")
                    await download_ff_reports(unique_employees, upload_callback=on_employee_downloaded)
                    
                    # 5. Clean up local Excel file
                    if local_file_path.exists():
                        local_file_path.unlink()
                        print(f"[{ts()}] [CLEANUP] Deleted local Excel file: {file_name}")
                                

                except Exception as e:
                    print(f"[{ts()}] [ERROR] Error processing '{file_name}': {e}\n{traceback.format_exc()}")
                
        except Exception as e:
            print(f"[{ts()}] [ERROR] SharePoint operation failed: {e}\n{traceback.format_exc()}")
            
    print(f"[{ts()}] Finished F&F Active Reports processing.")

if __name__ == "__main__":
    asyncio.run(process_fnf_active_reports())
