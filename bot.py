import os
import re
import json
import html
import logging
import hashlib
from pathlib import Path
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode

import requests
from bs4 import BeautifulSoup, NavigableString, Tag


# ==================================================
# CONFIGURAZIONE
# ==================================================

CHANNELS = [
    "offertedale",
    "offertedalecasa",
    "offervolt",
    "scontiamolo",
]

MAX_PRICE = 350.0
MIN_DISCOUNT = 50
POSTS_PER_CHANNEL = 30
REQUEST_TIMEOUT = 25

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()

STATE_FILE = Path("sent_deals.json")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)

session = requests.Session()
session.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    )
})


# ==================================================
# RICONOSCIMENTO DEI ROBOT
# ==================================================

ROBOT_RE = re.compile(
    r"\b("
    r"robot\s+aspirapolvere|"
    r"robot\s+lavapavimenti|"
    r"robot\s+aspira(?:polvere|polveri)|"
    r"aspirapolvere\s+robot|"
    r"aspira(?:polvere|polveri)\s+robot|"
    r"robot\s+lava(?:pavimenti|pavimento)|"
    r"roomba|"
    r"roborock|"
    r"deebot|"
    r"yeedi|"
    r"lefant|"
    r"dreame\s+(?:l\d|x\d|d\d|matrix|bot\b)|"
    r"ecovacs\s+.{0,35}\b(?:robot|omni|t\d)\b|"
    r"eufy\s+.{0,35}\b(?:robot|omni|x\d)\b|"
    r"xiaomi\s+.{0,35}\b(?:robot|vacuum\s+s\d|s\d{2})\b"
    r")\b",
    re.IGNORECASE,
)

MANUAL_VACUUM_RE = re.compile(
    r"\b("
    r"aspirapolvere\s+portatile|"
    r"mini\s+aspirapolvere|"
    r"aspirabriciole|"
    r"scopa\s+elettrica|"
    r"wet\s*(?:and|&)\s*dry\s+vacuum"
    r")\b",
    re.IGNORECASE,
)


# ==================================================
# LETTURA E SALVATAGGIO DEI DUPLICATI
# ==================================================

def load_sent_deals():
    try:
        if STATE_FILE.exists():
            with STATE_FILE.open("r", encoding="utf-8") as file:
                data = json.load(file)

            if isinstance(data, list):
                return set(data)

            if isinstance(data, dict):
                return set(data.get("sent", []))

    except (OSError, json.JSONDecodeError) as error:
        logging.warning("Impossibile leggere lo storico: %s", error)

    return set()


def save_sent_deals(sent_deals):
    try:
        # Limita la dimensione dello storico.
        recent = list(sent_deals)[-5000:]

        with STATE_FILE.open("w", encoding="utf-8") as file:
            json.dump(recent, file, ensure_ascii=False, indent=2)

    except OSError as error:
        logging.error("Impossibile salvare lo storico: %s", error)


def deal_key(link, title):
    parsed = urlparse(link)

    match = re.search(
        r"/(?:dp|gp/product)/([A-Z0-9]{10})",
        parsed.path,
        re.IGNORECASE,
    )

    if match:
        return "asin:" + match.group(1).upper()

    normalized_title = re.sub(
        r"\s+", " ", title.lower()
    ).strip()

    raw = normalized_title or link
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# ==================================================
# LINK AMAZON
# ==================================================

def normalize_amazon_link(url):
    if not url:
        return None

    url = html.unescape(url.strip())

    if url.startswith("//"):
        url = "https:" + url

    parsed = urlparse(url)

    if parsed.scheme not in ("http", "https"):
        return None

    host = (parsed.hostname or "").lower()

    amazon_domains = (
        "amazon.it",
        "www.amazon.it",
        "amzn.to",
        "www.amzn.to",
    )

    if not any(
        host == domain or host.endswith("." + domain)
        for domain in amazon_domains
    ):
        return None

    # Elimina i frammenti, conservando eventuali tag affiliato.
    parsed = parsed._replace(fragment="")
    return urlunparse(parsed)


# ==================================================
# LETTURA DEI POST TELEGRAM
# ==================================================

