# -*- coding: utf-8 -*-
"""Obsluga harmonogramu dla gminy Stare Juchy (Jeziorowskie i okolice, firma KOMA).

Modul jest celowo niezalezny od czesci warszawskiej aplikacji:
 * ma wlasny stan postepu (nie rusza globalnego progress_state),
 * zapisuje do osobnego kalendarza Google,
 * trzyma dane w katalogu data/jeziorowskie.

Dzieki temu korzystanie z tej czesci nie moze zaklocic synchronizacji z 19115.

Dane pochodza z PDF-ow firmy KOMA, czytanych przez `koma_parser` (analiza
pikseli - bez OCR i bez uslug zewnetrznych).
"""

from __future__ import annotations

import datetime
import glob
import json
import os
import threading

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data", "jeziorowskie")
PDF_DIR = os.path.join(DATA_DIR, "pdf")

#: Osobny kalendarz - synchronizacja nie miesza sie z warszawska.
CALENDAR_NAME = "Wywóz Śmieci (Jeziorowskie)"

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

def wczytaj_sektor(numer: int) -> dict | None:
    sciezka = os.path.join(DATA_DIR, f"sektor-{numer}.json")
    if not os.path.exists(sciezka):
        return None
    try:
        with open(sciezka, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def lista_sektorow() -> list[dict]:
    """Skrocony opis kazdego dostepnego sektora - do wyboru w interfejsie."""
    sektory = []
    for sciezka in sorted(glob.glob(os.path.join(DATA_DIR, "sektor-*.json"))):
        try:
            with open(sciezka, "r", encoding="utf-8") as f:
                dane = json.load(f)
        except Exception:
            continue
        numer = dane.get("numer_sektora")
        if numer is None:
            continue
        sektory.append({
            "numer": numer,
            "sektor": dane.get("sektor") or f"SEKTOR {numer}",
            "wykaz": dane.get("wykaz") or "",
            "miejscowosci": dane.get("miejscowosci", []),
            "rok": dane.get("rok"),
            "liczba_odbiorow": dane.get("liczba_odbiorow", 0),
            "ostrzezenia": dane.get("ostrzezenia", []),
            "pdf_dostepny": sciezka_pdf(numer) is not None,
        })
    return sorted(sektory, key=lambda s: s["numer"])


def sciezka_pdf(numer: int) -> str | None:
    trafienia = sorted(glob.glob(os.path.join(PDF_DIR, f"*sektor {numer}*.pdf")))
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


def harmonogram(numer: int, tylko_przyszle: bool = False) -> dict | None:
    """Pelny harmonogram sektora, gotowy do wyswietlenia lub wyslania."""
    dane = wczytaj_sektor(numer)
    if not dane:
        return None

    wszystkie = _odbiory(dane)
    dzis = datetime.date.today().isoformat()
    nadchodzace = [o for o in wszystkie if o["data"] >= dzis]
    widoczne = nadchodzace if tylko_przyszle else wszystkie

    dostepne_id = {x["id"] for x in dane.get("frakcje", [])}
    return {
        "numer": dane.get("numer_sektora", numer),
        "sektor": dane.get("sektor"),
        "rok": dane.get("rok"),
        "wykaz": dane.get("wykaz"),
        "miejscowosci": dane.get("miejscowosci", []),
        "frakcje": [f for f in FRAKCJE if f["id"] in dostepne_id],
        "odbiory": widoczne,
        "nadchodzace": nadchodzace[:6],
        "wg_miesiecy": dane.get("wg_miesiecy", []),
        "liczba_odbiorow": dane.get("liczba_odbiorow", len(wszystkie)),
        "liczba_nadchodzacych": len(nadchodzace),
        "ostrzezenia": dane.get("ostrzezenia", []),
        "pdf_dostepny": sciezka_pdf(numer) is not None,
        "zrodlo_pdf": dane.get("plik"),
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


def synchronizuj(service, numer: int, dozwolone: list[str],
                 tylko_przyszle: bool = True) -> dict:
    """Dodaje terminy sektora do osobnego kalendarza Google."""
    wynik = {
        "status": "success", "logs": [], "added_events": 0, "skipped": 0,
        "sektor": numer, "allowed_types": dozwolone,
        "tylko_przyszle": tylko_przyszle,
    }

    def log(msg: str) -> None:
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        print(f"[{ts}][jeziorowskie] {msg}")
        wynik["logs"].append(f"[{ts}] {msg}")

    try:
        _ustaw_postep(5, "Wczytywanie harmonogramu...")
        dane = harmonogram(numer, tylko_przyszle=tylko_przyszle)
        if not dane:
            raise Exception(f"Brak danych dla sektora {numer}")
        log(f"--- START: {dane['sektor']}, rok {dane['rok']} ---")

        pozycje = [
            (o["data"], fr["id"])
            for o in dane["odbiory"]
            for fr in o["frakcje"]
            if fr["id"] in dozwolone
        ]
        if not pozycje:
            raise Exception("Brak terminów do dodania (sprawdź filtry i zakres dat)")
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
        with _progress_lock:
            _progress.update({"percent": 100, "message": "Zakończono pomyślnie!",
                              "status": "finished", "result": wynik})
    except Exception as e:
        log(f"BŁĄD: {e}")
        wynik["status"] = "error"
        wynik["message"] = str(e)
        with _progress_lock:
            _progress.update({"status": "error", "message": str(e), "result": wynik})
    return wynik


def uruchom_synchronizacje(service, numer: int, dozwolone: list[str],
                           tylko_przyszle: bool = True) -> None:
    """Startuje synchronizacje w tle (postep czytany przez `stan_postepu`)."""
    with _progress_lock:
        _progress.update({"status": "running", "percent": 0,
                          "message": "Inicjalizacja...", "result": None})
    threading.Thread(
        target=synchronizuj,
        args=(service, numer, dozwolone, tylko_przyszle),
        daemon=True,
    ).start()


# --- ponowne przetworzenie PDF-ow (most do przyszlego pobierania ze strony) ---

def przetworz_pdfy() -> dict:
    """Czyta ponownie PDF-y z data/jeziorowskie/pdf i odswieza pliki JSON."""
    from pathlib import Path

    import koma_parser

    rozpoznawacz = koma_parser.Rozpoznawacz()
    przetworzone, bledy = [], []
    for sciezka in sorted(glob.glob(os.path.join(PDF_DIR, "*.pdf"))):
        nazwa = os.path.basename(sciezka)
        try:
            dane = koma_parser.przetworz(Path(sciezka), rozpoznawacz)
            numer = dane.get("numer_sektora")
            if numer is None:
                bledy.append(f"{nazwa}: nie rozpoznano numeru sektora")
                continue
            with open(os.path.join(DATA_DIR, f"sektor-{numer}.json"),
                      "w", encoding="utf-8") as f:
                json.dump(dane, f, ensure_ascii=False, indent=2)
            przetworzone.append({
                "plik": nazwa,
                "sektor": dane.get("sektor"),
                "rok": dane.get("rok"),
                "liczba_odbiorow": dane.get("liczba_odbiorow"),
                "ostrzezenia": dane.get("ostrzezenia", []),
            })
        except Exception as e:
            bledy.append(f"{nazwa}: {e}")
    return {
        "status": "error" if bledy and not przetworzone else "success",
        "przetworzone": przetworzone,
        "bledy": bledy,
    }
