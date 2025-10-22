import os
import time
import json
from dotenv import load_dotenv
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
import requests
from sys import exit
import gspread
from google.oauth2.service_account import Credentials
from datetime import datetime
from tabulate import tabulate
import re
import pytz

# Load environment variables
load_dotenv()

# Sanity check for environment variables
if not all([os.getenv("EMAIL"), os.getenv("PASSWORD"), os.getenv("EMAIL2"), os.getenv("PASSWORD2")]):
    print("❌ ERROR: Missing environment variables. Please check your .env file.")
    exit(1)

SCOPES = ['https://www.googleapis.com/auth/spreadsheets', 'https://www.googleapis.com/auth/drive']
# SERVICE_ACCOUNT_FILE = 'creds/service_account.json'

json_str = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
service_account_info = json.loads(json_str)

def get_gspread_client():

    # 🔧 Fix the line breaks in the private key
    if "private_key" in service_account_info:
        service_account_info["private_key"] = service_account_info["private_key"].replace("\\n", "\n")

    creds = Credentials.from_service_account_info(service_account_info, scopes=SCOPES)
    return gspread.authorize(creds)


LOGIN_URL = "https://tickets.mevalim.co.il/auth/sign-in"
EVENTS_URL = "https://tickets.mevalim.co.il/manager/events"
WEBHOOK_URL = os.getenv("WEBHOOK_URL")

USERS = [
    {"email": os.getenv("EMAIL"), "password": os.getenv("PASSWORD")},
    {"email": os.getenv("EMAIL2"), "password": os.getenv("PASSWORD2")},
]

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

    time.sleep(2)
    driver.find_element(By.ID, "email").send_keys(user["email"])
    driver.find_element(By.ID, "password").send_keys(user["password"])
    driver.find_element(By.CSS_SELECTOR, "button[type=submit]").click()

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

            # === Inside cols[1] is all the data we're after ===
            title = cols[1].find_element(By.CSS_SELECTOR, "a[title]").get_attribute("title").strip()

            spans = cols[1].find_elements(By.CSS_SELECTOR, "div.items-center span.text-xs")
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


# Update Google Sheet with ticket data
def update_sheet_with_ticket_data(sheet, all_ticket_data): 
    print("📥 Updating Google Sheet with ticket data...")

    records = sheet.get_all_records()
    headers = sheet.row_values(1)

    sold_col = headers.index("נמכרו")
    total_col = headers.index("קיבלו")
    updated_col = headers.index("עודכן לאחרונה")

    updated_rows = []
    not_updated = []

    for ticket in all_ticket_data:
        ticket_date = ticket["date"]
        israel_tz = pytz.timezone('Asia/Jerusalem')
        now_in_israel = datetime.now(israel_tz)

        found = False
        for i, row in enumerate(records, start=2):  # start=2 to skip header
            if (
                row.get("הפקה") == ticket["title"]
                and row.get("תאריך") == ticket_date
                and row.get("ארגון") == "מבלים"
            ):
                sheet.update_cell(i, sold_col + 1, ticket["sold"])
                # sheet.update_cell(i, total_col + 1, ticket["available"])
                sheet.update_cell(i, updated_col + 1, now_in_israel.strftime("%d/%m/%Y %H:%M"))
                updated_rows.append(i)
                found = True
                break

        if not found:
            not_updated.append(ticket)

    # ✅ Print result summary
    updated_data = []

    for ticket in all_ticket_data:
        ticket_date = ticket["date"]
        try:
            dt = datetime.strptime(ticket_date, "%d/%m/%y") if len(ticket_date.split("/")[-1]) == 2 else datetime.strptime(ticket_date, "%d/%m/%Y")
            ticket_date = dt.strftime("%d/%m/%Y")
        except:
            continue
        for i in updated_rows:
            row = records[i - 2]
            if (
                ticket["title"] == row.get("הפקה") and
                ticket_date == row.get("תאריך")
            ):
                updated_data.append(ticket)
                break

    # Now print like not_updated
    print(f"✅ Updated {len(updated_rows)} rows in sheet.")
    print(f"🗂️  That covers {len(updated_data)} unique events.")
    print("🟩 Row numbers updated:", updated_rows)
    print(tabulate(updated_data, headers="keys", tablefmt="grid", stralign="center"))

    if not_updated:
        print(f"\n⚠️ {len(not_updated)} items were NOT matched in the sheet:")
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
    print(f"🔍 Loaded service account email: {service_account_info.get('client_email')}")
    if not service_account_info.get("private_key"):
        print("❌ ERROR: No private key found in GOOGLE_SERVICE_ACCOUNT_JSON.")
        exit(1)
        
    # --- backup update, sends it to make automation to update sheet
    # try:
    #     if WEBHOOK_URL:
    #         res = requests.post(WEBHOOK_URL, json={"events": all_events})
    #         res.raise_for_status()
    #         print("🚀 Data sent to Make successfully.")
    # except Exception as e:
    #     print("❌ Failed to send to Make:", e)

    # ✅ Update Google Sheet
    try:
        client = get_gspread_client()
        sheet = client.open("דאטה אפשיט אופיס").worksheet("כרטיסים")
        update_sheet_with_ticket_data(sheet, all_events)
    except Exception as e:
        print("❌ Failed to update Google Sheet:", e)

if __name__ == "__main__":
    main()
