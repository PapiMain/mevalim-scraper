import os
import time
import json
from dotenv import load_dotenv
from selenium import webdriver
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
import requests
from sys import exit
from datetime import datetime
from tabulate import tabulate
import re
import pytz
from py_appsheet import AppSheetClient


# Load environment variables
load_dotenv()

# Sanity check for environment variables
if not all([os.getenv("EMAIL"), os.getenv("PASSWORD"), os.getenv("EMAIL2"), os.getenv("PASSWORD2")]):
    print("❌ ERROR: Missing environment variables. Please check your .env file.")
    exit(1)

LOGIN_URL = "https://tickets.mevalim.co.il/auth/sign-in"
EVENTS_URL = "https://tickets.mevalim.co.il/manager/events"
# WEBHOOK_URL = os.getenv("WEBHOOK_URL")

USERS = [
    {"email": os.getenv("EMAIL"), "password": os.getenv("PASSWORD")},
    {"email": os.getenv("EMAIL2"), "password": os.getenv("PASSWORD2")},
]

def get_appsheet_client():
    return AppSheetClient(
        app_id=os.environ.get("APPSHEET_APP_ID"),
        api_key=os.environ.get("APPSHEET_APP_KEY"),
    )

# def get_short_names():
#     """Fetches show names from AppSheet instead of GSpread."""
#     client = get_appsheet_client()
#     try:
#         # Fetching from 'הפקות' table
#         rows = client.find_items("הפקות", "")
#         return [row["שם מקוצר"] for row in rows if row.get("שם מקוצר")]
#     except Exception as e:
#         print(f"❌ Error fetching short names: {e}")
#         return []

def send_appsheet_batch(table_name, updates):
    """Sends a batch 'Edit' action directly to the AppSheet API."""
    app_id = os.environ.get("APPSHEET_APP_ID")
    api_key = os.environ.get("APPSHEET_APP_KEY")
    
    url = f"https://api.appsheet.com/api/v1/apps/{app_id}/tables/{table_name}/Action"
    
    headers = {
        "ApplicationAccessKey": api_key,
        "Content-Type": "application/json"
    }
    
    body = {
        "Action": "Edit",
        "Properties": {
            "Locale": "en-US",
            "Timezone": "Israel Standard Time"
        },
        "Rows": updates
    }
    
    try:
        response = requests.post(url, headers=headers, json=body)
        response.raise_for_status()
        print(f"✅ AppSheet API Response: {response.status_code} - Success")
        return True
    except Exception as e:
        print(f"❌ API Post Error: {e}")
        if hasattr(e, 'response') and e.response is not None:
            print(f"Context: {e.response.text}")
        return False
    
def setup_browser():
    options = Options()
    options.add_argument("--headless")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    driver = webdriver.Chrome(options=options)
    return driver

def login_and_scrape(user):
    print(f"🔐 Logging in as {user['email']}")
    driver = setup_browser()
    driver.get(LOGIN_URL)
    
    # Wait up to 10 seconds for the email field to appear
    wait = WebDriverWait(driver, 10)
    email_field = wait.until(EC.presence_of_element_located((By.ID, "email")))
    email_field.send_keys(user["email"])
    
    driver.find_element(By.ID, "password").send_keys(user["password"])
    
    # Click the button using the new ID
    driver.find_element(By.ID, "login_button").click()
    
    time.sleep(5)
    driver.get(EVENTS_URL)
    time.sleep(3)

    rows = driver.find_elements(By.CSS_SELECTOR, "table tbody tr")
    results = []

    for row in rows:
        try:
            cols = row.find_elements(By.TAG_NAME, "td")
            if len(cols) < 3:
                continue

            # new
            # === Extract Title ===
            title_el = cols[1].find_element(By.CSS_SELECTOR, "a[title]")
            title = title_el.get_attribute("title").strip()
            
            # === Extract time, date, location ===
            spans = cols[1].find_elements(By.CSS_SELECTOR, "span.text-xs")
            time_str = spans[0].text.strip() if len(spans) > 0 else ""
            date_str = spans[1].text.strip().replace(".", "/") if len(spans) > 1 else ""
            location = spans[2].text.strip() if len(spans) > 2 else ""


            # --- Get the sold number
            try:
                # Check for the <a> tag (non-zero sold)
                sold_a = cols[2].find_elements(By.CSS_SELECTOR, "a.text-slate-800.font-medium")
                if sold_a:
                    sold = int(sold_a[0].text.strip())
                else:
                    # Fallback to <div> which means sold is zero
                    sold = 0
                    print(f"⚠️ it was a div")
            except Exception as e:
                print(f"⚠️ Couldn't extract 'sold' from row: {e}")
                # print(f"Row HTML: {row.get_attribute('outerHTML')}")
                sold = 0
                continue

            # --- Get the available number (extract number from "47 נותרו")
            try:
                available_div = cols[2].find_element(By.XPATH, ".//div[contains(@class,'flex-col')]//div[contains(text(),'נותרו')]")
                match = re.search(r'(\d+)', available_div.text.strip())
                available = int(match.group(1)) if match else 0
            except Exception as e:
                print(f"⚠️ Couldn't extract 'available' from row: {e}")
                available = 0

            results.append({
                "title": title,
                "date": date_str,
                "time": time_str,
                "sold": sold,
                "available": available,
                "location": location,
                "sourceUser": user["email"]
            })

        except Exception as e:
            print(f"⚠️ Skipped row due to error: {e}")
            continue

    driver.quit()
    return results

