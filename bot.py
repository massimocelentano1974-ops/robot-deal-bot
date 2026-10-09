```python
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
from bs4 import BeautifulSoup, NavigableString, Tag


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
# FILTRI
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
    r"(?:€\s*)?(\d{1,4}(?:[.,]\d{1,2})?)\s*€"
    r"|€\s*(\d{1,4}(?:[.,]\d{1,2})?)",
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
# INVIO TELEGRAM
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
        raise RuntimeError(result.get("description", "Errore Telegram"))


# =====================================================
# LINK AMAZON
# =====================================================

def normalize_amazon_link(url):
    url = html.unescape((url or "").strip())

    # Alcuni link sono racchiusi in redirect di Telegram.
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


def collect_link_nodes(text_element):
    """
    Estrae i link Amazon con il testo circostante.
    Conserva la posizione di ciascun link nel messaggio.
    """

    full_text = text_element.get_text(" ", strip=False)
    results = []

    for anchor in text_element.select("a[href]"):
        href = anchor.get("href", "")
        link = normalize_amazon_link(href)

        if not link:
            continue

        anchor_text = anchor.get_text(" ", strip=True)

        # Posizione approssimativa del link nel testo.
        # I messaggi Telegram possono contenere link con testo
        # visibile diverso dall'URL effettivo.
        visible = anchor.get_text(" ", strip=False)
        position = full_text.find(visible) if visible else -1

        results.append({
            "url": link,
            "text": anchor_text,
            "position": position,
        })

    return full_text, results


# =====================================================
# LETTURA CANALI
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

        text, links = collect_link_nodes(text_element)
        text = re.sub(r"\s+", " ", text).strip()

        if not text:
            continue

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
    # Formato più comune nei canali di offerte.
    if "📌" in text:
        chunks = re.split(r"📌", text)
        products = [
            chunk.strip()
            for chunk in chunks
            if chunk.strip()
        ]
        if len(products) > 1:
            return products

    # Formato numerato: 1) prodotto, 2) prodotto...
    numbered = re.split(
        r"(?<!\d)\s+\d{1,2}\s*[).]\s+",
        text,
    )

    products = [x.strip() for x in numbered if x.strip()]
    if len(products) > 1:
        return products

    return [text.strip()]


def assign_links(products, links):
    """
    Associa i link ai blocchi prodotto usando le posizioni
    nel testo originale quando disponibili.

    Se le posizioni non sono utilizzabili, non abbina
    arbitrariamente link a prodotti diversi.
    """

    if not products or not links:
        return []

    if len(products) == 1:
        # Un singolo blocco può contenere più link.
        # Preferisce il primo link Amazon; gli altri potrebbero
        # essere link accessori o ulteriori destinazioni.
        return [(products[0], links[0]["url"])]

    if len(products) == len(links):
        return [
            (product, link["url"])
            for product, link in zip(products, links)
        ]

    # Conteggi diversi: cerca di associare il link al blocco
    # prodotto che contiene il suo testo visibile.
    assigned = []
    used_links = set()

    for product in products:
        product_lower = product.lower()
        candidates = []

        for index, link in enumerate(links):
            if index in used_links:
                continue

            visible = (link.get("text") or "").strip().lower()

            # Se il testo visibile del link contiene informazioni
            # sul prodotto, è una corrispondenza utile.
            if visible and len(visible) > 5:
                if visible in product_lower:
                    candidates.append(index)

        if len(candidates) == 1:
            index = candidates[0]
            used_links.add(index)
            assigned.append((product, links[index]["url"]))

    # Non inventa associazioni per i prodotti rimasti senza link.
    return assigned


# =====================================================
# PREZZI E SCONTI
# =====================================================

def parse_number(value):
    value = value.strip().replace(" ", "")

    if "," in value and "." in value:
        if value.rfind(",") > value.rfind("."):
            value = value.replace(".", "").replace(",", ".")
        else:
            value = value.replace(",", "")
    else:
        value = value.replace(",", ".")

    try:
        return float(value)
    except ValueError:
        return None


def extract_prices(text):
    prices = []

    for match in PRICE_RE.finditer(text):
        raw = next(
            (group for group in match.groups() if group),
            None,
        )
        if raw is None:
            continue

        price = parse_number(raw)
        if price is not None and 1 <= price <= 10000:
            prices.append(price)

    return prices


def extract_discount(text, prices):
    match = DISCOUNT_RE.search(text)

    if match:
        raw = next(
            (group for group in match.groups() if group),
            None,
        )
        if raw:
            return int(raw)

    if len(prices) >= 2:
        old_price = max(prices)
        new_price = min(prices)

        if old_price > new_price:
            return round(
                (old_price - new_price) / old_price * 100
            )

    return None


def extract_current_price(text, prices):
    patterns = [
        r"(?:ora|oggi|adesso|solo|prezzo attuale)"
        r"\s*[:\-]?\s*(?:€\s*)?(\d{1,4}(?:[.,]\d{1,2})?)\s*€?",
        r"(?:💵|💰)\s*(?:€\s*)?(\d{1,4}(?:[.,]\d{1,2})?)\s*€?",
    ]

    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            price = parse_number(match.group(1))
            if price is not None and 1 <= price <= 10000:
                return price

    return min(prices) if prices else None


# =====================================================
# CONTROLLO PRODOTTI
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

    if current_price is None or current_price > MAX_PRICE:
        return None

    if discount is None or discount < MIN_DISCOUNT:
        return None

    name = re.sub(r"\s+", " ", product_text).strip()
    if len(name) > 450:
        name = name[:447] + "..."

    if not link:
        link = post_url

    if not link:
        return None

    return {
        "name": name,
        "price": current_price,
        "discount": discount,
        "link": link,
        "channel": channel,
    }


def make_key(channel, post_url, name, link):
    identity = link or name
    raw = f"{channel}|{post_url}|{identity}".lower()

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
                "@%s: impossibile associare i link del post %s",
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
```
