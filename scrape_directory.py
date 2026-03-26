"""
MySLC Campus Directory Scraper
==============================
Connects to an already-authenticated Chrome session via remote debugging
and scrapes every student name from A–Z, handling sub-section pagination
(e.g. Ba-Be, Be-Bi, Bi-Br …) automatically within each letter.

Setup (run ONCE before this script):
  1. Quit Chrome completely.
  2. Open Terminal and run:
       "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
         --remote-debugging-port=9222 \
         --user-data-dir=/tmp/chrome-debug
  3. In that Chrome window, log into MySLC and navigate to the campus directory.
  4. Make sure letter A is selected (it is by default).
  5. Run this script:  python scrape_directory.py
"""

import csv
import string
import time

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait, Select
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import (
    TimeoutException,
    NoSuchElementException,
    StaleElementReferenceException,
    ElementClickInterceptedException,
)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
HEADLESS = False
CHROME_DEBUG_PORT = 9222
PAGE_LOAD_TIMEOUT = 15
OUTPUT_FILE = "student_directory_names.csv"
DIRECTORY_URL = "https://my.slc.edu/ICS/?tool=CampusDirectory"

# How long to pause after a postback click to let ASP.NET settle
POSTBACK_SETTLE = 2.0

# CSS selectors derived from the actual MySLC DOM
NAME_ROW_SELECTOR = "div.display-order-row:not(.header-row)"
LETTER_SELECTOR_NAV = "div.letterSelector"
LETTER_NAV = "div.letterNavigator"


def connect_to_chrome():
    """Attach Selenium to the already-running Chrome instance."""
    opts = Options()
    opts.add_experimental_option("debuggerAddress", f"127.0.0.1:{CHROME_DEBUG_PORT}")
    driver = webdriver.Chrome(options=opts)
    print(f"Connected to Chrome on port {CHROME_DEBUG_PORT}")
    print(f"Current URL: {driver.current_url}")
    print(f"Page title : {driver.title}\n")
    return driver


def is_redirect_error(driver):
    """Detect the MySLC 'you were redirected' error page."""
    try:
        body = driver.find_element(By.TAG_NAME, "body").text
        return "redirected to this page" in body.lower()
    except Exception:
        return False


def recover_to_directory(driver):
    """Navigate back to the campus directory page after a redirect error."""
    print("  ⚠ Detected redirect error — recovering…")
    driver.get(DIRECTORY_URL)
    WebDriverWait(driver, PAGE_LOAD_TIMEOUT).until(
        EC.presence_of_element_located((By.CSS_SELECTOR, LETTER_SELECTOR_NAV))
    )
    time.sleep(POSTBACK_SETTLE)
    ensure_students_filter(driver)


def ensure_students_filter(driver):
    """Make sure the 'Show' dropdown is set to 'Students'."""
    try:
        dropdown = WebDriverWait(driver, PAGE_LOAD_TIMEOUT).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "select"))
        )
        select = Select(dropdown)
        current = select.first_selected_option.text.strip().lower()
        if "student" not in current:
            for option in select.options:
                if "student" in option.text.strip().lower():
                    select.select_by_visible_text(option.text)
                    print(f"  Set 'Show' dropdown to: {option.text}")
                    time.sleep(POSTBACK_SETTLE)
                    break
        else:
            print(f"  'Show' dropdown already set to: {select.first_selected_option.text.strip()}")
    except TimeoutException:
        print("  WARNING: Could not find a 'Show' dropdown — continuing anyway.")


def click_letter(driver, letter, retries=2):
    """
    Click a letter in div.letterSelector.
    If a redirect error occurs, recover and retry.
    """
    for attempt in range(retries + 1):
        try:
            nav = WebDriverWait(driver, PAGE_LOAD_TIMEOUT).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, LETTER_SELECTOR_NAV))
            )
            links = nav.find_elements(By.TAG_NAME, "a")
            clicked = False
            for link in links:
                try:
                    if link.text.strip() == letter:
                        driver.execute_script("arguments[0].click();", link)
                        clicked = True
                        break
                except StaleElementReferenceException:
                    continue

            if not clicked:
                print(f"  Could not find link for letter {letter}")
                return False

            time.sleep(POSTBACK_SETTLE)

            if is_redirect_error(driver):
                recover_to_directory(driver)
                continue  # retry

            return True

        except Exception as exc:
            if attempt < retries:
                print(f"  Attempt {attempt+1} failed for letter {letter}: {exc} — retrying")
                recover_to_directory(driver)
            else:
                print(f"  Could not click letter {letter} after {retries+1} attempts: {exc}")
                return False
    return False


def wait_for_names(driver):
    """Wait until name rows appear after a navigation click."""
    try:
        WebDriverWait(driver, PAGE_LOAD_TIMEOUT).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, NAME_ROW_SELECTOR))
        )
    except TimeoutException:
        pass
    time.sleep(0.5)