def update_appsheet_with_ticket_data(all_ticket_data):
    print("📥 Updating AppSheet with ticket data...")

    israel_tz = pytz.timezone("Asia/Jerusalem")
    # Format: 2026-03-17 14:09:00
    now_in_israel = datetime.now(israel_tz).strftime('%Y-%m-%d %H:%M:00')

    client = get_appsheet_client()
    
    try:
        # Fetch existing records from AppSheet
        print("⏳ Fetching current AppSheet records for matching...")
        existing_records = client.find_items("הופעות עתידיות", "")
    except Exception as e:
        print(f"❌ Error fetching existing records from AppSheet: {e}")
        return

    updated_IDs = []
    not_updated = []
    updated_data = []
    updates = []  # Collect all updates here

    # --- Loop through all tickets and find matching row ---
    for ticket in all_ticket_data:
        ticket_date_str = ticket["date"]
        found = False

        try:
            # 1. Convert ticket date string to a DATE OBJECT
            if len(ticket_date_str.split("/")[-1]) == 2:
                dt = datetime.strptime(ticket_date_str, "%d/%m/%y")
            else:
                dt = datetime.strptime(ticket_date_str, "%d/%m/%Y")
            
            ticket_date_obj = dt.date() # Keep it as a date object for comparison
        except Exception as e:
            print(f"❌ Date parsing error for {ticket_date_str}: {e}")
            not_updated.append(ticket)
            continue

        for record in existing_records:
            row_date_str = str(record.get("תאריך", ""))
            if not row_date_str:
                continue
            
            try:
                # 2. Convert AppSheet row string to a DATE OBJECT
                # AppSheet often sends MM/DD/YYYY, but let's be safe
                if "/" in row_date_str:
                    try:
                        row_date_obj = datetime.strptime(row_date_str, "%m/%d/%Y").date()
                    except ValueError:
                        row_date_obj = datetime.strptime(row_date_str, "%d/%m/%Y").date()
                else:
                    # If it's already ISO format (YYYY-MM-DD)
                    row_date_obj = datetime.fromisoformat(row_date_str).date()
            except Exception:
                continue

            title_match = (
                ticket["title"].strip() in record.get("הפקה", "").strip()
                or record.get("הפקה", "").strip() in ticket["title"].strip()
            )

            # for debugging:
            print(f"Matching Event '{ticket['title']}' on {ticket_date_obj} against Row '{record.get('הפקה', '')}' on {row_date_obj}'")
            
            if (
                title_match
                and row_date_obj == ticket_date_obj
                and record.get("ארגון") == "מבלים"
            ):
                # Prepare update for this record
                updates.append({
                    "ID": record.get("ID"),
                    "נמכרו": ticket["sold"],
                    # "קיבלו": ticket["available"],
                    "עודכן לאחרונה": now_in_israel,
                })
                updated_IDs.append(record.get("ID"))
                updated_data.append(ticket)
                found = True
                break

        if not found:
            not_updated.append(ticket)

    # --- Send updates to AppSheet ---
    if updates:
        success = send_appsheet_batch("כרטיסים", updates)
        print(f"✅ Batch updated {len(updated_IDs)} rows in sheet.")
        if success:
            print(f"✅ Successfully updated {len(updated_IDs)} rows in AppSheet.")
        else:
            print("❌ Failed to update AppSheet.")
    else:
        print("⚠️ No matching rows found to update.")

    # ✅ Print result summary

    print(f"✅ Updated {len(updated_IDs)} rows in AppSheet.")
    print(f"🗂️  That covers {len(updated_data)} unique events.")
    print("🟩 IDs updated:", updated_IDs)
    print(tabulate(updated_data, headers="keys", tablefmt="grid", stralign="center"))

    if not_updated:
        print(f"⚠️ {len(not_updated)} items were NOT matched in AppSheet:")
        print(tabulate(not_updated, headers="keys", tablefmt="grid", stralign="center"))
    else:
        print("✅ All items matched and updated successfully.")

def main():
    all_events = []
    for i, user in enumerate(USERS):
        user_events = login_and_scrape(user)
        all_events.extend(user_events)
        if i < len(USERS) - 1:
            print("⏱ Waiting 5 seconds before next login...")
            time.sleep(5)

    print(f"✅ Scraped {len(all_events)} events total.")

    # ✅ Update Google Sheet
    try:
        update_appsheet_with_ticket_data(all_events)
    except Exception as e:
        print("❌ Failed to update Google Sheet:", e)

if __name__ == "__main__":
    main()
