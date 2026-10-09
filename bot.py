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


# ==================================================
# CONFIGURAZIONE
# ==================================================

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


# ==================================================
# FILTRI PRODOTTI
# ==================================================

ROBOT_RE = re.compile(
    r"\b("
    r"robot|robovac|roborock|roomba|ecovacs|narwal|yeedi|"
    r"lubluelu|dreame|eufy|lefant|"
    r"xiaomi\s+robot|"
    r"deebot|"
    r"i[\s-]?robot|"
    r"mova\s+(?:p\d|v\d|z\d|e\d)|"
    r"tapo\s+rv\d|"
    r"switchbot\s+k\d"
    r")\b",
    re.IGNORECASE,
)

MANUAL_RE = re.compile(
    r"\b("
    r"scopa elettrica|"
    r"aspirapolvere a mano|"
    r"aspirapolvere portatile|"
    r"lavapavimenti manuale|"
    r"tineco floor one|"
    r"ricambio|"
    r"accessorio|"
    r"filtro di ricambio|"
    r"spazzola di ricambio"
    r")\b",
    re.IGNORECASE,
)

PRICE_RE = re.compile(
    r"(?:€\s*)?(\d{1,4}(?:[.,]\d{1,2})?)\s*€"
    r"|€\s*(\d{1,4}(?:[.,]\d{1,2})?)",
    re.IGNORECASE,
)

DISCOUNT_RE = re.compile(
    r"(?:sconto(?:\s+del)?\s*)"
    r"(\d{1,2})\s*%"
    r"|(\d{1,2})\s*%\s*(?:di\s*)?sconto",
    re.IGNORECASE,
)


# ==================================================
# STATO: EVITA NOTIFICHE DUPLICATE
# ==================================================

def load_seen():
    try:
        if STATE_FILE.exists():
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return set(data)
    except (OSError, json.JSONDecodeError) as exc:
        logging.warning("Impossibile leggere lo storico: %s", exc)

    return set()


