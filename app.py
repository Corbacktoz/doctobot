import os, asyncio, re, argparse
from datetime import datetime, timedelta
import pytz
import requests
from dateutil import parser as dp
from playwright.async_api import async_playwright

# --------- Config ----------
DOCTOLIB_URL = "https://www.doctolib.fr/pneumologue-pediatrique/toulouse?availabilities=1"
MAIIA_URL = "https://www.maiia.com/recherche/pneumologue-pediatrique/toulouse"
TZ = pytz.timezone("Europe/Paris")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
DEFAULT_WINDOW_DAYS = int(os.getenv("WINDOW_DAYS", "14"))

# ---------------------------

def fr_to_en_date(s: str) -> str:
    months = {
        "janvier": "January", "février": "February", "fevrier": "February", "mars": "March", "avril": "April",
        "mai": "May", "juin": "June", "juillet": "July", "août": "August", "aout": "August",
        "septembre": "September", "octobre": "October", "novembre": "November", "décembre": "December"
    }
    t = s.lower()
    for fr, en in months.items():
        t = re.sub(rf"\b{fr}\b", en, t)
    return t

def parse_date_fr(text: str):
    for pat in [
        r"([0-9]{1,2}\s+\w+\s+[0-9]{4})",
        r"([0-9]{1,2}\s+\w+)",
        r"aujourd'hui", r"demain"
    ]:
        if "aujourd" in pat and "aujourd" in text.lower():
            return TZ.localize(datetime.now())
        if "demain" in pat and "demain" in text.lower():
            return TZ.localize(datetime.now() + timedelta(days=1))
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            raw = m.group(1)
            try:
                en = fr_to_en_date(raw)
                dt = dp.parse(en, dayfirst=True)
                if dt.tzinfo is None:
                    dt = TZ.localize(dt)
                return dt
            except Exception:
                pass
    return None

# ---------- Doctolib ----------
async def fetch_doctolib(window_days: int):
    out = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(locale="fr-FR", timezone_id="Europe/Paris")
        page = await context.new_page()
        await page.goto(DOCTOLIB_URL, wait_until="domcontentloaded")
        try:
            await page.get_by_role("button", name=re.compile("Accepter|Tout accepter|J'accepte", re.I)).click(timeout=3000)
        except Exception:
            pass
        await page.wait_for_timeout(1500)
        cards = await page.locator("a[href*='/pneumologue']").all()
        now = TZ.localize(datetime.now())
        seen = set()
        for a in cards:
            try:
                name = (await a.inner_text()).strip()
                href = await a.get_attribute("href") or ""
                card = a.locator("xpath=ancestor::article | xpath=ancestor::div[contains(@class,'card')]")
                text = (await card.inner_text()).replace("\n", " ")
                dt = parse_date_fr(text)
                if dt and now <= dt <= now + timedelta(days=window_days):
                    url = "https://www.doctolib.fr" + href if href.startswith("/") else href
                    if (name, url) not in seen:
                        seen.add((name, url))
                        out.append({"source": "Doctolib", "name": name, "date": dt, "url": url})
            except Exception:
                continue
        await browser.close()
    return out

# ---------- Maiia ----------
async def fetch_maiia(window_days: int):
    out = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(locale="fr-FR", timezone_id="Europe/Paris")
        page = await context.new_page()
        await page.goto(MAIIA_URL, wait_until="domcontentloaded")
        await page.wait_for_timeout(2000)
        cards = await page.locator("a[href*='/cabinet/'], a[href*='/docteur/']").all()
        now = TZ.localize(datetime.now())
        seen = set()
        for a in cards:
            try:
                name = (await a.inner_text()).strip()
                href = await a.get_attribute("href") or ""
                card = a.locator("xpath=ancestor::article | xpath=ancestor::div")
                text = (await card.inner_text()).replace("\n", " ")
                dt = parse_date_fr(text)
                if dt and now <= dt <= now + timedelta(days=window_days):
                    url = "https://www.maiia.com" + href if href.startswith("/") else href
                    if (name, url) not in seen:
                        seen.add((name, url))
                        out.append({"source": "Maiia", "name": name, "date": dt, "url": url})
            except Exception:
                continue
        await browser.close()
    return out

# ---------- Format + Telegram ----------
def fmt(items):
    if not items:
        return "Aucune disponibilité trouvée sur Doctolib ou Maiia."
    items.sort(key=lambda x: x["date"])
    lines = ["👶 Pneumologues pédiatriques disponibles (Toulouse):"]
    for it in items:
        d = it["date"].astimezone(TZ)
        hh = "" if d.strftime("%H%M") == "0000" else f" à {d.strftime('%Hh')}"
        lines.append(f"• [{it['source']}] {it['name']} — {d.strftime('%a %d/%m').capitalize()}{hh}\n  {it['url']}")
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

# ---------- Main ----------
async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--window", type=int, default=DEFAULT_WINDOW_DAYS)
    parser.add_argument("--print-only", dest="print_only", action="store_true")
    args = parser.parse_args()

    print("[INFO] Recherche Doctolib + Maiia…")
    results_doctolib = await fetch_doctolib(args.window)
    results_maiia = await fetch_maiia(args.window)
    all_items = results_doctolib + results_maiia

    msg = fmt(all_items)

    if args.print_only:
        print("\n--- MESSAGE ---\n" + msg)
    else:
        send_telegram(msg)

if __name__ == "__main__":
    asyncio.run(main())
