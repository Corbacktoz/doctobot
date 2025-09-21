import os, asyncio, re, json
from datetime import datetime, timedelta
import pytz
import requests
from dateutil import parser as dp
from playwright.async_api import async_playwright

DOCTOLIB_URL = "https://www.doctolib.fr/dermatologue/toulouse?availabilities=1"
TZ = pytz.timezone("Europe/Paris")
WINDOW_DAYS = 14

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
STATE_PATH = os.getenv("STATE_PATH", "/data/state.json")

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

def fr_to_en_date(s: str) -> str:
    t = s.lower()
    for fr,en in MONTHS_FR.items(): t = re.sub(rf"\b{fr}\b", en, t)
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

async def fetch_derm_in_14_days():
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
        # scroll
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
                if now <= dt <= now + timedelta(days=WINDOW_DAYS):
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

def load_state():
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"items": []}

def save_state(items):
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump({"items": items}, f, ensure_ascii=False, indent=2)

def canonicalize(items):
    # On ne garde que (name, url, date_jour) pour la comparaison
    canon = []
    for it in items:
        d = it["date"].astimezone(TZ)
        canon.append({
            "name": it["name"],
            "url": it["url"],
            "day": d.strftime("%Y-%m-%d")  # ignore l’heure si Doctolib ne la donne pas toujours
        })
    return sorted(canon, key=lambda x: (x["day"], x["name"], x["url"]))

def compute_diff(old, new):
    old_set = {(i["name"], i["url"], i["day"]) for i in old}
    new_set = {(i["name"], i["url"], i["day"]) for i in new}
    added = new_set - old_set
    removed = old_set - new_set
    return added, removed

def send_telegram(text):
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        print("TELEGRAM_BOT_TOKEN ou TELEGRAM_CHAT_ID manquant — aucun envoi Telegram.")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "disable_web_page_preview": True}
    try:
        r = requests.post(url, json=payload, timeout=15)
        r.raise_for_status()
    except Exception as e:
        print("Erreur envoi Telegram:", e)

def fmt(items):
    lines = ["🧴 Dermatologues avec RDV ≤ 14 jours (Toulouse):"]
    for it in items:
        lines.append(
            f"• {it['name']} — {it['date'].strftime('%a %d/%m').capitalize()} "
            f"{'' if it['date'].strftime('%H%M')=='0000' else 'à ' + it['date'].strftime('%Hh')}\n  {it['url']}"
        )
    return "\n".join(lines)

async def main():
    items = await fetch_derm_in_14_days()
    # log console
    for it in items:
        print(f"{it['date'].strftime('%Y-%m-%d')}  {it['name']}  {it['url']}")
    # diff vs état précédent
    state = load_state()
    old = state.get("items", [])
    new = canonicalize(items)
    added, removed = compute_diff(old, new)

    if not old and not new and os.getenv("NOTIFY_WHEN_EMPTY", "0") == "1":
        send_telegram("Aucune dispo détectée pour les 14 prochains jours.")
    elif added or removed:
        # Il y a du nouveau (ajouts ou suppressions) → on envoie la liste complète courante
        send_telegram(fmt(items) if items else "Plus de disponibilité ≤ 14 jours pour le moment.")
        save_state(new)
    else:
        print("Aucun changement — pas d'envoi Telegram.")

if __name__ == "__main__":
    asyncio.run(main())
