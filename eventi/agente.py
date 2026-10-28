"""Agente eventi: legge i post recenti degli account Instagram in fonti.json,
filtra quelli che parlano di eventi (keyword) e ne estrae i dati.

L'estrazione è gratuita, a regole (date, orari, prezzi, @menzioni).
Se è impostata ANTHROPIC_API_KEY usa invece Claude, più preciso ma a pagamento.

Variabili d'ambiente:
  IG_USER_ID         id dell'account Instagram Business/Creator che fa le richieste
  META_TOKEN         token di accesso (long-lived) con instagram_basic + pages_show_list
  ANTHROPIC_API_KEY  opzionale, chiave API Anthropic (se assente: estrazione a regole)
  CLAUDE_MODEL       opzionale, default claude-opus-5-5
  GRAPH_VERSION      opzionale, default v23.0

Uso:
  python eventi/agente.py              esecuzione normale
  python eventi/agente.py --prova      non salva nulla, stampa i post candidati e gli eventi estratti a regole
"""

import json
import os
import re
import sys
import unicodedata
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

CARTELLA = Path(__file__).parent
FONTI = CARTELLA / "fonti.json"
USCITA = CARTELLA / "eventi.json"
MAX_VISTI = 3000

MODELLO = os.environ.get("CLAUDE_MODEL", "claude-opus-5-5")
GRAPH = f"https://graph.facebook.com/{os.environ.get('GRAPH_VERSION', 'v23.0')}"

SCHEMA = {
    "type": "object",
    "properties": {
        "eventi": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "post_id": {"type": "string"},
                    "titolo": {"type": "string"},
                    "data": {"type": "string", "description": "YYYY-MM-DD"},
                    "ora": {"type": "string", "description": "HH:MM oppure stringa vuota"},
                    "luogo": {"type": "string"},
                    "citta": {"type": "string"},
                    "artisti": {"type": "array", "items": {"type": "string"}},
                    "categoria": {
                        "type": "string",
                        "enum": ["dj set", "live", "concerto", "festival", "party", "aperitivo", "altro"],
                    },
                    "prezzo": {"type": "string"},
                },
                "required": ["post_id", "titolo", "data", "ora", "luogo", "citta", "artisti", "categoria", "prezzo"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["eventi"],
    "additionalProperties": False,
}

ISTRUZIONI = """Ricevi i post Instagram di un account (locale, DJ, organizzatore).
Per ogni evento futuro o di oggi annunciato nei post, restituisci un elemento in "eventi".
- Considera solo eventi con musica o intrattenimento (dj set, live, concerti, festival, party, aperitivi con musica).
- Ricava la data assoluta usando la data di pubblicazione del post: "sabato" o "stasera" si riferiscono ai giorni successivi alla pubblicazione.
- Se un post annuncia più date, crea un elemento per data.
- Scarta post che non annunciano un evento preciso (foto ricordo, promozioni generiche, eventi già passati).
- Se un dato manca lascia la stringa vuota; non inventare. Se manca il luogo e l'account è un locale, usa il nome dell'account.
- post_id deve essere l'id del post da cui hai preso l'evento."""


def norm(s):
    s = unicodedata.normalize("NFD", str(s or "")).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]", "", s.lower())


