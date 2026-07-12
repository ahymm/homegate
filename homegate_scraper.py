import xml.etree.ElementTree as ET
import gzip
import io
import os
import time
import smtplib
import requests
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders
from datetime import datetime

# Sitemap namespace
NAMESPACES = {'ns': 'http://www.sitemaps.org/schemas/sitemap/0.9'}

BASE_DOMAIN = "https://www.homegate.ch"
MASTER_FILE = "homegate_listings.txt"  # sab purane + naye links yahan save hote hain (sirf links, koi date nahi)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; OAI-SearchBot/1.0; +http://openai.com)",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# Safety cap — agar server kabhi hamesha 200 dene lage (ghalati se) to bhi
# infinite loop na bane.
MAX_CLUSTERS_PER_CATEGORY = 500


def fetch_sitemap(url: str):
    """
    Ek gzipped sub-sitemap fetch/decompress/parse karta hai (sirf <loc> nikalta hai, lastmod ignore).
    Return: (status, urls_list)
      status = "ok"        -> mil gaya
      status = "not_found" -> 404 -> is category ka loop yahan rok dein
      status = "error"     -> koi aur masla -> is index ko skip kar k aage try karein
    """
    try:
        response = requests.get(url, headers=HEADERS, timeout=20)
    except Exception as e:
        print(f"  [Error] Request failed for {url.split('/')[-1]}: {e}")
        return "error", []

    if response.status_code == 404:
        return "not_found", []

    try:
        response.raise_for_status()
    except Exception as e:
        print(f"  [Error] Bad status for {url.split('/')[-1]}: {e}")
        return "error", []

    try:
        with gzip.GzipFile(fileobj=io.BytesIO(response.content)) as f:
            xml_content = f.read()
    except Exception as e:
        print(f"  [Error] Failed to decompress file: {e}")
        return "error", []

    try:
        root = ET.fromstring(xml_content)
        urls = [loc.text.strip() for loc in root.findall('.//ns:url/ns:loc', NAMESPACES) if loc.text]
        return "ok", urls
    except ET.ParseError:
        print("  [Error] Failed to parse XML structure.")
        return "error", []


def scrape_category(category: str) -> list:
    """
    pdp-0, pdp-1, pdp-2 ... aise barhta jata hai jab tak 404 (sitemap not found)
    na mil jaye, phir rok deta hai. Transient error par skip kar k aage badhta hai.
    """
    collected = []
    idx = 0
    consecutive_errors = 0

    while idx < MAX_CLUSTERS_PER_CATEGORY:
        url = f"{BASE_DOMAIN}/sitemap/pdp/pdp-{idx}-sitemap-{category}-en.xml.gz"
        filename = url.split("/")[-1]
        print(f"[{category}] Processing cluster: {filename}")

        status, urls = fetch_sitemap(url)

        if status == "not_found":
            print(f"  -> pdp-{idx} 404 mila. {category} category yahan khatam samjhi ja rahi hai.")
            break

        if status == "error":
            consecutive_errors += 1
            print(f"  -> Cluster skip kiya (error #{consecutive_errors} in a row).")
            if consecutive_errors >= 3:
                print(f"  -> {category}: 3 lagataar errors, safety k liye ruk rahe hain.")
                break
        else:
            consecutive_errors = 0
            collected.extend(urls)
            print(f"  -> Extracted {len(urls)} links. Category total so far: {len(collected)}")

        idx += 1
        time.sleep(1.0)

    return collected


def load_existing_links(path: str) -> set:
    """Purani master file se already-known links load karta hai."""
    if not os.path.exists(path):
        return set()
    with open(path, "r", encoding="utf-8") as f:
        return set(line.strip() for line in f if line.strip())


def send_email(subject: str, body: str, attachment_path: str = None):
    """
    Gmail SMTP (App Password) k through email bhejta hai.
    Attachment optional hai — agar koi naya link nahi mila to bina attachment k
    sirf "no new links" wala message chala jayega.
    """
    sender = os.environ["GMAIL_ADDRESS"]
    password = os.environ["GMAIL_APP_PASSWORD"]
    receiver = os.environ["RECEIVER_EMAIL"]

    msg = MIMEMultipart()
    msg["From"] = sender
    msg["To"] = receiver
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain"))

    if attachment_path:
        with open(attachment_path, "rb") as f:
            part = MIMEBase("application", "octet-stream")
            part.set_payload(f.read())
        encoders.encode_base64(part)
        part.add_header(
            "Content-Disposition",
            f'attachment; filename="{os.path.basename(attachment_path)}"',
        )
        msg.attach(part)

    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        server.starttls()
        server.login(sender, password)
        server.sendmail(sender, receiver, msg.as_string())


if __name__ == "__main__":
    print("Starting Homegate dynamic scrape (BUY aur RENT dono k liye jab tak 404 na mile)...")

    # 1. Purane (already known) links load karo
    existing_links = load_existing_links(MASTER_FILE)
    print(f"Loaded {len(existing_links)} previously known links from {MASTER_FILE}.")

    # 2. Fresh scrape
    print("\n--- BUY category ---")
    buy_links = scrape_category("BUY")
    print(f"BUY category total: {len(buy_links)} links")

    print("\n--- RENT category ---")
    rent_links = scrape_category("RENT")
    print(f"RENT category total: {len(rent_links)} links")

    all_extracted_listings = buy_links + rent_links
    current_links = set(all_extracted_listings)

    # 3. Naye links nikalo (jo pehle nahi thay)
    new_links = sorted(current_links - existing_links)
    print(f"\nTotal scraped this run: {len(current_links)} | Brand-new links: {len(new_links)}")

    timestamp_label = datetime.now().strftime("%Y-%m-%d %H:%M")

    if new_links:
        timestamp_file = datetime.now().strftime("%Y%m%d_%H%M%S")
        new_links_file = f"new_links_{timestamp_file}.txt"

        # Naye links ki alag .txt file (SIRF links, koi lastmod/date nahi)
        with open(new_links_file, "w", encoding="utf-8") as f:
            for link in new_links:
                f.write(f"{link}\n")

        # Purani master file mein naye links append karo (sirf links)
        with open(MASTER_FILE, "a", encoding="utf-8") as f:
            for link in new_links:
                f.write(f"{link}\n")

        print(f"Appended {len(new_links)} new links to {MASTER_FILE}")

        subject = f"Homegate - {len(new_links)} New Listings - {timestamp_label}"
        body = (
            f"{len(new_links)} new listing(s) mile hain is run mein.\n\n"
            f"Attached .txt file mein poori list hai (sirf links)."
        )

        try:
            send_email(subject, body, attachment_path=new_links_file)
            print("Email sent successfully (with new links attached).")
        except Exception as e:
            print(f"[Error] Email sending failed: {e}")

        os.remove(new_links_file)
    else:
        print("Koi naya link nahi mila is run mein.")

        subject = f"Homegate - No New Listings - {timestamp_label}"
        body = "Is run mein koi naya link nahi mila."

        try:
            send_email(subject, body, attachment_path=None)
            print("Email sent successfully (no new links notice).")
        except Exception as e:
            print(f"[Error] Email sending failed: {e}")

    print("\nDone.")
