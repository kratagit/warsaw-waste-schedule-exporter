# -*- coding: utf-8 -*-
"""Harmonogram odbioru odpadow dla sektora II gminy Stare Juchy (firma KOMA).

Modul obsluguje wylacznie sektor II - Liski, Jeziorowskie, Balamutowo i okolice.

Jest celowo niezalezny od czesci warszawskiej aplikacji:
 * ma wlasny stan postepu (nie rusza globalnego progress_state),
 * zapisuje do osobnego kalendarza Google,
 * trzyma dane w katalogu data/jeziorowskie.

Dzieki temu korzystanie z tej czesci nie moze zaklocic synchronizacji z 19115.

Harmonogram jest w PDF-ie obrazkiem - czyta go `koma_parser` przez analize
pikseli, bez OCR i bez uslug zewnetrznych.
"""

from __future__ import annotations

import datetime
import glob
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request


def _bezpieczny_print(msg: str) -> None:
    """Wypisuje komunikat tak, by polskie znaki nie wywrocily watku.

    Przy przekierowanym wyjsciu Windows koduje stdout w cp1252 i print
    z polskimi znakami rzuca UnicodeEncodeError - a wyjatek w logowaniu
    bledu zostawilby synchronizacje na zawsze w stanie "running".
    """
    try:
        print(msg)
    except UnicodeEncodeError:
        try:
            kod = getattr(sys.stdout, "encoding", None) or "ascii"
            print(msg.encode(kod, errors="replace").decode(kod, errors="replace"))
        except Exception:
            pass
    except Exception:
        pass


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data", "jeziorowskie")
PDF_DIR = os.path.join(DATA_DIR, "pdf")

#: Obslugiwany sektor. Program czyta wylacznie jego harmonogram.
SEKTOR = 2

#: Osobny kalendarz - synchronizacja nie miesza sie z warszawska.
CALENDAR_NAME = "Wywóz Śmieci (Jeziorowskie)"

# --- pobieranie harmonogramu ze strony gminy ---

SERWIS = "https://stare-juchy.pl/"
STRONA_SEKTORA = (
    SERWIS + "sektor-ii-liski-jeziorowskie-balamutowo-sikory-juskie-czerwonka-"
    "grabnik-grabnik-osada-olszewo-kaltki-panistruga.html"
)

#: Serwis odrzuca zadania ze skroconym User-Agentem (HTTP 403), wiec podajemy
#: pelny naglowek przegladarki.
NAGLOWKI = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "pl-PL,pl;q=0.9",
}

#: Serwis potrafi odciac klienta po serii zapytan, wiec nie pozwalamy odpytywac
#: go czesciej niz co tyle sekund.
MIN_ODSTEP_SYNCHRONIZACJI = 60

_ostatnia_synchronizacja = 0.0

#: Kolejnosc, nazwy, ikony i kolory wydarzen w Kalendarzu Google.
#: colorId Google: 3=Grape, 4=Flamingo, 5=Banana, 6=Tangerine, 7=Peacock,
#: 8=Graphite, 10=Basil.
FRAKCJE = [
    {"id": "zmieszane",                  "nazwa": "Zmieszane",                  "icon": "fas fa-trash-alt",    "colorId": "8"},
    {"id": "bio",                        "nazwa": "Bio",                        "icon": "fas fa-leaf",         "colorId": "6"},
    {"id": "metale_i_tworzywa_sztuczne", "nazwa": "Metale i tworzywa sztuczne", "icon": "fas fa-cube",         "colorId": "5"},
    {"id": "papier",                     "nazwa": "Papier",                     "icon": "fas fa-newspaper",    "colorId": "7"},
    {"id": "szklo",                      "nazwa": "Szkło",                      "icon": "fas fa-glass-cheers", "colorId": "10"},
    {"id": "gabaryty",                   "nazwa": "Gabaryty",                   "icon": "fas fa-couch",        "colorId": "3"},
    {"id": "popiol",                     "nazwa": "Popiół",                     "icon": "fas fa-fire",         "colorId": "4"},
]
FRAKCJE_WG_ID = {f["id"]: f for f in FRAKCJE}