def get_channel_posts(channel):
    url = f"https://t.me/s/{channel}"

    response = session.get(
        url,
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")
    posts = []

    for element in soup.select(".tgme_widget_message_wrap"):
        text_element = element.select_one(
            ".tgme_widget_message_text"
        )

        if not text_element:
            continue

        links = []

        def extract_text_with_links(node):
            parts = []

            for child in node.children:
                if isinstance(child, NavigableString):
                    parts.append(str(child))

                elif isinstance(child, Tag):
                    if child.name == "a":
                        link = normalize_amazon_link(
                            child.get("href", "")
                        )

                        if link:
                            if link not in links:
                                links.append(link)

                            index = links.index(link)
                            parts.append(f" [[LINK_{index}]] ")

                        else:
                            parts.append(
                                child.get_text(" ", strip=False)
                            )
                    else:
                        parts.append(
                            extract_text_with_links(child)
                        )

            return "".join(parts)

        text = extract_text_with_links(text_element)
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


# ==================================================
# DIVISIONE DEI POST IN SINGOLI PRODOTTI
# ==================================================

def split_products(text):
    # Individua prodotti numerati: 1), 2), 3)...
    pattern = re.compile(
        r"(?<!\S)\s*\d{1,2}\s*\)\s*"
    )

    matches = list(pattern.finditer(text))

    if matches:
        products = []

        for index, match in enumerate(matches):
            start = match.start()
            end = (
                matches[index + 1].start()
                if index + 1 < len(matches)
                else len(text)
            )

            product = text[start:end].strip()

            if product:
                products.append(product)

        return products

    # Alcuni canali separano le offerte con una puntina.
    if "📌" in text:
        products = [
            part.strip()
            for part in text.split("📌")
            if part.strip()
        ]

        if products:
            return products

    return [text.strip()] if text.strip() else []


def assign_link(product, links):
    # Il link viene scelto usando il riferimento inserito
    # nel testo HTML, non la posizione generica nella lista.
    match = re.search(r"\[\[LINK_(\d+)\]\]", product)

    if match:
        index = int(match.group(1))

        if 0 <= index < len(links):
            return links[index]

    # Fallback prudente: un solo link per un solo prodotto.
    if len(links) == 1:
        return links[0]

    # Non indovinare se ci sono più link non associabili.
    return None


# ==================================================
# PREZZI E SCONTI
# ==================================================

PRICE_RE = re.compile(
    r"(?<!\w)("
    r"\d{1,3}(?:\.\d{3})+(?:,\d{1,2})?"
    r"|\d+(?:,\d{1,2})?"
    r")\s*€"
    r"|€\s*("
    r"\d{1,3}(?:\.\d{3})+(?:,\d{1,2})?"
    r"|\d+(?:,\d{1,2})?"
    r")",
    re.IGNORECASE,
)


def parse_number(value):
    if not value:
        return None

    try:
        return float(
            value.replace(".", "").replace(",", ".")
        )
    except (ValueError, AttributeError):
        return None


def extract_prices(text):
    prices = []

    for match in PRICE_RE.finditer(text):
        value = match.group(1) or match.group(2)
        price = parse_number(value)

        if price is not None:
            prices.append(price)

    return prices


def extract_discount(text):
    prices = extract_prices(text)

    if len(prices) >= 2:
        current_price = prices[0]
        old_price = prices[1]

        if old_price > 0 and old_price > current_price:
            discount = round(
                (old_price - current_price) / old_price * 100
            )

            return current_price, old_price, discount

    # Fallback per i post che dichiarano lo sconto in percentuale.
    percent_match = re.search(
        r"(?:sconto|risparmio|offerta)\s*[:\-]?\s*"
        r"(\d{1,2})\s*%",
        text,
        re.IGNORECASE,
    )

    if percent_match and prices:
        return prices[0], None, int(percent_match.group(1))

    return None, None, None


# ==================================================
# PREPARAZIONE DELLE OFFERTE
# ==================================================

def clean_product_text(text):
    text = re.sub(r"\[\[LINK_\d+\]\]", " ", text)
    text = re.sub(r"^\s*\d{1,2}\s*\)\s*", "", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip(" \t\n-–—|")


def is_robot_product(title):
    if MANUAL_VACUUM_RE.search(title):
        return False

    return bool(ROBOT_RE.search(title))


def build_deal(product, links, channel, post_url):
    link = assign_link(product, links)

    if not link:
        logging.warning(
            "@%s: link non associabile al prodotto: %s",
            channel,
            clean_product_text(product)[:100],
        )
        return None

    title = clean_product_text(product)

    if not title or not is_robot_product(title):
        return None

    current_price, old_price, discount = extract_discount(product)

    if current_price is None:
        logging.info(
            "@%s: prezzo non riconosciuto: %s",
            channel,
            title[:100],
        )
        return None

    if current_price <= 0 or current_price > MAX_PRICE:
        logging.info(
            "@%s: prezzo fuori limite (%.2f €): %s",
            channel,
            current_price,
            title[:100],
        )
        return None

    if discount is None or discount < MIN_DISCOUNT:
        logging.info(
            "@%s: sconto inferiore al %s%% o non verificabile: %s",
            channel,
            MIN_DISCOUNT,
            title[:100],
        )
        return None

    return {
        "title": title,
        "link": link,
        "price": current_price,
        "old_price": old_price,
        "discount": discount,
        "channel": channel,
        "post_url": post_url,
    }


# ==================================================
# INVIO TELEGRAM
# ==================================================

def send_telegram_message(deal):
    if not BOT_TOKEN or not CHAT_ID:
        logging.error(
            "Mancano i segreti BOT_TOKEN o CHAT_ID su GitHub."
        )
        return False

    title = html.escape(deal["title"])
    link = html.escape(deal["link"], quote=True)
    channel = html.escape(deal["channel"])

    price_text = f'{deal["price"]:.2f} €'.replace(".", ",")

    if deal["old_price"] is not None:
        old_price_text = (
            f'{deal["old_price"]:.2f} €'.replace(".", ",")
        )
        price_line = (
            f"💶 Prezzo: <b>{price_text}</b>\n"
            f"🏷️ Prezzo precedente: {old_price_text}\n"
        )
    else:
        price_line = f"💶 Prezzo: <b>{price_text}</b>\n"

    message = (
        "🤖 <b>OFFERTA ROBOT ASPIRAPOLVERE</b>\n\n"
        f"📦 {title}\n\n"
        f"{price_line}"
        f"🔥 Sconto: <b>{deal['discount']}%</b>\n"
        f"📢 Canale: @{channel}\n\n"
        f'🛒 <a href="{link}">VEDI OFFERTA AMAZON</a>'
    )

    if deal["post_url"]:
        post_url = html.escape(
            deal["post_url"],
            quote=True,
        )
        message += (
            f'\n\n🔎 <a href="{post_url}">'
            "Post originale</a>"
        )

    api_url = (
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    )

    try:
        response = session.post(
            api_url,
            data={
                "chat_id": CHAT_ID,
                "text": message,
                "parse_mode": "HTML",
                "disable_web_page_preview": False,
            },
            timeout=REQUEST_TIMEOUT,
        )

        response.raise_for_status()
        result = response.json()

        if not result.get("ok"):
            logging.error(
                "Telegram ha rifiutato il messaggio: %s",
                result,
            )
            return False

        return True

    except (requests.RequestException, ValueError) as error:
        logging.error(
            "Errore durante l'invio Telegram: %s",
            error,
        )
        return False


# ==================================================
# CONTROLLO DEI CANALI
# ==================================================

def main():
    sent_deals = load_sent_deals()
    new_count = 0
    ambiguous_count = 0

    logging.info("Avvio controllo offerte.")
    logging.info(
        "Prezzo massimo: %.2f € | Sconto minimo: %s%%",
        MAX_PRICE,
        MIN_DISCOUNT,
    )

    for channel in CHANNELS:
        logging.info("Controllo canale: @%s", channel)

        try:
            posts = get_channel_posts(channel)

        except requests.RequestException as error:
            logging.error(
                "Errore nella lettura di @%s: %s",
                channel,
                error,
            )
            continue

        channel_new = 0

        for post in posts:
            products = split_products(post["text"])

            for product in products:
                # Ignora il testo introduttivo senza offerte.
                if not is_robot_product(clean_product_text(product)):
                    continue

                deal = build_deal(
                    product=product,
                    links=post["links"],
                    channel=channel,
                    post_url=post["url"],
                )

                if not deal:
                    # Un prodotto fuori dai limiti viene ignorato.
                    # Un link non associabile viene già registrato
                    # nel log da build_deal.
                    if (
                        is_robot_product(clean_product_text(product))
                        and not assign_link(product, post["links"])
                    ):
                        ambiguous_count += 1
                    continue

                key = deal_key(deal["link"], deal["title"])

                if key in sent_deals:
                    logging.info(
                        "Duplicato ignorato: %s",
                        deal["title"][:100],
                    )
                    continue

                if send_telegram_message(deal):
                    sent_deals.add(key)
                    new_count += 1
                    channel_new += 1

                    # Salva subito dopo ogni invio riuscito.
                    save_sent_deals(sent_deals)

                    logging.info(
                        "Notifica inviata: %s",
                        deal["title"][:100],
                    )

        logging.info(
            "@%s: nuove notifiche %s",
            channel,
            channel_new,
        )

    save_sent_deals(sent_deals)

    logging.info(
        "Controllo completato. Nuove notifiche: %s",
        new_count,
    )
    logging.info(
        "Prodotti con link non associabile: %s",
        ambiguous_count,
    )


if __name__ == "__main__":
    main()