def leggi_json(percorso, default):
    try:
        return json.loads(percorso.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def post_account(username, quanti):
    campi = (
        f"business_discovery.username({username})"
        f"{{username,name,media.limit({quanti}){{id,caption,timestamp,permalink,media_type,media_url,thumbnail_url}}}}"
    )
    r = requests.get(
        f"{GRAPH}/{os.environ['IG_USER_ID']}",
        params={"fields": campi, "access_token": os.environ["META_TOKEN"]},
        timeout=30,
    )
    dati = r.json()
    if "error" in dati:
        raise RuntimeError(dati["error"].get("message", str(dati["error"])))
    bd = dati.get("business_discovery", {})
    return bd.get("name") or username, bd.get("media", {}).get("data", [])


def candidati(posts, regex, limite, visti):
    out = []
    for p in posts:
        testo = p.get("caption") or ""
        quando = datetime.fromisoformat(p["timestamp"].replace("+0000", "+00:00"))
        if p["id"] in visti or quando < limite or not regex.search(testo):
            continue
        out.append(p)
    return out


def estrai_eventi(client, username, nome, posts):
    righe = [
        {"post_id": p["id"], "pubblicato": p["timestamp"][:10], "testo": p.get("caption", "")}
        for p in posts
    ]
    prompt = (
        f"Oggi è {date.today().isoformat()}.\n"
        f"Account: @{username} ({nome})\n\n"
        f"Post:\n{json.dumps(righe, ensure_ascii=False, indent=1)}"
    )
    risposta = client.beta.messages.create(
        model=MODELLO,
        max_tokens=16000,
        system=ISTRUZIONI,
        messages=[{"role": "user", "content": prompt}],
        output_config={"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}},
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
    )
    if risposta.stop_reason == "refusal":
        print(f"  @{username}: richiesta rifiutata dal modello, salto")
        return []
    testo = next(b.text for b in risposta.content if b.type == "text")
    return json.loads(testo)["eventi"]


MESI = {
    "gennaio": 1, "gen": 1, "febbraio": 2, "feb": 2, "marzo": 3, "mar": 3, "aprile": 4, "apr": 4,
    "maggio": 5, "mag": 5, "giugno": 6, "giu": 6, "luglio": 7, "lug": 7, "agosto": 8, "ago": 8,
    "settembre": 9, "set": 9, "sett": 9, "ottobre": 10, "ott": 10, "novembre": 11, "nov": 11,
    "dicembre": 12, "dic": 12,
}
GIORNI = {
    "lunedi": 0, "martedi": 1, "mercoledi": 2, "giovedi": 3, "venerdi": 4, "sabato": 5, "domenica": 6,
    "lun": 0, "mar": 1, "mer": 2, "gio": 3, "ven": 4, "sab": 5, "dom": 6,
}
RE_DATA_MESE = re.compile(r"\b(\d{1,2})\s*(?:°|º)?\s+(" + "|".join(sorted(MESI, key=len, reverse=True)) + r")\b\.?(?:\s+(\d{4}))?")
RE_DATA_NUM = re.compile(r"\b(\d{1,2})[/-](\d{1,2})(?:[/-](\d{2,4}))?\b")
RE_GIORNO = re.compile(r"\b(lunedi|martedi|mercoledi|giovedi|venerdi|sabato|domenica)\b")
RE_ORA = re.compile(r"\b(?:ore|h|dalle|from|start|inizio|apertura|opening)\s*:?\s*(\d{1,2})(?:[:.](\d{2}))?\b")
RE_ORA_NUDA = re.compile(r"\b(\d{1,2}):(\d{2})\b")
RE_PREZZO = re.compile(r"(?:€\s*\d+(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?\s*(?:€|euro\b))|ingresso\s+(?:libero|gratuito|gratis|free)|free\s+entry", re.IGNORECASE)
RE_MENZIONE = re.compile(r"@([A-Za-z0-9_.]{2,30})")
CATEGORIE = [
    ("festival", ("festival", "fest ")),
    ("concerto", ("concerto", "concert", "in concerto", "tour")),
    ("live", ("live", "dal vivo", "band", "acustico", "unplugged")),
    ("dj set", ("dj", "djset", "consolle", "console", "b2b", "techno", "house")),
    ("party", ("party", "serata", "night", "notte")),
    ("aperitivo", ("aperitivo", "aperi", "happy hour", "sunset")),
]


def ascii_minuscolo(s):
    return unicodedata.normalize("NFD", s).encode("ascii", "ignore").decode().lower()


def data_valida(anno, mese, giorno):
    try:
        return date(anno, mese, giorno)
    except ValueError:
        return None


def anno_vicino(mese, giorno, rif):
    for anno in (rif.year, rif.year + 1):
        d = data_valida(anno, mese, giorno)
        if d and d >= rif - timedelta(days=1):
            return d
    return None


def trova_date(testo, pubblicato):
    t = ascii_minuscolo(testo)
    date_trovate = []
    for g, m, a in RE_DATA_MESE.findall(t):
        d = data_valida(int(a), MESI[m], int(g)) if a else anno_vicino(MESI[m], int(g), pubblicato)
        if d:
            date_trovate.append(d)
    for g, m, a in RE_DATA_NUM.findall(t):
        if not (1 <= int(m) <= 12):
            continue
        anno = int(a) + 2000 if a and len(a) == 2 else int(a) if a else None
        d = data_valida(anno, int(m), int(g)) if anno else anno_vicino(int(m), int(g), pubblicato)
        if d:
            date_trovate.append(d)
    if not date_trovate:
        if re.search(r"\b(stasera|stanotte|oggi|tonight|today)\b", t):
            date_trovate.append(pubblicato)
        elif re.search(r"\b(domani|tomorrow)\b", t):
            date_trovate.append(pubblicato + timedelta(days=1))
        else:
            for nome in RE_GIORNO.findall(t):
                date_trovate.append(pubblicato + timedelta(days=(GIORNI[nome] - pubblicato.weekday()) % 7))
    return sorted(set(date_trovate))[:6]


def trova_ora(testo):
    t = ascii_minuscolo(testo)
    for regex in (RE_ORA, RE_ORA_NUDA):
        for h, m in regex.findall(t):
            if int(h) <= 23 and int(m or 0) <= 59:
                return f"{int(h):02d}:{int(m or 0):02d}"
    return ""


def titolo_da(testo, nome):
    for riga in testo.splitlines():
        pulita = re.sub(r"#\w+", "", riga)
        pulita = re.sub(r"[^\w\s@&'’.,:!?()/+€-]", " ", pulita)
        pulita = re.sub(r"\s+", " ", pulita).strip(" .,:-")
        if len(pulita) >= 4:
            return pulita[:80]
    return nome


def estrai_eventi_regole(username, nome, posts):
    """Estrazione gratuita: una riga per ogni data trovata nel testo del post."""
    eventi = []
    for p in posts:
        testo = p.get("caption") or ""
        pubblicato = date.fromisoformat(p["timestamp"][:10])
        t = ascii_minuscolo(testo)
        categoria = next((c for c, parole in CATEGORIE if any(x in t for x in parole)), "altro")
        prezzo = RE_PREZZO.search(testo)
        artisti = [m for m in dict.fromkeys(RE_MENZIONE.findall(testo)) if m.lower() != username.lower()]
        for d in trova_date(testo, pubblicato):
            eventi.append({
                "post_id": p["id"],
                "titolo": titolo_da(testo, nome),
                "data": d.isoformat(),
                "ora": trova_ora(testo),
                "luogo": nome,
                "citta": "",
                "artisti": artisti[:8],
                "categoria": categoria,
                "prezzo": prezzo.group(0) if prezzo else "",
            })
    return eventi


def chiave(e):
    return f"{e['data']}|{norm(e['luogo'] or e['account'])}|{norm(e['titolo'])[:24]}"


def main():
    prova = "--prova" in sys.argv
    fonti = leggi_json(FONTI, {})
    archivio = leggi_json(USCITA, {"eventi": [], "visti": []})
    visti = set(archivio.get("visti", []))
    regex = re.compile(
        r"\b(" + "|".join(re.escape(k) for k in fonti.get("keywords", [])) + r")\b", re.IGNORECASE
    )
    limite = datetime.now(timezone.utc) - timedelta(days=fonti.get("giorni_indietro", 21))
    client = None
    if not prova and os.environ.get("ANTHROPIC_API_KEY"):
        import anthropic

        client = anthropic.Anthropic()
    print("Estrazione:", "Claude" if client else "a regole (gratuita)")
    oggi = date.today().isoformat()

    nuovi = []
    for username in fonti.get("instagram", []):
        username = username.strip().lstrip("@")
        try:
            nome, posts = post_account(username, fonti.get("post_per_account", 15))
        except Exception as err:
            print(f"@{username}: errore Instagram: {err}")
            continue
        scelti = candidati(posts, regex, limite, visti)
        print(f"@{username}: {len(posts)} post, {len(scelti)} candidati")
        if not scelti:
            continue
        per_id = {p["id"]: p for p in scelti}
        if client:
            try:
                trovati = estrai_eventi(client, username, nome, scelti)
            except Exception as err:
                print(f"  @{username}: errore Claude: {err}")
                continue
        else:
            trovati = estrai_eventi_regole(username, nome, scelti)
        if prova:
            for e in trovati:
                print(f"   - {e['data']} {e['ora'] or '--:--'} | {e['titolo']} | {e['categoria']} | {e['prezzo']}")
            continue
        for e in trovati:
            p = per_id.get(e["post_id"])
            if not p or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", e["data"]) or e["data"] < oggi:
                continue
            e.update(
                account=username,
                nome_account=nome,
                link=p.get("permalink", ""),
                immagine=p.get("thumbnail_url") or p.get("media_url", ""),
                descrizione=(p.get("caption") or "")[:400],
            )
            nuovi.append(e)
        visti.update(per_id)

    if prova:
        return

    unici = {}
    for e in archivio.get("eventi", []) + nuovi:
        if e["data"] >= oggi:
            unici[chiave(e)] = e
    eventi = sorted(unici.values(), key=lambda e: (e["data"], e["ora"] or "99:99"))

    USCITA.write_text(
        json.dumps(
            {
                "aggiornato": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "eventi": eventi,
                "visti": sorted(visti)[-MAX_VISTI:],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"Eventi nuovi: {len(nuovi)} - totale in archivio: {len(eventi)}")


if __name__ == "__main__":
    main()
