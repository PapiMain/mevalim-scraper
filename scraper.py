import os
import time
import json
from dotenv import load_dotenv
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
import requests
from sys import exit

# Load environment variables
load_dotenv()

# Sanity check for environment variables
if not all([os.getenv("EMAIL"), os.getenv("PASSWORD"), os.getenv("EMAIL2"), os.getenv("PASSWORD2")]):
    print("❌ ERROR: Missing environment variables. Please check your .env file.")
    exit(1)


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

    time.sleep(5)  # wait for redirect
    driver.get(EVENTS_URL)
    time.sleep(3)

    rows = driver.find_elements(By.CSS_SELECTOR, "table tbody tr")
    results = []

    for row in rows:
        cols = row.find_elements(By.TAG_NAME, "td")
        if len(cols) >= 6:
            event = {
                "title": cols[1].text.strip(),
                "sold": cols[2].text.strip().split(" ")[0],
                "available": cols[3].text.strip(),
                "dateTime": cols[4].text.strip(),
                "location": cols[5].text.strip(),
                "sourceUser": user["email"]
            }
            results.append(event)

    driver.quit()
    return results

def main():
    all_events = []
    for i, user in enumerate(USERS):
        user_events = login_and_scrape(user)
        all_events.extend(user_events)
        if i < len(USERS) - 1:
            print("⏱ Waiting 5 seconds before next login...")
            time.sleep(5)

    print(f"✅ Scraped {len(all_events)} events total.")
    try:
        if WEBHOOK_URL:
            res = requests.post(WEBHOOK_URL, json={"events": all_events})
            res.raise_for_status()
            print("🚀 Data sent to Make successfully.")
        else:
            print(json.dumps(all_events, ensure_ascii=False, indent=2))
    except Exception as e:
        print("❌ Failed to send to Make:", e)

if __name__ == "__main__":
    main()
