#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Promemoria su Telegram / Discord, eseguito da GitHub Actions ogni 5 minuti.

- Legge  promemoria.json  (i promemoria) e  stato.json  (cosa e' gia' stato inviato).
- Se e' arrivato il momento di un promemoria (evento - "anticipo" minuti) manda il messaggio.
- Token e webhook NON stanno nei file: arrivano dai "Secrets" del repository
  (TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, DISCORD_WEBHOOK_URL).
Solo libreria standard di Python: nessuna installazione.
"""
import os
import sys
import json
import math
import datetime
import urllib.request
import urllib.parse
import urllib.error

try:
    from zoneinfo import ZoneInfo
except ImportError:          # Python < 3.9 (non dovrebbe capitare su GitHub)
    ZoneInfo = None

QUI = os.path.dirname(os.path.abspath(__file__))
FILE_PROMEMORIA = os.path.join(QUI, "promemoria.json")
FILE_STATO = os.path.join(QUI, "stato.json")
PASSO_GIORNI = {"mai": 0, "ogni giorno": 1, "giornaliero": 1, "ogni settimana": 7, "settimanale": 7}


def leggi_json(percorso, default):
    try:
        with open(percorso, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def parse_evento(r):
    """Data/ora dell'evento (naive, ora locale) oppure None. Data: AAAA-MM-GG oppure GG/MM/AAAA."""
    try:
        d = str(r["data"]).strip().replace(".", "/").replace("-", "/")
        p = [int(x) for x in d.split("/")]
        if p[0] > 31:                       # AAAA/MM/GG
            a, m, g = p
        else:                               # GG/MM/AAAA
            g, m, a = p
        if a < 100:
            a += 2000
        return datetime.datetime(a, m, g, int(r["ora"]), int(r["minuti"]))
    except Exception:
        return None


def istanza_corrente(r, adesso, tolleranza):
    """
    L'evento a cui riferirsi adesso.
    - 'mai': sempre la data scritta.
    - ripetuto: la prima occorrenza (data + k*passo) che non e' ancora trascorsa (tolleranza compresa).
    """
    t0 = parse_evento(r)
    if t0 is None:
        return None
    passo = PASSO_GIORNI.get(str(r.get("ripeti", "mai")).strip().lower(), 0)
    if not passo:
        return t0
    passo_td = datetime.timedelta(days=passo)
    if adesso <= t0 + tolleranza:
        return t0
    k = math.ceil(((adesso - tolleranza) - t0) / passo_td)
    return t0 + max(0, k) * passo_td


def componi(r, evento, anticipo):
    righe = []
    if str(r.get("titolo", "")).strip():
        righe.append(f"📌 {str(r['titolo']).strip()}")
    if str(r.get("messaggio", "")).strip():
        righe.append(str(r["messaggio"]).strip())
    righe.append(f"🕒 {evento:%d/%m/%Y %H:%M}" + (f" (tra {anticipo} min)" if anticipo else ""))
    return "\n".join(righe)


def da_inviare(config, stato, adesso):
    """Lista di (id, testo, istanza_iso) da mandare adesso."""
    tol = datetime.timedelta(minutes=int(config.get("tolleranza_ritardo_min", 30)))
    pronti = []
    for r in config.get("promemoria", []):
        if not r.get("attivo"):
            continue
        rid = str(r.get("id", ""))
        try:
            anticipo = max(0, int(r.get("anticipo_min", 0)))
        except (ValueError, TypeError):
            print(f"[{rid}] 'anticipo_min' non valido, salto")
            continue
        ev = istanza_corrente(r, adesso, tol)
        if ev is None:
            print(f"[{rid}] data/ora non valide, salto")
            continue
        invio = ev - datetime.timedelta(minutes=anticipo)
        if stato.get(rid) == ev.isoformat():
            continue
        if invio <= adesso <= ev + tol:
            pronti.append((rid, componi(r, ev, anticipo), ev.isoformat()))
    return pronti


def post(url, corpo, tipo):
    req = urllib.request.Request(url, data=corpo, headers={"Content-Type": tipo, "User-Agent": "promemoria-actions/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status
    except urllib.error.HTTPError as e:
        try:
            dettaglio = e.read().decode("utf-8", "replace")[:200]
        except Exception:
            dettaglio = ""
        raise RuntimeError(f"HTTP {e.code} {dettaglio}") from None


def invia(testo):
    """Manda il messaggio a Telegram e/o Discord. True se almeno una destinazione ha funzionato."""
    token = os.environ.get("TELEGRAM_TOKEN", "").strip()
    chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    discord = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    base = os.environ.get("TELEGRAM_API_BASE", "https://api.telegram.org")
    ok, tentate = False, 0
    if token and chat:
        tentate += 1
        try:
            post(f"{base}/bot{token}/sendMessage",
                 urllib.parse.urlencode({"chat_id": chat, "text": testo}).encode(),
                 "application/x-www-form-urlencoded")
            print("  Telegram OK")
            ok = True
        except Exception as e:
            print(f"  Telegram ERRORE: {e}")
    if discord:
        tentate += 1
        try:
            post(discord, json.dumps({"content": testo}).encode("utf-8"), "application/json")
            print("  Discord OK")
            ok = True
        except Exception as e:
            print(f"  Discord ERRORE: {e}")
    if not tentate:
        print("  Nessuna destinazione: mancano i Secrets (TELEGRAM_TOKEN + TELEGRAM_CHAT_ID, oppure DISCORD_WEBHOOK_URL)")
    return ok


def main():
    config = leggi_json(FILE_PROMEMORIA, {})
    stato = leggi_json(FILE_STATO, {})
    nome_fuso = config.get("fuso_orario", "Europe/Rome")
    if os.environ.get("ADESSO_FINTO"):                 # solo per i test
        adesso = datetime.datetime.fromisoformat(os.environ["ADESSO_FINTO"])
    elif ZoneInfo:
        adesso = datetime.datetime.now(ZoneInfo(nome_fuso)).replace(tzinfo=None)
    else:
        adesso = datetime.datetime.utcnow() + datetime.timedelta(hours=1)
    print(f"Ora locale ({nome_fuso}): {adesso:%d/%m/%Y %H:%M:%S}")

    if os.environ.get("PROVA", "").lower() in ("1", "true", "si", "yes"):
        print("Messaggio di PROVA")
        ok = invia(f"✅ Prova: i promemoria da GitHub funzionano.\n🕒 {adesso:%d/%m/%Y %H:%M}")
        sys.exit(0 if ok else 1)

    pronti = da_inviare(config, stato, adesso)
    if not pronti:
        print("Niente da inviare adesso.")
        return
    falliti = 0
    for rid, testo, istanza in pronti:
        print(f"Invio promemoria [{rid}]")
        if invia(testo):
            stato[rid] = istanza           # segnato come inviato solo se almeno una destinazione ha risposto
        else:
            falliti += 1
    with open(FILE_STATO, "w", encoding="utf-8") as f:
        json.dump(stato, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
    if falliti:
        sys.exit(1)                        # il run risulta fallito: GitHub ti manda una mail e al prossimo giro riprova


if __name__ == "__main__":
    main()