def save_seen(seen):
    try:
        # Mantiene lo storico limitato per non far crescere
        # indefinitamente il file.
        recent = sorted(seen)[-5000:]
        STATE_FILE.write_text(
            json.dumps(recent, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError as exc:
        logging.error("Impossibile salvare lo storico: %s", exc)


# ==================================================
# TELEGRAM
# ==================================================

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
            "Telegram non ha accettato il messaggio: "
            + str(result.get("description", "errore sconosciuto"))
        )


# ==================================================
# LETTURA DEI CANALI PUBBLICI
# ==================================================

def get_channel_posts(channel):
    url = f"https://t.me/s/{channel}"

    response = session.get(url, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")
    posts = []

    for element in soup.select(".tgme_widget_message_wrap"):
        text_element = element.select_one(".tgme_widget_message_text")

        if not text_element:
            continue

        text = text_element.get_text(" ", strip=True)

        if not text:
            continue

        date_element = element.select_one(
            ".tgme_widget_message_date"
        )

        post_url = ""

        if date_element and date_element.get("href"):
            post_url = date_element["href"]

        if not post_url:
            data_post = element.select_one(
                "[data-post]"
            )
            if data_post:
                post_id = data_post.get("data-post", "")
                if "/" in post_id:
                    post_url = "https://t.me/" + post_id

        # Estrae i link presenti nel messaggio originale.
        links = []

        for anchor in text_element.select("a[href]"):
            href = anchor.get("href", "").strip()
            if href:
                normalized = normalize_amazon_link(href)
                if normalized:
                    links.append(normalized)

        # Elimina link ripetuti senza cambiare l'ordine.
        links = list(dict.fromkeys(links))

        posts.append({
            "text": text,
            "links": links,
            "url": post_url,
        })

    return posts[-POSTS_PER_CHANNEL:]


# ==================================================
# NORMALIZZAZIONE DEI LINK AMAZON
# ==================================================

def normalize_amazon_link(url):
    url = html.unescape(url.strip())

    # Telegram può racchiudere il link in un redirect.
    for _ in range(3):
        parsed = urlparse(url)
        query = parse_qs(parsed.query)

        wrapped = (
            query.get("url")
            or query.get("q")
            or query.get("u")
        )

        if wrapped:
            candidate = unquote(wrapped[0])
            if candidate.startswith(("http://", "https://")):
                url = candidate
                continue

        break

    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()

    if host == "amzn.to":
        return url

    if host == "amazon.it" or host.endswith(".amazon.it"):
        return url

    return None


# ==================================================
# SEPARAZIONE DEI PRODOTTI
# ==================================================

def split_products(text):
    # I canali di offerte spesso separano i prodotti con 📌.
    if "📌" in text:
        chunks = re.split(r"📌", text)
        products = [chunk.strip() for chunk in chunks if chunk.strip()]

        if len(products) > 1:
            return products

    # Formato alternativo: 1) prodotto, 2) prodotto...
    numbered = re.split(r"(?<!\d)\s+\d{1,2}\s*[).]\s+", text)

    products = [chunk.strip() for chunk in numbered if chunk.strip()]

    if len(products) > 1:
        return products

    return [text.strip()]


# ==================================================
# PREZZI E SCONTI
# ==================================================

def parse_number(value):
    value = value.strip().replace(" ", "")

    if "," in value and "." in value:
        # Esempio: 1.299,99
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
        raw = next((group for group in match.groups() if group), None)

        if raw is None:
            continue

        price = parse_number(raw)

        if price is not None and 1 <= price <= 10000:
            prices.append(price)

    return prices


def extract_discount(text, prices):
    match = DISCOUNT_RE.search(text)

    if match:
        raw = next((group for group in match.groups() if group), None)
        if raw:
            return int(raw)

    # Se non è scritto lo sconto, prova a calcolarlo dai prezzi.
    # Si usa il prezzo maggiore come prezzo precedente e il minore
    # come prezzo attuale, ma solo quando ci sono almeno due prezzi.
    if len(prices) >= 2:
        old_price = max(prices)
        new_price = min(prices)

        if old_price > new_price:
            return round((old_price - new_price) / old_price * 100)

    return None


def extract_current_price(text, prices):
    # Preferisce il prezzo vicino a parole come "ora", "oggi",
    # "prezzo" o "solo". Altrimenti usa il prezzo più basso
    # trovato, scelta prudente per i controlli sul budget.
    current_patterns = [
        r"(?:ora|oggi|adesso|solo|prezzo attuale)"
        r"\s*[:\-]?\s*(?:€\s*)?(\d{1,4}(?:[.,]\d{1,2})?)\s*€?",
        r"(?:💵|💰)\s*(?:€\s*)?(\d{1,4}(?:[.,]\d{1,2})?)\s*€?",
    ]

    for pattern in current_patterns:
        match = re.search(pattern, text, re.IGNORECASE)

        if match:
            price = parse_number(match.group(1))
            if price is not None and 1 <= price <= 10000:
                return price

    if prices:
        return min(prices)

    return None


# ==================================================
# FILTRO E COSTRUZIONE DELL'OFFERTA
# ==================================================

def is_robot_product(text):
    if not ROBOT_RE.search(text):
        return False

    if MANUAL_RE.search(text):
        return False

    return True


def product_key(channel, post_url, product_text, link):
    # L'identificatore del link rende stabile la deduplicazione.
    identity = link or product_text
    raw = f"{channel}|{post_url}|{identity}".lower()

    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def build_deal(channel, product_text, link, post_url):
    if not is_robot_product(product_text):
        return None

    prices = extract_prices(product_text)
    current_price = extract_current_price(product_text, prices)
    discount = extract_discount(product_text, prices)

    if current_price is None:
        logging.info(
            "Robot ignorato: prezzo non riconosciuto (%s)",
            channel,
        )
        return None

    if current_price > MAX_PRICE:
        return None

    if discount is None or discount < MIN_DISCOUNT:
        return None

    product_name = re.sub(r"\s+", " ", product_text).strip()

    # Accorcia il testo per mantenere le notifiche leggibili.
    if len(product_name) > 450:
        product_name = product_name[:447] + "..."

    product_link = link or post_url

    if not product_link:
        logging.info(
            "Robot ignorato: manca un link utilizzabile (%s)",
            channel,
        )
        return None

    return {
        "name": product_name,
        "price": current_price,
        "discount": discount,
        "link": product_link,
        "channel": channel,
        "post_url": post_url,
    }


# ==================================================
# ASSOCIAZIONE PRODOTTI E LINK
# ==================================================

def pair_products_and_links(products, links):
    """
    Se ogni prodotto ha un link, li abbina in ordine.

    Se i conteggi non coincidono, non associa alla cieca i link
    ai prodotti: restituisce soltanto le associazioni non ambigue.
    Questo evita di inviare un'offerta con il link sbagliato.
    """

    if not products or not links:
        return []

    if len(products) == len(links):
        return list(zip(products, links))

    logging.warning(
        "Conteggi diversi: %s prodotti e %s link. "
        "Il post sarà controllato senza abbinamenti rischiosi.",
        len(products),
        len(links),
    )

    # Se esiste un solo prodotto e un solo link, l'associazione è chiara.
    if len(products) == 1 and len(links) == 1:
        return [(products[0], links[0])]

    # In caso di dubbio, non inventare l'associazione.
    # I post con più prodotti e conteggi diversi vengono lasciati
    # senza abbinamento, invece di associare link errati.
    return []


# ==================================================
# CONTROLLO DEI CANALI
# ==================================================

def check_channel(channel, seen):
    logging.info("Controllo canale: @%s", channel)

    new_notifications = 0
    skipped = 0

    try:
        posts = get_channel_posts(channel)
    except (requests.RequestException, ValueError) as exc:
        logging.error(
            "Impossibile leggere @%s: %s",
            channel,
            exc,
        )
        return new_notifications, skipped

    for post in posts:
        text = post["text"]

        # Non serve analizzare messaggi che non contengono
        # neanche una parola riconducibile a un robot.
        if not ROBOT_RE.search(text):
            continue

        products = split_products(text)
        links = post["links"]

        pairs = pair_products_and_links(products, links)

        if not pairs:
            skipped += 1
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

            key = product_key(
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
                logging.error(
                    "Errore invio Telegram: %s",
                    exc,
                )
                # Non segnare come inviato: sarà ritentato
                # al prossimo controllo.
                continue

            seen.add(key)
            save_seen(seen)
            new_notifications += 1

            logging.info(
                "Notifica inviata da @%s",
                channel,
            )

            time.sleep(1)

    logging.info(
        "@%s: nuove notifiche %s, "
        "post con associazione ambigua %s",
        channel,
        new_notifications,
        skipped,
    )

    return new_notifications, skipped


# ==================================================
# AVVIO
# ==================================================

def main():
    seen = load_seen()

    total_notifications = 0
    total_skipped = 0

    for channel in SOURCE_CHANNELS:
        notifications, skipped = check_channel(channel, seen)

        total_notifications += notifications
        total_skipped += skipped

    logging.info(
        "Controllo completato. Nuove notifiche: %s",
        total_notifications,
    )

    logging.info(
        "Post con associazione ambigua: %s",
        total_skipped,
    )


if __name__ == "__main__":
    main()

