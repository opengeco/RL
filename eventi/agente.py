"""Agente eventi: legge i post recenti degli account Instagram in fonti.json,
filtra quelli che parlano di eventi (keyword) e ne estrae i dati con Claude.

Variabili d'ambiente:
  IG_USER_ID         id dell'account Instagram Business/Creator che fa le richieste
  META_TOKEN         token di accesso (long-lived) con instagram_basic + pages_show_list
  ANTHROPIC_API_KEY  chiave API Anthropic
  CLAUDE_MODEL       opzionale, default claude-opus-5-5
  GRAPH_VERSION      opzionale, default v23.0

Uso:
  python eventi/agente.py              esecuzione normale
  python eventi/agente.py --prova      non chiama Claude, stampa solo i post candidati
"""

import json
import os
import re
import sys
import unicodedata
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import anthropic
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
    client = None if prova else anthropic.Anthropic()
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
        if prova:
            for p in scelti:
                print("   -", (p.get("caption") or "").replace("\n", " ")[:120])
            continue
        if not scelti:
            continue
        per_id = {p["id"]: p for p in scelti}
        try:
            trovati = estrai_eventi(client, username, nome, scelti)
        except anthropic.APIError as err:
            print(f"  @{username}: errore Claude: {err}")
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
