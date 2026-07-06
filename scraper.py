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

# --- Reliability tuning ---
WAIT_TIMEOUT = 30        # seconds to wait for a page element before giving up
LOGIN_MAX_ATTEMPTS = 3   # how many times to retry a full login+scrape per user
RETRY_BACKOFF = 5        # seconds to wait between retry attempts
DEBUG_DIR = "debug"      # where screenshots + page HTML are dumped on failure

USERS = [
    {"email": os.getenv("EMAIL"), "password": os.getenv("PASSWORD")},
    {"email": os.getenv("EMAIL2"), "password": os.getenv("PASSWORD2")},
]

def save_debug(driver, label):
    """Dump a screenshot + page HTML so we can see what the CI runner saw on failure."""
    try:
        os.makedirs(DEBUG_DIR, exist_ok=True)
        driver.save_screenshot(os.path.join(DEBUG_DIR, f"{label}.png"))
        with open(os.path.join(DEBUG_DIR, f"{label}.html"), "w", encoding="utf-8") as f:
            f.write(driver.page_source)
        print(f"🧾 Saved debug artifacts: {label}.png / {label}.html")
    except Exception as e:
        print(f"⚠️ Could not save debug artifacts for {label}: {e}")

def get_appsheet_client():
    return AppSheetClient(
        app_id=os.environ.get("APPSHEET_APP_ID"),
        api_key=os.environ.get("APPSHEET_APP_KEY"),
    )

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

def login(driver, user):
    """Perform login. Waits for real content rather than fixed sleeps. Raises on failure."""
    wait = WebDriverWait(driver, WAIT_TIMEOUT)
    driver.get(LOGIN_URL)

    # Wait for the email field to actually render (this is what used to time out at 10s)
    email_field = wait.until(EC.presence_of_element_located((By.ID, "email")))
    email_field.clear()
    email_field.send_keys(user["email"])

    driver.find_element(By.ID, "password").send_keys(user["password"])
    driver.find_element(By.ID, "login_button").click()

    # Wait until login actually completes (we leave the sign-in page) instead of sleeping blindly
    wait.until(lambda d: "sign-in" not in d.current_url)


def scrape_events(driver, user):
    """Load the events page, wait for the table to render, then parse rows."""
    wait = WebDriverWait(driver, WAIT_TIMEOUT)
    driver.get(EVENTS_URL)

    # Wait until the table rows AND their inner title links have rendered.
    # This is the fix for the intermittent "no such element: a[title]" skips:
    # previously a fixed time.sleep(3) read the table before the SPA finished rendering.
    try:
        wait.until(EC.presence_of_element_located(
            (By.CSS_SELECTOR, "table tbody tr td a[title]")))
    except Exception:
        # Could be a genuinely empty events list — log and carry on with whatever is there.
        print("⚠️ No event rows with a title link appeared within the timeout.")
        save_debug(driver, f"no-rows-{user['email'].split('@')[0]}")

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

    return results


def login_and_scrape(user, label):
    """Log in and scrape for one user, retrying the whole flow on transient failures.

    Each attempt uses a fresh browser. One user failing here does not affect the other
    user or the AppSheet update — main() isolates them.
    """
    last_error = None
    for attempt in range(1, LOGIN_MAX_ATTEMPTS + 1):
        driver = setup_browser()
        try:
            print(f"🔐 Logging in as {user['email']} (attempt {attempt}/{LOGIN_MAX_ATTEMPTS})")
            login(driver, user)
            return scrape_events(driver, user)
        except Exception as e:
            last_error = e
            print(f"⚠️ Attempt {attempt}/{LOGIN_MAX_ATTEMPTS} failed for {user['email']}: {e}")
            save_debug(driver, f"{label}-attempt{attempt}")
            if attempt < LOGIN_MAX_ATTEMPTS:
                print(f"⏳ Retrying in {RETRY_BACKOFF}s...")
                time.sleep(RETRY_BACKOFF)
        finally:
            driver.quit()

    # All attempts exhausted — surface the failure to main() which decides how to proceed.
    raise last_error

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
            # print(f"Matching Event '{ticket['title']}' on {ticket_date_obj} against Row '{record.get('הפקה', '')}' on {row_date_obj}'")
            
            if (
                title_match
                and row_date_obj == ticket_date_obj
                and record.get("ארגון") == "מבלים"
            ):
                # Prepare update for this record
                updates.append({
                    "ID": record.get("ID"),
                    "נמכרו": ticket["sold"],
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
        if success:
            print(f"✅ Successfully updated {len(updated_IDs)} rows in AppSheet.")
        else:
            print("❌ Failed to update AppSheet.")
    else:
        print("⚠️ No matching rows found to update.")

    # ✅ Print result summary

    print("🟩 IDs updated:", updated_IDs)
    print(tabulate(updated_data, headers="keys", tablefmt="grid", stralign="center"))

    if not_updated:
        print(f"⚠️ {len(not_updated)} items were NOT matched in AppSheet:")
        print(tabulate(not_updated, headers="keys", tablefmt="grid", stralign="center"))
    else:
        print("✅ All items matched and updated successfully.")

def main():
    all_events = []
    failed_users = []
    for i, user in enumerate(USERS):
        # Isolate each user: if one login fails after all retries, we log it,
        # keep whatever the other user scraped, and still push that to AppSheet.
        try:
            user_events = login_and_scrape(user, label=f"user{i + 1}")
            all_events.extend(user_events)
            print(f"✅ Got {len(user_events)} events from user {i + 1}.")
        except Exception as e:
            print(f"❌ User {i + 1} failed after {LOGIN_MAX_ATTEMPTS} attempts, skipping: {e}")
            failed_users.append(i + 1)

        if i < len(USERS) - 1:
            print("⏱ Waiting 5 seconds before next login...")
            time.sleep(5)

    print(f"✅ Scraped {len(all_events)} events total.")
    if failed_users:
        print(f"⚠️ {len(failed_users)} of {len(USERS)} user(s) failed: {failed_users}")

    # ✅ Update Google Sheet
    try:
        update_appsheet_with_ticket_data(all_events)
    except Exception as e:
        print("❌ Failed to update Google Sheet:", e)

    # Fail the CI run (red) only if EVERY user failed — a partial success stays green but logged.
    if failed_users and len(failed_users) == len(USERS):
        print("❌ All users failed — exiting with error so the run is flagged.")
        exit(1)

if __name__ == "__main__":
    main()
