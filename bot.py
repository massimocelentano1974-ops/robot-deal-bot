import os
import re
import json
import hashlib
from pathlib import Path
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

# Segreti GitHub già configurati
BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]

# Quattro canali da controllare
SOURCE_CHANNELS = [
    channel.strip().lstrip("@")
    for channel in os.getenv(
        "SOURCE_CHANNELS",
        "offertedale,offertedalecasa,offervolt,scontiamolo",
    ).split(",")
    if channel.strip()
]

# Filtri delle offerte
MAX_PRICE = 350.0
MIN_DISCOUNT = 50

STATE_FILE = Path("state.json")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; RobotDealBot/1.0)"
}

# Riconosce robot e modelli comunemente venduti come robot.
# Il riconoscimento si basa sul testo e non garantisce
# che ogni modello sia effettivamente autonomo.
ROBOT_RE = re.compile(
    r"\b("
    r"robot|robovac|"
    r"roborock|roomba|"
    r"ecovacs|narwal|yeedi|"
    r"lubluelu|"
    r"dreame\s*(?:l\d|x\d|d\d|f\d|matrix|l\d{2})|"
    r"eufy\s*(?:clean|robovac|omni)|"
    r"lefant\s*(?:m\d|n\d|t\d)|"
    r"xiaomi\s*(?:robot|robot vacuum)"
    r")\b",
    re.IGNORECASE,
)

# Esclude alcuni aspirapolvere/lavapavimenti manuali noti.
# Evitiamo di escludere genericamente "senza fili",
# perché anche alcuni robot possono essere descritti così.
MANUAL_RE = re.compile(
    r"\b("
    r"tineco|floor\s*one|"
    r"scopa elettrica|"
    r"aspirapolvere a mano|"
    r"aspirapolvere portatile|"
    r"lavapavimenti manuale"
    r")\b",
    re.IGNORECASE,
)

PRICE_RE = re.compile(
    r"€\s*(\d{1,4}(?:[.,]\d{1,2})?)"
    r"|(\d{1,4}(?:[.,]\d{1,2})?)\s*€",
    re.IGNORECASE,
)

DISCOUNT_PATTERNS = [
    re.compile(
        r"(\d{1,3})\s*%\s*(?:di\s*)?sconto",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bsconto\s*(?:del|di)?\s*(\d{1,3})\s*%",
        re.IGNORECASE,
    ),
]


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


def extract_discount(text, prices):
    discount = 0

    for pattern in DISCOUNT_PATTERNS:
        match = pattern.search(text)

        if match:
            value = int(match.group(1))

            if 0 <= value <= 100:
                discount = value
                break

    # Calcola lo sconto anche se sono indicati due prezzi.
    # Usa il primo prezzo come prezzo corrente.
    if len(prices) >= 2 and prices[1] > prices[0]:
        calculated = round(
            (prices[1] - prices[0]) / prices[1] * 100
        )
        discount = max(discount, calculated)

    return discount


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
    # Formato con icone:
    # 📌 prodotto
    # 💵 prezzo
    # 🔗 link
    markers = list(re.finditer(r"📌", text))

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

    # Formato numerato: 1), 2), 3)...
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

    # Post non numerato con un solo robot riconoscibile.
    if ROBOT_RE.search(text):
        return [text]

    return []


def send_telegram(message):
    url = (
        f"https://api.telegram.org/bot"
        f"{BOT_TOKEN}/sendMessage"
    )

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


def check_channel(channel, sent):
    source_url = f"https://t.me/s/{channel}"

    print(f"Controllo canale: @{channel}")

    try:
        response = requests.get(
            source_url,
            headers=HEADERS,
            timeout=30,
        )
        response.raise_for_status()
    except requests.RequestException as error:
        print(f"Canale @{channel} non leggibile: {error}")
        return 0, 0

    soup = BeautifulSoup(response.text, "html.parser")

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

        # Per non abbinare un prodotto al link sbagliato,
        # richiediamo lo stesso numero di prodotti e link.
        if len(products) != len(links):
            skipped += 1
            continue

        date_link = post.select_one(
            "a.tgme_widget_message_date"
        )

        source_link = (
            date_link.get("href")
            if date_link and date_link.get("href")
            else source_url
        )

        for product_text, link in zip(products, links):
            if not ROBOT_RE.search(product_text):
                continue

            if MANUAL_RE.search(product_text):
                continue

            prices = extract_prices(product_text)

            if not prices:
                continue

            current_price = prices[0]

            if current_price > MAX_PRICE:
                continue

            discount = extract_discount(
                product_text,
                prices,
            )

            if discount < MIN_DISCOUNT:
                continue

            fingerprint = hashlib.sha256(
                f"{product_text}|{link}".encode("utf-8")
            ).hexdigest()[:20]

            if fingerprint in sent:
                continue

            message = (
                "🤖 POSSIBILE OFFERTA ROBOT AUTONOMO\n\n"
                f"{product_text[:900]}\n\n"
                f"Prezzo individuato: {current_price:.2f} €\n"
                f"Sconto individuato: {discount}%\n"
                f"Canale: @{channel}\n"
                f"Link Amazon: {link}\n"
                f"Fonte: {source_link}\n\n"
                "Verifica su Amazon il prezzo finale, "
                "il modello e il venditore prima di acquistare."
            )

            try:
                send_telegram(message)
            except (
                requests.RequestException,
                RuntimeError,
            ) as error:
                print(f"Errore invio Telegram: {error}")
                continue

            sent.add(fingerprint)
            found += 1

    print(
        f"@{channel}: nuove notifiche {found}, "
        f"post ignorati per link non associabili {skipped}"
    )

    return found, skipped


def main():
    state = load_state()
    sent = set(state.get("sent", []))

    total_found = 0
    total_skipped = 0

    for channel in SOURCE_CHANNELS:
        found, skipped = check_channel(channel, sent)
        total_found += found
        total_skipped += skipped

    state["sent"] = list(sent)[-1000:]
    save_state(state)

    print(
        f"Controllo completato. "
        f"Nuove notifiche: {total_found}"
    )
    print(
        "Post ignorati per link non associabili: "
        f"{total_skipped}"
    )


if __name__ == "__main__":
    main()
