import os
import re
import json
import hashlib
from pathlib import Path
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]
SOURCE_CHANNEL = os.getenv("SOURCE_CHANNEL", "offertedale")

MAX_PRICE = 300.0
MIN_DISCOUNT = 40
VERY_LOW_PRICE = 150.0

SOURCE_URL = f"https://t.me/s/{SOURCE_CHANNEL}"
STATE_FILE = Path("state.json")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; RobotDealBot/1.0)"
}

ROBOT_RE = re.compile(
    r"robot.*(aspirapolvere|aspira.?lava|lavapavimenti|"
    r"mop|vacuum|roborock|dreame|ecovacs|eufy|roomba|"
    r"lubluelu|tineco|xiaomi|cecotec|narwal|yeedi)",
    re.IGNORECASE,
)

PRICE_RE = re.compile(
    r"(?:€\s*)?(\d{1,4}(?:[.,]\d{1,2})?)\s*€"
    r"|€\s*(\d{1,4}(?:[.,]\d{1,2})?)",
    re.IGNORECASE,
)

DISCOUNT_RE = re.compile(
    r"(\d{1,2})\s*%\s*(?:di\s*)?sconto", re.I
)


def load_state():
    if not STATE_FILE.exists():
        return {"sent": []}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"sent": []}


def save_state(state):
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def parse_number(raw):
    raw = raw.strip().replace(" ", "")
    if "," in raw and "." in raw:
        if raw.rfind(",") > raw.rfind("."):
            raw = raw.replace(".", "").replace(",", ".")
        else:
            raw = raw.replace(",", "")
    elif "," in raw:
        raw = raw.replace(",", ".")
    return float(raw)


def extract_prices(text):
    prices = []
    for match in PRICE_RE.finditer(text):
        raw = next((g for g in match.groups() if g), None)
        if raw:
            try:
                value = parse_number(raw)
                if 10 <= value <= 5000 and value not in prices:
                    prices.append(value)
            except ValueError:
                pass
    return prices


def get_amazon_link(post):
    for a in post.select("a[href]"):
        href = a.get("href", "").strip()
        host = urlparse(href).netloc.lower()
        if (
            host == "amazon.it"
            or host.endswith(".amazon.it")
            or host == "amzn.to"
        ):
            return href
    return None


def send_telegram(message):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    response = requests.post(
        url,
        data={
            "chat_id": CHAT_ID,
            "text": message,
            "disable_web_page_preview": True,
        },
        timeout=20,
    )
    response.raise_for_status()
    result = response.json()
    if not result.get("ok"):
        raise RuntimeError("Telegram non ha accettato il messaggio")


def main():
    response = requests.get(SOURCE_URL, headers=HEADERS, timeout=30)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")

    state = load_state()
    sent = set(state.get("sent", []))
    found = 0

    posts = soup.select(".tgme_widget_message")
    for post in posts:
        text_node = post.select_one(".tgme_widget_message_text")
        if not text_node:
            continue

        text = text_node.get_text(" ", strip=True)
        if not ROBOT_RE.search(text):
            continue

        link = get_amazon_link(post)
        if not link:
            continue

        prices = extract_prices(text)
        if not prices:
            continue

        current_candidates = [p for p in prices if p <= MAX_PRICE]
        if not current_candidates:
            continue
        current_price = min(current_candidates)

        discount_match = DISCOUNT_RE.search(text)
        discount = int(discount_match.group(1)) if discount_match else 0

        exceptional = (
            current_price <= VERY_LOW_PRICE
            or discount >= MIN_DISCOUNT
        )
        if not exceptional:
            continue

        post_link_node = post.select_one("a.tgme_widget_message_date")
        source_link = (
            post_link_node.get("href")
            if post_link_node and post_link_node.get("href")
            else SOURCE_URL
        )

        fingerprint = hashlib.sha256(
            f"{text}|{link}".encode("utf-8")
        ).hexdigest()[:20]

        if fingerprint in sent:
            continue

        label = "POSSIBILE PREZZO ECCEZIONALE"
        if current_price <= 100:
            label = "ATTENZIONE: VERIFICA POSSIBILE ERRORE DI PREZZO"

        message = (
            f"🤖 {label}\n\n"
            f"{text[:900]}\n\n"
            f"Prezzo individuato: {current_price:.2f} €\n"
            f"Link Amazon: {link}\n"
            f"Fonte: {source_link}\n\n"
            "Controlla prezzo finale, modello e venditore prima di acquistare."
        )

        send_telegram(message)
        sent.add(fingerprint)
        found += 1

    state["sent"] = list(sent)[-1000:]
    save_state(state)
    print(f"Controllo completato. Nuove notifiche: {found}")


if __name__ == "__main__":
    main()

