import os
import re
import json
import time
import html
import hashlib
import logging
from pathlib import Path
from urllib.parse import urlparse, parse_qs, unquote

import requests
from bs4 import BeautifulSoup


# =====================================================
# CONFIGURAZIONE
# =====================================================

BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]

SOURCE_CHANNELS = [
    channel.strip().lstrip("@")
    for channel in os.getenv(
        "SOURCE_CHANNELS",
        "offertedale,offertedalecasa,offervolt,scontiamolo",
    ).split(",")
    if channel.strip()
]

MAX_PRICE = 350.0
MIN_DISCOUNT = 50
POSTS_PER_CHANNEL = 25
REQUEST_TIMEOUT = 20

STATE_FILE = Path("sent_deals.json")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)

session = requests.Session()
session.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 Chrome/130.0 Safari/537.36"
    )
})


# =====================================================
# FILTRI PRODOTTI
# =====================================================

ROBOT_RE = re.compile(
    r"\b("
    r"robot|robovac|roborock|roomba|ecovacs|narwal|yeedi|"
    r"lubluelu|dreame|eufy|lefant|deebot|"
    r"xiaomi\s+robot|"
    r"mova\s+(?:p\d|v\d|z\d|e\d)|"
    r"tapo\s+rv\d|switchbot\s+k\d"
    r")\b",
    re.IGNORECASE,
)

MANUAL_RE = re.compile(
    r"\b("
    r"scopa elettrica|aspirapolvere a mano|"
    r"aspirapolvere portatile|lavapavimenti manuale|"
    r"tineco floor one|ricambio|accessorio|"
    r"filtro di ricambio|spazzola di ricambio"
    r")\b",
    re.IGNORECASE,
)

PRICE_RE = re.compile(
    r"(\d{1,3}(?:\.\d{3})*(?:,\d{1,2})?)\s*€"
    r"|€\s*(\d{1,3}(?:\.\d{3})*(?:,\d{1,2})?)",
    re.IGNORECASE,
)

DISCOUNT_RE = re.compile(
    r"(?:sconto(?:\s+del)?\s*)(\d{1,2})\s*%"
    r"|(\d{1,2})\s*%\s*(?:di\s*)?sconto",
    re.IGNORECASE,
)


# =====================================================
# STORICO NOTIFICHE
# =====================================================

def load_seen():
    try:
        if STATE_FILE.exists():
            data = json.loads(
                STATE_FILE.read_text(encoding="utf-8")
            )
            if isinstance(data, list):
                return set(data)
    except (OSError, json.JSONDecodeError) as exc:
        logging.warning("Errore lettura storico: %s", exc)

    return set()


