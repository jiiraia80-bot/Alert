#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Promemoria su Telegram / Discord, eseguito da GitHub Actions (avviato ogni minuto da cron-job.org).

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
import time
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
    - 'giorni': il prossimo giorno scelto (Lu=0 ... Do=6) alla stessa ora.
    - ripetuto: la prima occorrenza (data + k*passo) che non e' ancora trascorsa (tolleranza compresa).
    """
    t0 = parse_evento(r)
    if t0 is None:
        return None
    if str(r.get("ripeti", "mai")).strip().lower() == "giorni":
        giorni = {int(g) for g in r.get("giorni", []) if str(g).isdigit() and 0 <= int(g) <= 6}
        if not giorni:
            return t0
        da = max(t0, adesso - tolleranza)
        for k in range(9):
            cand = datetime.datetime.combine(da.date() + datetime.timedelta(days=k), t0.time())
            if cand >= da and cand.weekday() in giorni:
                return cand
        return t0
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
    if anticipo:
        et = str(r.get("etichetta") or "").strip()
        righe.append(f"⏳ {et}: {anticipo} min" if et else f"⏳ {anticipo} min")
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
        if str(r.get("salta", "")) == ev.isoformat():
            continue
        if invio <= adesso <= invio + tol:
            pronti.append((rid, componi(r, ev, anticipo), ev.isoformat()))
    return pronti


class ErroreHTTP(RuntimeError):
    def __init__(self, codice, dettaglio):
        super().__init__(f"HTTP {codice} {dettaglio}")
        self.codice = codice


def post(url, corpo, tipo):
    req = urllib.request.Request(url, data=corpo, headers={"Content-Type": tipo, "User-Agent": "promemoria-actions/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status
    except urllib.error.HTTPError as e:
        try:
            dettaglio = e.read().decode("utf-8", "replace")[:200]
        except Exception:
            dettaglio = ""
        raise ErroreHTTP(e.code, dettaglio) from None


def prova_post(nome, url, corpo, tipo, tentativi=3):
    """Manda il messaggio e riprova se la destinazione ha un problema momentaneo. True se riuscito."""
    for n in range(1, tentativi + 1):
        try:
            post(url, corpo, tipo)
            print(f"  {nome} OK" + (f" (al tentativo {n})" if n > 1 else ""))
            return True
        except Exception as e:
            print(f"  {nome} ERRORE (tentativo {n}/{tentativi}): {e}")
            codice = getattr(e, "codice", None)
            if codice is not None and 400 <= codice < 500 and codice != 429:
                break                      # errore di configurazione (token, chat, webhook): riprovare non serve
            if n < tentativi:
                time.sleep(2 * n)
    return False


def invia(testo):
    """Manda il messaggio a Discord e/o Telegram. Ritorna (almeno_uno_ok, tutti_ok)."""
    token = os.environ.get("TELEGRAM_TOKEN", "").strip()
    chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    discord = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    base = os.environ.get("TELEGRAM_API_BASE", "https://api.telegram.org")
    esiti = []
    if discord:                            # Discord per primo: se Telegram e' lento il messaggio e' gia' arrivato
        esiti.append(prova_post("Discord", discord, json.dumps({"content": testo}).encode("utf-8"), "application/json"))
    if token and chat:
        esiti.append(prova_post("Telegram", f"{base}/bot{token}/sendMessage",
                                urllib.parse.urlencode({"chat_id": chat, "text": testo}).encode(),
                                "application/x-www-form-urlencoded"))
    if not esiti:
        print("  Nessuna destinazione: mancano i Secrets (TELEGRAM_TOKEN + TELEGRAM_CHAT_ID, oppure DISCORD_WEBHOOK_URL)")
    return any(esiti), bool(esiti) and all(esiti)


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
        ok, tutti = invia(f"✅ Prova: i promemoria da GitHub funzionano.\n🕒 {adesso:%d/%m/%Y %H:%M}")
        sys.exit(0 if tutti else 1)

    pronti = da_inviare(config, stato, adesso)
    if not pronti:
        print("Niente da inviare adesso.")
        return
    falliti = 0
    for rid, testo, istanza in pronti:
        print(f"Invio promemoria [{rid}]")
        ok, tutti = invia(testo)
        if ok:
            stato[rid] = istanza           # segnato come inviato se almeno una destinazione ha risposto
        if not tutti:
            falliti += 1                   # il run diventa rosso e GitHub ti manda l'email
    with open(FILE_STATO, "w", encoding="utf-8") as f:
        json.dump(stato, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
    if falliti:
        sys.exit(1)


if __name__ == "__main__":
    main()