def get_current_subsection(driver):
    """Return the currently active sub-section label (the bold one), or ''."""
    try:
        nav = driver.find_element(By.CSS_SELECTOR, LETTER_NAV)
        strong = nav.find_elements(By.TAG_NAME, "strong")
        if strong:
            return strong[0].text.strip()
    except (NoSuchElementException, StaleElementReferenceException):
        pass
    return ""


def extract_name_text(driver, element):
    """
    Pull only the human-readable name from a row element,
    ignoring child elements like the blue info-icon buttons.
    Reads direct text nodes via JS.
    """
    js = """
    var target = arguments[0].querySelector('[class*="col-"]') || arguments[0];
    var text = '';
    for (var i = 0; i < target.childNodes.length; i++) {
        if (target.childNodes[i].nodeType === 3) {
            text += target.childNodes[i].textContent;
        }
    }
    return text.trim();
    """
    return driver.execute_script(js, element)


def scrape_names_on_page(driver):
    """Extract student names from all display-order-row divs on the current page."""
    names = []
    rows = driver.find_elements(By.CSS_SELECTOR, NAME_ROW_SELECTOR)
    for row in rows:
        try:
            name = extract_name_text(driver, row)
            if name and len(name) > 1:
                names.append(name)
        except StaleElementReferenceException:
            continue
    return names


def find_next_page_link(driver):
    """
    Return the 'Next page' link inside div.letterNavigator, or None.
    The HTML text is 'Next page -->' (rendered as 'Next page →').
    """
    try:
        nav = driver.find_element(By.CSS_SELECTOR, LETTER_NAV)
        links = nav.find_elements(By.TAG_NAME, "a")
        for link in links:
            try:
                text = link.text.strip().lower()
                if "next page" in text:
                    return link
            except StaleElementReferenceException:
                continue
    except NoSuchElementException:
        pass
    return None


def click_next_subsection(driver, next_el):
    """Click the 'Next page' sub-section link and wait for the postback."""
    try:
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", next_el)
        time.sleep(0.3)
        driver.execute_script("arguments[0].click();", next_el)
        time.sleep(POSTBACK_SETTLE)
        return True
    except (StaleElementReferenceException, ElementClickInterceptedException) as exc:
        print(f"  Failed to click next sub-section: {exc}")
        return False


def scrape_all_subsections(driver, letter, all_names):
    """
    Scrape names from every sub-section of the current letter.
    Clicks 'Next page -->' until there are no more sub-sections.
    """
    subsection_num = 1

    while True:
        # Check for redirect error before scraping
        if is_redirect_error(driver):
            recover_to_directory(driver)
            if not click_letter(driver, letter):
                print(f"  Could not recover letter {letter} — skipping rest.")
                return
            wait_for_names(driver)

        subsection_label = get_current_subsection(driver)
        label_display = f" ({subsection_label})" if subsection_label else ""
        print(f"  Letter {letter} — sub-section {subsection_num}{label_display}")

        try:
            page_names = scrape_names_on_page(driver)
            print(f"  Found {len(page_names)} names")
            all_names.extend(page_names)
        except Exception as exc:
            print(f"  ERROR scraping letter {letter} sub-section {subsection_num}: {exc}")

        # Try to advance to the next sub-section
        next_el = find_next_page_link(driver)
        if next_el:
            print("  Moving to next sub-section →")
            if not click_next_subsection(driver, next_el):
                break
            wait_for_names(driver)
            subsection_num += 1
        else:
            print(f"  No more sub-sections for letter {letter}.")
            break


def save_to_csv(all_names, filename):
    """Deduplicate (preserving order) and write names to CSV."""
    seen = set()
    unique = []
    for name in all_names:
        key = name.strip()
        if key and key not in seen:
            seen.add(key)
            unique.append(key)

    with open(filename, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["name"])
        for name in unique:
            writer.writerow([name])

    return unique


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    driver = connect_to_chrome()

    # Confirm the Students filter is active
    ensure_students_filter(driver)

    all_names = []
    letters = list(string.ascii_uppercase)  # A … Z

    for i, letter in enumerate(letters):
        print(f"\n{'='*50}")
        print(f"Scraping letter: {letter}")
        print(f"{'='*50}")

        if i == 0:
            # Letter A is already loaded — no need to click
            print("  (Page already on letter A — starting scrape)")
            wait_for_names(driver)
        else:
            if not click_letter(driver, letter):
                print(f"  Skipping letter {letter} — could not click it.")
                continue
            wait_for_names(driver)

        scrape_all_subsections(driver, letter, all_names)

    # Save results
    print(f"\n{'='*50}")
    print("Saving results …")
    unique_names = save_to_csv(all_names, OUTPUT_FILE)
    print(f"Total raw names collected : {len(all_names)}")
    print(f"Total unique names saved  : {len(unique_names)}")
    print(f"Output file               : {OUTPUT_FILE}")
    print(f"{'='*50}")


if __name__ == "__main__":
    main()