def save_seen(seen):
    try:
        STATE_FILE.write_text(
            json.dumps(
                sorted(seen)[-5000:],
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    except OSError as exc:
        logging.error("Errore salvataggio storico: %s", exc)


# =====================================================
# TELEGRAM
# =====================================================

def send_telegram(message):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

    response = session.post(
        url,
        data={
            "chat_id": CHAT_ID,
            "text": message,
            "disable_web_page_preview": False,
        },
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()

    result = response.json()
    if not result.get("ok"):
        raise RuntimeError(
            result.get("description", "Errore Telegram")
        )


# =====================================================
# LINK AMAZON
# =====================================================

def normalize_amazon_link(url):
    url = html.unescape((url or "").strip())

    for _ in range(3):
        parsed = urlparse(url)
        query = parse_qs(parsed.query)

        wrapped = (
            query.get("url")
            or query.get("q")
            or query.get("u")
        )

        if not wrapped:
            break

        candidate = unquote(wrapped[0])
        if not candidate.startswith(("http://", "https://")):
            break

        url = candidate

    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()

    if host == "amzn.to":
        return url

    if host == "amazon.it" or host.endswith(".amazon.it"):
        return url

    return None


# =====================================================
# LETTURA DEI CANALI
# =====================================================

def get_channel_posts(channel):
    url = f"https://t.me/s/{channel}"
    response = session.get(url, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")
    posts = []

    for element in soup.select(".tgme_widget_message_wrap"):
        text_element = element.select_one(
            ".tgme_widget_message_text"
        )

        if not text_element:
            continue

        text = text_element.get_text(" ", strip=True)
        text = re.sub(r"\s+", " ", text).strip()

        if not text:
            continue

        links = []

        for anchor in text_element.select("a[href]"):
            link = normalize_amazon_link(
                anchor.get("href", "")
            )
            if link and link not in links:
                links.append(link)

        date_element = element.select_one(
            ".tgme_widget_message_date"
        )
        post_url = (
            date_element.get("href", "")
            if date_element else ""
        )

        if not post_url:
            data_post = element.select_one("[data-post]")
            if data_post:
                post_id = data_post.get("data-post", "")
                if "/" in post_id:
                    post_url = "https://t.me/" + post_id

        posts.append({
            "text": text,
            "links": links,
            "url": post_url,
        })

    return posts[-POSTS_PER_CHANNEL:]


# =====================================================
# DIVISIONE DEI PRODOTTI
# =====================================================

def split_products(text):
    # Formato numerato: 1) prodotto, 2) prodotto...
    numbered = re.split(
        r"(?:^|\s)\**\s*\d{1,2}\s*\)\s*\**\s*",
        text,
    )

    products = [
        chunk.strip()
        for chunk in numbered
        if chunk.strip()
    ]

    if len(products) > 1:
        return products

    # Formato con separatore 📌.
    if "📌" in text:
        chunks = re.split(r"📌", text)
        products = [
            chunk.strip()
            for chunk in chunks
            if chunk.strip()
        ]

        if len(products) > 1:
            return products

    return [text.strip()]


# =====================================================
# ASSOCIAZIONE LINK E PRODOTTI
# =====================================================

def assign_links(products, links):
    if not products or not links:
        return []

    # Un solo prodotto nel post: usa il primo link Amazon.
    if len(products) == 1:
        return [(products[0], links[0])]

    # Stesso numero di prodotti e link: associa in ordine.
    if len(products) == len(links):
        return list(zip(products, links))

    # Conteggi diversi: prova ad associare i link usando
    # il codice ASIN Amazon, quando presente nel testo.
    assigned = []
    used_links = set()

    def asin(value):
        match = re.search(
            r"/dp/([A-Z0-9]{10})|/gp/product/([A-Z0-9]{10})",
            value,
            re.IGNORECASE,
        )
        if not match:
            return None
        return (match.group(1) or match.group(2)).upper()

    for product in products:
        product_asins = {
            code.upper()
            for code in re.findall(
                r"\b[A-Z0-9]{10}\b",
                product,
                re.IGNORECASE,
            )
        }

        matches = []

        for index, link in enumerate(links):
            if index in used_links:
                continue

            link_asin = asin(link)

            if link_asin and link_asin in product_asins:
                matches.append(index)

        if len(matches) == 1:
            index = matches[0]
            used_links.add(index)
            assigned.append((product, links[index]))

    return assigned


# =====================================================
# PREZZI E SCONTI
# =====================================================

def parse_number(value):
    value = value.strip().replace(".", "").replace(",", ".")

    try:
        return float(value)
    except ValueError:
        return None


def extract_prices(text):
    prices = []

    for match in PRICE_RE.finditer(text):
        raw = match.group(1) or match.group(2)

        if not raw:
            continue

        price = parse_number(raw)

        if price is not None and 1 <= price <= 10000:
            prices.append(price)

    return prices


def extract_discount(text, prices):
    match = DISCOUNT_RE.search(text)

    if match:
        raw = match.group(1) or match.group(2)
        return int(raw)

    # Calcola lo sconto usando i due prezzi quando disponibili.
    if len(prices) >= 2:
        current = prices[0]
        old = prices[1]

        if old > current:
            return round((old - current) / old * 100)

    return None


def extract_current_price(text, prices):
    # I post delle offerte spesso indicano:
    # 399,00€ invece di 699,00€
    match = re.search(
        r"(\d{1,3}(?:\.\d{3})*(?:,\d{1,2})?)\s*€"
        r"\s*(?:invece di|anziché|anziche|prima era)",
        text,
        re.IGNORECASE,
    )

    if match:
        price = parse_number(match.group(1))
        if price is not None:
            return price

    # Altrimenti usa il primo prezzo trovato.
    if prices:
        return prices[0]

    return None


# =====================================================
# FILTRO E CREAZIONE OFFERTA
# =====================================================

def is_robot_product(text):
    if not ROBOT_RE.search(text):
        return False

    if MANUAL_RE.search(text):
        return False

    return True


def build_deal(channel, product_text, link, post_url):
    if not is_robot_product(product_text):
        return None

    prices = extract_prices(product_text)
    current_price = extract_current_price(product_text, prices)
    discount = extract_discount(product_text, prices)

    if current_price is None:
        return None

    if current_price > MAX_PRICE:
        return None

    if discount is None or discount < MIN_DISCOUNT:
        return None

    product_name = re.sub(r"\s+", " ", product_text).strip()

    if len(product_name) > 450:
        product_name = product_name[:447] + "..."

    if not link:
        link = post_url

    if not link:
        return None

    return {
        "name": product_name,
        "price": current_price,
        "discount": discount,
        "link": link,
        "channel": channel,
    }


def make_key(channel, post_url, name, link):
    raw = f"{channel}|{post_url}|{link or name}".lower()

    return hashlib.sha256(
        raw.encode("utf-8")
    ).hexdigest()


# =====================================================
# CONTROLLO CANALI
# =====================================================

def check_channel(channel, seen):
    logging.info("Controllo canale: @%s", channel)

    notifications = 0
    ambiguous = 0

    try:
        posts = get_channel_posts(channel)
    except requests.RequestException as exc:
        logging.error("Errore lettura @%s: %s", channel, exc)
        return notifications, ambiguous

    for post in posts:
        text = post["text"]

        if not ROBOT_RE.search(text):
            continue

        products = split_products(text)
        links = post["links"]
        pairs = assign_links(products, links)

        if not pairs:
            ambiguous += 1
            logging.warning(
                "@%s: link non associabili nel post %s",
                channel,
                post["url"] or "(URL non disponibile)",
            )
            continue

        for product_text, link in pairs:
            deal = build_deal(
                channel,
                product_text,
                link,
                post["url"],
            )

            if not deal:
                continue

            key = make_key(
                channel,
                post["url"],
                deal["name"],
                deal["link"],
            )

            if key in seen:
                continue

            message = (
                "🤖 OFFERTA ROBOT\n\n"
                f"{deal['name']}\n\n"
                f"💶 Prezzo: {deal['price']:.2f} €\n"
                f"🔥 Sconto: {deal['discount']}%\n"
                f"📢 Canale: @{deal['channel']}\n\n"
                f"🛒 Acquista: {deal['link']}"
            )

            try:
                send_telegram(message)
            except (requests.RequestException, RuntimeError) as exc:
                logging.error("Errore invio Telegram: %s", exc)
                continue

            seen.add(key)
            save_seen(seen)
            notifications += 1

            logging.info(
                "Notifica inviata da @%s",
                channel,
            )

            time.sleep(1)

    logging.info(
        "@%s: nuove notifiche %s, post ambigui %s",
        channel,
        notifications,
        ambiguous,
    )

    return notifications, ambiguous


# =====================================================
# AVVIO
# =====================================================

def main():
    seen = load_seen()

    total_notifications = 0
    total_ambiguous = 0

    for channel in SOURCE_CHANNELS:
        notifications, ambiguous = check_channel(channel, seen)

        total_notifications += notifications
        total_ambiguous += ambiguous

    logging.info(
        "Controllo completato. Nuove notifiche: %s",
        total_notifications,
    )

    logging.info(
        "Post con associazione ambigua: %s",
        total_ambiguous,
    )


if __name__ == "__main__":
    main()
