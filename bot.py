
import os
import re
import json
import hashlib
from pathlib import Path
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

# Impostazioni Telegram: usa i segreti già salvati su GitHub
BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]

# Canale pubblico Telegram da controllare
SOURCE_CHANNEL = os.getenv("SOURCE_CHANNEL", "offertedale")
SOURCE_URL = f"https://t.me/s/{SOURCE_CHANNEL}"

# Regole delle offerte
MAX_PRICE = 300.0
MIN_DISCOUNT = 50

STATE_FILE = Path("state.json")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; RobotDealBot/1.0)"
}

# Parole e marchi associati ai robot autonomi
ROBOT_RE = re.compile(
    r"\b("
    r"robot aspirapolvere|"
    r"robot lavapavimenti|"
    r"robot aspirapolvere e lavapavimenti|"
    r"robot aspirapolvere lavapavimenti|"
    r"robot aspirapolvere e lava pavimenti|"
    r"robot vacuum|"
    r"robot mop|"
    r"robot vacuum cleaner|"
    r"robot lavapavimenti automatico|"
    r"roborock|roomba|ecovacs|narwal|yeedi|lubluelu"
    r")\b",
    re.IGNORECASE,
)

# Esclusioni per ridurre i falsi positivi dei prodotti manuali
MANUAL_RE = re.compile(
    r"\b("
    r"tineco|"
    r"floor\s*one|"
    r"wet\s*(?:&|and|-)?\s*dry|"
    r"aspirapolvere senza fili|"
    r"scopa elettrica|"
    r"aspirapolvere a mano|"
    r"aspirapolvere manuale"
    r")\b",
    re.IGNORECASE,
)

# Prezzi come 199 €, €199, 199,99 € oppure €199,99
PRICE_RE = re.compile(
    r"(?:€\s*)(\d{1,4}(?:[.,]\d{1,2})?)"
    r"|(\d{1,4}(?:[.,]\d{1,2})?)\s*€",
    re.IGNORECASE,
)

# Sconti espliciti, ad esempio "70% sconto"
DISCOUNT_RE = re.compile(
    r"(\d{1,3})\s*%\s*(?:di\s*)?sconto",
    re.IGNORECASE,
)


def load_state():
    if not STATE_FILE.exists():
        return {"sent": []}

    try:
        return json.loads(
            STATE_FILE.read_text(encoding="utf-8")
        )
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
        raw = next(
            (group for group in match.groups() if group),
            None,
        )

        if not raw:
            continue

        try:
            price = parse_number(raw)

            if 1 <= price <= 10000:
                prices.append(price)
        except ValueError:
            continue

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
    # Se il post contiene prodotti numerati, separali.
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

    # Nei post non numerati, considera il post come un prodotto.
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
        raise RuntimeError(
            "Telegram non ha accettato il messaggio"
        )


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
        text_node = post.select_one(
            ".tgme_widget_message_text"
        )

        if not text_node:
            continue

        text = text_node.get_text(" ", strip=True)
        products = split_products(text)
        links = get_amazon_links(post)

        if not products or not links:
            continue

        # Non associare link e prodotti se il numero non coincide.
        if len(products) != len(links):
            skipped += 1
            continue

        for product_text, link in zip(products, links):
            # Deve sembrare un robot autonomo.
            if not ROBOT_RE.search(product_text):
                continue

            # Escludi prodotti manuali riconoscibili.
            if MANUAL_RE.search(product_text):
                continue

            prices = extract_prices(product_text)

            if not prices:
                continue

            # Il primo prezzo è considerato quello in offerta.
            current_price = prices[0]

            # Il prezzo finale deve essere al massimo 300 €.
            if current_price > MAX_PRICE:
                continue

            # Leggi lo sconto dichiarato nel testo.
            discount_match = DISCOUNT_RE.search(product_text)

            if discount_match:
                discount = int(discount_match.group(1))
            else:
                discount = 0

            # Se ci sono due prezzi, calcola anche lo sconto.
            if len(prices) >= 2 and prices[1] > current_price:
                calculated_discount = int(
                    (prices[1] - current_price)
                    / prices[1]
                    * 100
                )

                discount = max(discount, calculated_discount)

            # Accetta tutti gli sconti dal 50% in su:
            # 50, 60, 70, 80, 90 e anche 100.
            if discount < MIN_DISCOUNT:
                continue

            # Evita di inviare di nuovo la stessa offerta.
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

            message = (
                "🤖 OFFERTA ROBOT AUTONOMO\n\n"
                f"{product_text[:900]}\n\n"
                f"Prezzo individuato: {current_price:.2f} €\n"
                f"Sconto individuato: {discount}%\n"
                f"Link Amazon: {link}\n"
                f"Fonte: {source_link}\n\n"
                "Controlla su Amazon il prezzo finale, "
                "il modello e il venditore prima di acquistare."
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
