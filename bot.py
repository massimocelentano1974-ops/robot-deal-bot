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
VERY_LOW_PRICE = 0.0
MIN_DISCOUNT = 50

SOURCE_URL = f"https://t.me/s/{SOURCE_CHANNEL}"
STATE_FILE = Path("state.json")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; RobotDealBot/1.0)"
}

ROBOT_RE = re.compile(
    r"\b(robot|aspirapolvere|aspira.?lava|lavapavimenti)\b",
    re.IGNORECASE,
)

PRICE_RE = re.compile(
    r"(?:€\s*)?(\d{1,4}(?:[.,]\d{1,2})?)\s*€"
    r"|€\s*(\d{1,4}(?:[.,]\d{1,2})?)",
    re.IGNORECASE,
)

DISCOUNT_RE = re.compile(
    r"(\d{1,2})\s*%\s*(?:di\s*)?sconto", re.IGNORECASE
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
        raw = next((group for group in match.groups() if group), None)
        if raw:
            try:
                price = parse_number(raw)
                if 1 <= price <= 10000:
                    prices.append(price)
            except ValueError:
                pass

    return prices


def get_amazon_links(post):
    links = []

    for anchor in post.select("a[href]"):
        href = anchor.get("href", "").strip()
        host = urlparse(href).netloc.lower()

        if (
            host == "amazon.it"
            or host.endswith(".amazon.it")
            or host == "amzn.to"
        ):
            if href not in links:
                links.append(href)

    return links


def split_products(text):
    # Divide i post numerati: 1), 2), 3)...
    markers = list(
        re.finditer(r"(?m)(?:^|\s)(\d{1,2})\)\s*", text)
    )

    if markers:
        products = []

        for index, marker in enumerate(markers):
            start = marker.end()
            end = (
                markers[index + 1].start()
                if index + 1 < len(markers)
                else len(text)
            )

            block = text[start:end].strip()

            if block:
                products.append(block)

        return products

    # Nei post non numerati, accetta solo un prodotto
    # se il post contiene un unico robot riconoscibile.
    if ROBOT_RE.search(text):
        return [text]

    return []


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

    if not response.json().get("ok"):
        raise RuntimeError("Telegram non ha accettato il messaggio")


def main():
    response = requests.get(
        SOURCE_URL,
        headers=HEADERS,
        timeout=30,
    )
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")

    state = load_state()
    sent = set(state.get("sent", []))
    found = 0
    skipped = 0

    for post in soup.select(".tgme_widget_message"):
        text_node = post.select_one(".tgme_widget_message_text")

        if not text_node:
            continue

        text = text_node.get_text(" ", strip=True)
        products = split_products(text)
        links = get_amazon_links(post)

        if not products or not links:
            continue

        # Se non possiamo associare con sicurezza un link
        # a ciascun prodotto, ignoriamo il post.
        if len(products) != len(links):
            skipped += 1
            continue

        for product_text, link in zip(products, links):
            if not ROBOT_RE.search(product_text):
                continue

            prices = extract_prices(product_text)

            # Usiamo il primo prezzo, non il minimo:
            # spesso è il prezzo in offerta, seguito dal vecchio.
            if not prices:
                continue

            current_price = prices[0]

            # Il prezzo del robot deve rispettare il tuo limite.
            if current_price > MAX_PRICE:
                continue

            discount_match = DISCOUNT_RE.search(product_text)
            discount = (
                int(discount_match.group(1))
                if discount_match
                else 0
            )

            # Se è presente anche il vecchio prezzo,
            # calcoliamo lo sconto quando possibile.
            if len(prices) >= 2 and prices[1] > current_price:
                calculated_discount = round(
                    (prices[1] - current_price) / prices[1] * 100
                )
                discount = max(discount, calculated_discount)

            if (
                current_price > VERY_LOW_PRICE
                and discount < MIN_DISCOUNT
            ):
                continue

            fingerprint = hashlib.sha256(
                f"{product_text}|{link}".encode("utf-8")
            ).hexdigest()[:20]

            if fingerprint in sent:
                continue

            date_link = post.select_one(
                "a.tgme_widget_message_date"
            )
            source_link = (
                date_link.get("href")
                if date_link and date_link.get("href")
                else SOURCE_URL
            )

            label = "POSSIBILE OFFERTA ECCEZIONALE"

            if current_price <= 100:
                label = "PREZZO MOLTO BASSO: VERIFICA"

            message = (
                f"🤖 {label}\n\n"
                f"{product_text[:900]}\n\n"
                f"Prezzo individuato: {current_price:.2f} €\n"
                f"Link Amazon: {link}\n"
                f"Fonte: {source_link}\n\n"
                "Verifica modello, prezzo finale e venditore "
                "prima di acquistare."
            )

            send_telegram(message)
            sent.add(fingerprint)
            found += 1

    state["sent"] = list(sent)[-1000:]
    save_state(state)

    print(f"Controllo completato. Nuove notifiche: {found}")
    print(f"Post ignorati per link non associabili: {skipped}")


if __name__ == "__main__":
    main()
