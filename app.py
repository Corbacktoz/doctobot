import os, asyncio, re, argparse
from datetime import datetime, timedelta
import pytz
import requests
from dateutil import parser as dp
from playwright.async_api import async_playwright

# --------- Config ----------
DOCTOLIB_URL = "https://www.doctolib.fr/dermatologue/toulouse?availabilities=1"
TZ = pytz.timezone("Europe/Paris")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
# Env var pour la fenêtre par défaut (jours)
DEFAULT_WINDOW_DAYS = int(os.getenv("WINDOW_DAYS", "14"))

DATE_PATTERNS = [
    r"Prochain(?:\s+RDV| rendez-vous)?\s*(?:le)?\s*([0-9]{1,2}\s+\w+\s+[0-9]{4})",
    r"Prochain(?:\s+RDV| rendez-vous)?\s*(?:le)?\s*([0-9]{1,2}\s+\w+)",
    r"Disponibilit[ée]s?\s*(?:le)?\s*([0-9]{1,2}\s+\w+(?:\s+[0-9]{4})?)",
]
MONTHS_FR = {
    "janvier":"January","février":"February","fevrier":"February","mars":"March","avril":"April",
    "mai":"May","juin":"June","juillet":"July","août":"August","aout":"August",
    "septembre":"September","octobre":"October","novembre":"November","décembre":"December","decembre":"December"
}
# ---------------------------

def fr_to_en_date(s: str) -> str:
    t = s.lower()
    for fr,en in MONTHS_FR.items():
        t = re.sub(rf"\b{fr}\b", en, t)
    return t

def parse_date_fr(text: str):
    for pat in DATE_PATTERNS:
        m = re.search(pat, text, flags=re.IGNORECASE)
        if not m:
            continue
        raw = m.group(1).strip()
        try:
            en = fr_to_en_date(raw)
            dt = dp.parse(en, dayfirst=True, default=TZ.localize(datetime.now()).replace(month=1, day=1))
            if dt.tzinfo is None:
                dt = TZ.localize(dt)
            return dt
        except Exception:
            continue
    # cas "aujourd'hui"/"demain"
    if re.search(r"\baujourd'hui\b", text, re.I):
        return TZ.localize(datetime.now())
    if re.search(r"\bdemain\b", text, re.I):
        return TZ.localize(datetime.now() + timedelta(days=1))
    return None

async def fetch_derm(window_days: int):
    out = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(locale="fr-FR", timezone_id="Europe/Paris")
        page = await context.new_page()
        await page.goto(DOCTOLIB_URL, wait_until="domcontentloaded")
        # cookies
        try:
            await page.get_by_role("button", name=re.compile("Accepter|Tout accepter|J'accepte", re.I)).click(timeout=3000)
        except Exception:
            pass
        # scroll (lazy load)
        last_h = 0
        for _ in range(8):
            await page.mouse.wheel(0, 2000)
            await page.wait_for_timeout(600)
            h = await page.evaluate("() => document.body.scrollHeight")
            if h == last_h: break
            last_h = h
        # cartes
        cards = await page.locator("a[href*='/dermatologue/']").all()
        seen = set()
        now = TZ.localize(datetime.now())
        for a in cards:
            try:
                name = (await a.inner_text()).strip()
                href = await a.get_attribute("href") or ""
                if not name or not href:
                    continue
                card = a.locator("xpath=ancestor::article | xpath=ancestor::div[contains(@class,'card')]")
                card_text = (await card.inner_text()).replace("\n", " ")
                dt = parse_date_fr(card_text)
                if not dt:
                    continue
                if now <= dt <= now + timedelta(days=window_days):
                    url = "https://www.doctolib.fr" + href if href.startswith("/") else href
                    key = (name, url)
                    if key in seen:
                        continue
                    seen.add(key)
                    out.append({"name": name, "date": dt, "url": url})
            except Exception:
                continue
        await browser.close()
    out.sort(key=lambda x: x["date"])
    return out

def fmt(items):
    if not items:
        return "Aucune disponibilité ≤ fenêtre définie."
    lines = ["🧴 Dermatologues avec RDV ≤ fenêtre définie (Toulouse):"]
    for it in items:
        d = it["date"].astimezone(TZ)
        hh = "" if d.strftime("%H%M") == "0000" else f" à {d.strftime('%Hh')}"
        lines.append(f"• {it['name']} — {d.strftime('%a %d/%m').capitalize()}{hh}\n  {it['url']}")
    return "\n".join(lines)

def send_telegram(text: str):
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        print("[INFO] Pas de TELEGRAM_BOT_TOKEN/CHAT_ID -> impression console uniquement.")
        print(text)
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "disable_web_page_preview": True}
        r = requests.post(url, json=payload, timeout=20)
        r.raise_for_status()
        print("[OK] Message Telegram envoyé.")
    except Exception as e:
        print("[ERR] Envoi Telegram:", e)

async def main():
    parser = argparse.ArgumentParser(description="Doctobot – Dispos derma Toulouse")
    parser.add_argument("--window", type=int, default=DEFAULT_WINDOW_DAYS, help="Fenêtre en jours (def. 14)")
    parser.add_argument("--print-only", dest="print_only", action="store_true",
                        help="N'envoie pas sur Telegram, affiche seulement")
    args = parser.parse_args()

    items = await fetch_derm(args.window)
    for it in items:
        print(f"{it['date'].strftime('%Y-%m-%d %H:%M')}  {it['name']}  {it['url']}")
    msg = fmt(items)

    if args.print_only:
        print("\n--- MESSAGE ---\n" + msg)
    else:
        send_telegram(msg)

if __name__ == "__main__":
    asyncio.run(main())