# --- wlasny stan postepu (niezalezny od warszawskiego) ---

_progress = {"status": "idle", "percent": 0, "message": "Gotowy", "result": None}
_progress_lock = threading.Lock()


def stan_postepu() -> dict:
    with _progress_lock:
        return dict(_progress)


def _ustaw_postep(percent: int, message: str, status: str = "running") -> None:
    with _progress_lock:
        _progress["percent"] = percent
        _progress["message"] = message
        _progress["status"] = status


def czy_trwa() -> bool:
    with _progress_lock:
        return _progress["status"] == "running"


# --- wczytywanie danych ---

def _plik_danych() -> str:
    return os.path.join(DATA_DIR, f"sektor-{SEKTOR}.json")


def _plik_logow() -> str:
    return os.path.join(DATA_DIR, "last_logs.json")


def wczytaj_logi() -> list[str]:
    """Odczytuje trwale zapisane logi ostatniej operacji."""
    if os.path.exists(_plik_logow()):
        try:
            with open(_plik_logow(), "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    # Jeśli plik logów nie istnieje, ale mamy wczytane dane sektora, zwróć log informacyjny
    dane = wczytaj_dane()
    if dane:
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        return [
            f"[{ts}][jeziorowskie] Załadowano harmonogram z pliku lokalnego: {dane.get('sektor', 'SEKTOR II')} (rok {dane.get('rok', '')})",
            f"[{ts}][jeziorowskie] Odczytano {dane.get('liczba_odbiorow', len(dane.get('odbiory', [])))} terminów z pliku \"{dane.get('plik', '')}\"",
            f"[{ts}][jeziorowskie] Gotowy do synchronizacji lub eksportu.",
        ]
    return []


def zapisz_logi(logi: list[str]) -> None:
    """Zapisuje listę logów do trwałego pliku JSON."""
    os.makedirs(DATA_DIR, exist_ok=True)
    try:
        with open(_plik_logow(), "w", encoding="utf-8") as f:
            json.dump(logi, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def dodaj_log(msg: str) -> None:
    """Dopisuje pojedynczy wpis do logów i wypisuje w konsoli."""
    ts = datetime.datetime.now().strftime("%H:%M:%S")
    wpis = f"[{ts}][jeziorowskie] {msg}"
    _bezpieczny_print(wpis)
    logi = wczytaj_logi()
    logi.append(wpis)
    if len(logi) > 200:
        logi = logi[-200:]
    zapisz_logi(logi)


def wyczysc_logi() -> None:
    """Czyści trwale zapisane logi dla regionu Jeziorowskie."""
    zapisz_logi([])
    with _progress_lock:
        if _progress.get("result") and isinstance(_progress["result"], dict):
            _progress["result"]["logs"] = []


def wczytaj_dane() -> dict | None:
    """Surowy odczyt zapisanego harmonogramu."""
    if not os.path.exists(_plik_danych()):
        return None
    try:
        with open(_plik_danych(), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def sciezka_pdf() -> str | None:
    trafienia = sorted(glob.glob(os.path.join(PDF_DIR, f"*sektor {SEKTOR}*.pdf")))
    return trafienia[0] if trafienia else None


# --- harmonogram ---

def _odbiory(dane: dict) -> list[dict]:
    """Lista odbiorow wzbogacona o nazwy i ikony frakcji (do wyswietlenia)."""
    wynik = []
    for wpis in dane.get("odbiory", []):
        wynik.append({
            "data": wpis["data"],
            "dzien_tygodnia": wpis.get("dzien_tygodnia", ""),
            "frakcje": [
                {
                    "id": fid,
                    "nazwa": FRAKCJE_WG_ID.get(fid, {}).get("nazwa", fid),
                    "icon": FRAKCJE_WG_ID.get(fid, {}).get("icon", "fas fa-trash"),
                }
                for fid in wpis.get("frakcje", [])
            ],
        })
    return wynik


def harmonogram() -> dict | None:
    """Caly harmonogram sektora - do wyswietlenia i do wyslania do kalendarza."""
    dane = wczytaj_dane()
    if not dane:
        return None

    wszystkie = _odbiory(dane)
    dzis = datetime.date.today().isoformat()
    nadchodzace = [o for o in wszystkie if o["data"] >= dzis]

    dostepne_id = {x["id"] for x in dane.get("frakcje", [])}
    return {
        "numer": dane.get("numer_sektora", SEKTOR),
        "sektor": dane.get("sektor"),
        "rok": dane.get("rok"),
        "wykaz": dane.get("wykaz"),
        "miejscowosci": dane.get("miejscowosci", []),
        "frakcje": [f for f in FRAKCJE if f["id"] in dostepne_id],
        "odbiory": wszystkie,
        "wg_miesiecy": dane.get("wg_miesiecy", []),
        "liczba_odbiorow": dane.get("liczba_odbiorow", len(wszystkie)),
        "liczba_nadchodzacych": len(nadchodzace),
        "ostrzezenia": dane.get("ostrzezenia", []),
        "pdf_dostepny": sciezka_pdf() is not None,
        "zrodlo_pdf": dane.get("plik"),
        "logs": wczytaj_logi(),
    }


def konfiguracja() -> dict:
    """Dane potrzebne interfejsowi przy starcie."""
    return {
        "sektor": SEKTOR,
        "strona": STRONA_SEKTORA,
        "fractions": FRAKCJE,
        "calendar": CALENDAR_NAME,
    }


# --- synchronizacja z Kalendarzem Google ---

def _znajdz_lub_utworz_kalendarz(service, log) -> str:
    page_token = None
    while True:
        clist = service.calendarList().list(pageToken=page_token).execute()
        for e in clist.get("items", []):
            if e.get("summary") == CALENDAR_NAME:
                return e["id"]
        page_token = clist.get("nextPageToken")
        if not page_token:
            break
    utworzony = service.calendars().insert(
        body={"summary": CALENDAR_NAME, "timeZone": "Europe/Warsaw"}
    ).execute()
    log(f"Utworzono kalendarz \"{CALENDAR_NAME}\".")
    return utworzony["id"]


def synchronizuj(service, dozwolone: list[str]) -> dict:
    """Dodaje do osobnego kalendarza Google wszystkie terminy z harmonogramu."""
    wynik = {
        "status": "success", "logs": [], "added_events": 0, "skipped": 0,
        "allowed_types": dozwolone,
    }

    def log(msg: str) -> None:
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        _bezpieczny_print(f"[{ts}][jeziorowskie] {msg}")
        wynik["logs"].append(f"[{ts}] {msg}")

    try:
        _ustaw_postep(5, "Wczytywanie harmonogramu...")
        dane = harmonogram()      # caly rocznik, nie tylko nadchodzace
        if not dane:
            raise Exception("Brak danych harmonogramu - najpierw kliknij Synchronizuj")
        log(f"--- START: {dane['sektor']}, rok {dane['rok']} ---")

        pozycje = [
            (o["data"], fr["id"])
            for o in dane["odbiory"]
            for fr in o["frakcje"]
            if fr["id"] in dozwolone
        ]
        if not pozycje:
            raise Exception("Brak terminów do dodania (sprawdź filtry frakcji)")
        log(f"Do dodania: {len(pozycje)} terminów.")

        _ustaw_postep(15, "Łączenie z Kalendarzem Google...")
        cal_id = _znajdz_lub_utworz_kalendarz(service, log)

        _ustaw_postep(20, "Sprawdzanie istniejących wydarzeń...")
        istniejace = set()
        page_token = None
        while True:
            odp = service.events().list(
                calendarId=cal_id, singleEvents=True, maxResults=2500,
                pageToken=page_token
            ).execute()
            for ev in odp.get("items", []):
                istniejace.add((ev.get("start", {}).get("date"), ev.get("summary")))
            page_token = odp.get("nextPageToken")
            if not page_token:
                break
        log(f"W kalendarzu jest już {len(istniejace)} wydarzeń.")

        opis = f"{dane['sektor']} - {dane.get('wykaz') or ''}".strip(" -")
        for i, (data_iso, frakcja_id) in enumerate(pozycje):
            frakcja = FRAKCJE_WG_ID.get(frakcja_id, {"nazwa": frakcja_id, "colorId": "8"})
            summary = f"Odbiór: {frakcja['nazwa']}"
            _ustaw_postep(25 + int((i / len(pozycje)) * 70),
                          f"{data_iso} - {frakcja['nazwa']}")
            if (data_iso, summary) in istniejace:
                wynik["skipped"] += 1
                continue
            body = {
                "summary": summary,
                "description": opis,
                "start": {"date": data_iso},
                "end": {"date": data_iso},
                "colorId": frakcja.get("colorId", "8"),
                "transparency": "transparent",
                "reminders": {"useDefault": False, "overrides": [
                    {"method": "popup", "minutes": 300},
                    {"method": "email", "minutes": 300},
                ]},
            }
            service.events().insert(calendarId=cal_id, body=body).execute()
            istniejace.add((data_iso, summary))
            wynik["added_events"] += 1
            log(f" -> DODANO: {frakcja['nazwa']} ({data_iso})")

        wynik["timestamp"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log(f"--- SUKCES: dodano {wynik['added_events']}, "
            f"pominięto duplikatów {wynik['skipped']} ---")
        zapisz_logi(wynik["logs"])
        with _progress_lock:
            _progress.update({"percent": 100, "message": "Zakończono pomyślnie!",
                              "status": "finished", "result": wynik})
    except Exception as e:
        log(f"BŁĄD: {e}")
        wynik["status"] = "error"
        wynik["message"] = str(e)
        zapisz_logi(wynik["logs"])
        with _progress_lock:
            _progress.update({"status": "error", "message": str(e), "result": wynik})
    return wynik


def uruchom_synchronizacje(service, dozwolone: list[str]) -> None:
    """Startuje wysylke do kalendarza w tle."""
    with _progress_lock:
        _progress.update({"status": "running", "percent": 0,
                          "message": "Inicjalizacja...", "result": None})
    threading.Thread(
        target=synchronizuj,
        args=(service, dozwolone),
        daemon=True,
    ).start()


# --- pobieranie PDF-u ze strony gminy ---

def _pobierz_bajty(url: str, timeout: int = 30) -> bytes:
    """Zwykle zadanie HTTP z naglowkami przegladarki (bez Selenium)."""
    zadanie = urllib.request.Request(url, headers=NAGLOWKI)
    try:
        with urllib.request.urlopen(zadanie, timeout=timeout) as odp:
            return odp.read()
    except urllib.error.HTTPError as e:
        if e.code == 403:
            raise Exception(
                "Serwis odrzucił żądanie (HTTP 403). Strona gminy potrafi "
                "blokować po serii zapytań - spróbuj za kilkanaście minut."
            ) from e
        raise Exception(f"Błąd HTTP {e.code} przy pobieraniu {url}") from e
    except urllib.error.URLError as e:
        raise Exception(f"Brak połączenia ze stroną gminy ({e.reason})") from e


def znajdz_link_pdf(html: str) -> str | None:
    """Wyszukuje w kodzie strony adres pliku PDF z harmonogramem.

    Adres zawiera zmienny numer (np. .../324_harmonogram-sektor-2.pdf), ktory
    zmienia sie przy kazdej aktualizacji harmonogramu, dlatego czytamy go ze
    strony zamiast zapisywac na sztywno.
    """
    kandydaci = sorted(set(re.findall(r"files/file_add/download/[^\"'\s>]+?\.pdf", html)))
    if not kandydaci:
        return None

    for k in kandydaci:
        nazwa = k.rsplit("/", 1)[-1].lower()
        if f"sektor-{SEKTOR}" in nazwa or "sektor-ii-" in nazwa:
            return urllib.parse.urljoin(SERWIS, k)

    # Bez dopasowania bierzemy plik tylko wtedy, gdy jest jedyny na stronie -
    # inaczej moglibysmy zapisac harmonogram innego sektora.
    if len(kandydaci) == 1:
        return urllib.parse.urljoin(SERWIS, kandydaci[0])
    return None


def pobierz_pdf(log) -> str:
    """Pobiera ze strony gminy PDF sektora. Zwraca sciezke do pliku."""
    log("Pobieram stronę sektora...")
    html = _pobierz_bajty(STRONA_SEKTORA).decode("utf-8", errors="replace")

    link = znajdz_link_pdf(html)
    if not link:
        raise Exception("Nie znalazłem odnośnika do PDF na stronie sektora")
    log(f"Znaleziono plik: {link.rsplit('/', 1)[-1]}")

    dane = _pobierz_bajty(link, timeout=60)
    if not dane.startswith(b"%PDF"):
        raise Exception("Pobrany plik nie jest dokumentem PDF")

    os.makedirs(PDF_DIR, exist_ok=True)
    cel = os.path.join(PDF_DIR, f"Harmonogram sektor {SEKTOR}.pdf")
    with open(cel, "wb") as f:
        f.write(dane)
    log(f"Zapisano {os.path.basename(cel)} ({len(dane) // 1024} kB)")
    return cel


def pobierz_i_przetworz() -> dict:
    """Pobiera harmonogram ze strony gminy i od razu go odczytuje."""
    global _ostatnia_synchronizacja
    wynik = {"status": "success", "logs": []}

    def log(msg: str) -> None:
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        _bezpieczny_print(f"[{ts}][jeziorowskie] {msg}")
        wynik["logs"].append(f"[{ts}] {msg}")

    try:
        odstep = time.monotonic() - _ostatnia_synchronizacja
        if _ostatnia_synchronizacja and odstep < MIN_ODSTEP_SYNCHRONIZACJI:
            raise Exception(
                f"Odczekaj {int(MIN_ODSTEP_SYNCHRONIZACJI - odstep)} s - "
                f"strona gminy blokuje zbyt częste zapytania."
            )
        _ostatnia_synchronizacja = time.monotonic()

        _ustaw_postep(15, "Pobieranie harmonogramu ze strony gminy...")
        sciezka = pobierz_pdf(log)

        _ustaw_postep(60, "Odczytywanie harmonogramu z PDF...")
        from pathlib import Path

        import koma_parser
        dane = koma_parser.przetworz(Path(sciezka), koma_parser.Rozpoznawacz())

        rozpoznany = dane.get("numer_sektora")
        if rozpoznany is not None and rozpoznany != SEKTOR:
            raise Exception(
                f"Pobrany plik to sektor {rozpoznany}, a nie {SEKTOR} - "
                f"sprawdź, czy strona gminy nie zmieniła treści."
            )

        _ustaw_postep(90, "Zapisywanie danych...")
        with open(_plik_danych(), "w", encoding="utf-8") as f:
            json.dump(dane, f, ensure_ascii=False, indent=2)

        log(f"Odczytano: {dane.get('sektor')} {dane.get('rok')}, "
            f"{dane.get('liczba_odbiorow')} terminów"
            + (f", ostrzeżeń: {len(dane.get('ostrzezenia', []))}"
               if dane.get("ostrzezenia") else ""))

        wynik.update({
            "sektor": dane.get("sektor"),
            "rok": dane.get("rok"),
            "liczba_odbiorow": dane.get("liczba_odbiorow"),
            "ostrzezenia": dane.get("ostrzezenia", []),
        })
        _ustaw_postep(100, "Zakończono pomyślnie!", status="finished")
        zapisz_logi(wynik["logs"])
        with _progress_lock:
            _progress["result"] = wynik
    except Exception as e:
        log(f"BŁĄD: {e}")
        wynik["status"] = "error"
        wynik["message"] = str(e)
        zapisz_logi(wynik["logs"])
        with _progress_lock:
            _progress.update({"status": "error", "message": str(e), "result": wynik})
    return wynik


def uruchom_pobieranie() -> None:
    """Startuje pobieranie w tle (postep czytany przez `stan_postepu`)."""
    with _progress_lock:
        _progress.update({"status": "running", "percent": 0,
                          "message": "Łączenie ze stroną gminy...", "result": None})
    threading.Thread(target=pobierz_i_przetworz, daemon=True).start()


# --- eksport do formatu iCalendar (.ics) ---

def _escape_ical_text(tekst: str) -> str:
    """Escapuje znaki specjalne dla pól tekstowych RFC 5545 (SUMMARY, DESCRIPTION)."""
    if not tekst:
        return ""
    tekst = str(tekst).replace("\\", "\\\\")
    tekst = tekst.replace(";", "\\;")
    tekst = tekst.replace(",", "\\,")
    tekst = tekst.replace("\r\n", "\\n").replace("\n", "\\n").replace("\r", "\\n")
    return tekst


def _fold_ical_line(line: str) -> str:
    """Zawija wiersz zgodnie z RFC 5545 (maksymalnie 75 oktetów na wiersz)."""
    b = line.encode("utf-8")
    if len(b) <= 75:
        return line
    chunks = []
    while len(b) > 75:
        split_idx = 75
        while split_idx > 0:
            try:
                b[:split_idx].decode("utf-8")
                break
            except UnicodeDecodeError:
                split_idx -= 1
        chunks.append(b[:split_idx].decode("utf-8"))
        b = b" " + b[split_idx:]
    if b:
        chunks.append(b.decode("utf-8"))
    return "\r\n".join(chunks)


def generuj_ics(dozwolone: list[str] | None = None) -> str | None:
    """Generuje plik .ics (iCalendar) zgodny z RFC 5545 i Google Calendar.

    Args:
        dozwolone: lista identyfikatorów frakcji do uwzględnienia. Jeśli None,
                   eksportowane są wszystkie dostępne w harmonogramie frakcje.

    Returns:
        Treść pliku .ics ze znakami końca linii CRLF (\r\n) lub None w razie braku danych.
    """
    dane = harmonogram()
    if not dane or not dane.get("odbiory"):
        return None

    if dozwolone is None:
        dozwolone = [f["id"] for f in dane.get("frakcje", [])]

    # Filtruj pozycje
    pozycje = [
        (o["data"], fr["id"])
        for o in dane["odbiory"]
        for fr in o["frakcje"]
        if fr["id"] in dozwolone
    ]
    if not pozycje:
        return None

    opis = f"{dane.get('sektor', '')} - {dane.get('wykaz', '')}".strip(" -")
    opis_escaped = _escape_ical_text(opis)
    now_utc = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    linie = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Harmonogram Wywozu//Jeziorowskie//PL",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{_escape_ical_text(CALENDAR_NAME)}",
        "X-WR-TIMEZONE:Europe/Warsaw",
    ]

    for data_iso, frakcja_id in pozycje:
        frakcja = FRAKCJE_WG_ID.get(frakcja_id, {"nazwa": frakcja_id})
        nazwa_frakcji = frakcja.get("nazwa", frakcja_id)
        summary = f"Odbiór: {nazwa_frakcji}"
        summary_escaped = _escape_ical_text(summary)

        dt_start = datetime.date.fromisoformat(data_iso)
        dt_end = dt_start + datetime.timedelta(days=1)
        dtstart_str = dt_start.strftime("%Y%m%d")
        dtend_str = dt_end.strftime("%Y%m%d")

        uid = f"{dtstart_str}-{frakcja_id}-sektor{SEKTOR}@stare-juchy.pl"

        linie.extend([
            "BEGIN:VEVENT",
            f"UID:{uid}",
            f"DTSTAMP:{now_utc}",
            f"DTSTART;VALUE=DATE:{dtstart_str}",
            f"DTEND;VALUE=DATE:{dtend_str}",
            f"SUMMARY:{summary_escaped}",
            f"DESCRIPTION:{opis_escaped}",
            "STATUS:CONFIRMED",
            "TRANSP:TRANSPARENT",
            "END:VEVENT",
        ])

    linie.append("END:VCALENDAR")

    nazwy_frakcji = [FRAKCJE_WG_ID.get(fid, {}).get("nazwa", fid) for fid in dozwolone]
    dodaj_log(f"Wyeksportowano plik .ics: {len(pozycje)} terminów (frakcje: {', '.join(nazwy_frakcji)})")

    linie_folded = [_fold_ical_line(l) for l in linie]
    return "\r\n".join(linie_folded) + "\r\n"

