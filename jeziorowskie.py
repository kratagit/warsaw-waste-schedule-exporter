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

#: Osobny kalendarz - synchronizacja nie miesza sie z warszawska.
CALENDAR_NAME = "Wywóz Śmieci (Jeziorowskie)"

# --- pobieranie harmonogramow ze strony gminy ---

SERWIS = "https://stare-juchy.pl/"

#: Podstrony sektorow w serwisie gminy Stare Juchy.
STRONY_SEKTOROW = {
    1: SERWIS + "sektor-i-rogale-rogalik-skomack-wielki-skomack-osada-krolowa-wola-gorlo-zawady-elckie-gorlowko-orzechowo-szczecinowo-nowe-krzywe-plowce-lasmiady.html",
    2: SERWIS + "sektor-ii-liski-jeziorowskie-balamutowo-sikory-juskie-czerwonka-grabnik-grabnik-osada-olszewo-kaltki-panistruga.html",
    3: SERWIS + "sektor-iii-stare-juchy-stare-juchy-spoldzielnie-mieszkaniowe-i-wspolnoty-mieszkaniowe.html",
}

#: Serwis odrzuca zadania ze skroconym User-Agentem (HTTP 403), wiec podajemy
#: pelny naglowek przegladarki.
NAGLOWKI = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "pl-PL,pl;q=0.9",
}

#: Serwis potrafi odciac klienta po serii zapytan, wiec nie pozwalamy odpytywac
#: go czesciej niz co tyle sekund ani pobierac sektorow bez przerwy.
MIN_ODSTEP_SYNCHRONIZACJI = 60
ODSTEP_MIEDZY_SEKTORAMI = 2.0

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
        _bezpieczny_print(f"[{ts}][jeziorowskie] {msg}")
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


# --- pobieranie PDF-ow ze strony gminy ---

def _pobierz_bajty(url: str, timeout: int = 30) -> bytes:
    """Zwykle zadanie HTTP z naglowkami przegladarki (bez Selenium)."""
    zadanie = urllib.request.Request(url, headers=NAGLOWKI)
    try:
        with urllib.request.urlopen(zadanie, timeout=timeout) as odp:
            return odp.read()
    except urllib.error.HTTPError as e:
        if e.code == 403:
            raise Exception(
                f"Serwis odrzucił żądanie (HTTP 403). Strona gminy potrafi "
                f"blokować po serii zapytań - spróbuj za kilkanaście minut."
            ) from e
        raise Exception(f"Błąd HTTP {e.code} przy pobieraniu {url}") from e
    except urllib.error.URLError as e:
        raise Exception(f"Brak połączenia ze stroną gminy ({e.reason})") from e


def znajdz_link_pdf(html: str, numer: int) -> str | None:
    """Wyszukuje w kodzie strony adres pliku PDF z harmonogramem.

    Adres zawiera zmienny numer (np. .../324_harmonogram-sektor-2.pdf), ktory
    zmienia sie przy kazdej aktualizacji harmonogramu, dlatego czytamy go ze
    strony zamiast zapisywac na sztywno.
    """
    kandydaci = sorted(set(re.findall(r"files/file_add/download/[^\"'\s>]+?\.pdf", html)))
    if not kandydaci:
        return None

    # preferujemy plik, ktorego nazwa wskazuje na wlasciwy sektor
    rzymskie = {1: "i", 2: "ii", 3: "iii"}.get(numer, "")
    for k in kandydaci:
        nazwa = k.rsplit("/", 1)[-1].lower()
        if f"sektor-{numer}" in nazwa or (rzymskie and f"sektor-{rzymskie}-" in nazwa):
            return urllib.parse.urljoin(SERWIS, k)

    # Bez dopasowania bierzemy plik tylko wtedy, gdy jest jedyny na stronie -
    # inaczej moglibysmy zapisac harmonogram innego sektora pod zla nazwa.
    if len(kandydaci) == 1:
        return urllib.parse.urljoin(SERWIS, kandydaci[0])
    return None


def pobierz_pdf_sektora(numer: int, log) -> str:
    """Pobiera ze strony gminy PDF danego sektora. Zwraca sciezke do pliku."""
    strona = STRONY_SEKTOROW.get(numer)
    if not strona:
        raise Exception(f"Nie znam adresu strony dla sektora {numer}")

    log(f"Pobieram stronę sektora {numer}...")
    html = _pobierz_bajty(strona).decode("utf-8", errors="replace")

    link = znajdz_link_pdf(html, numer)
    if not link:
        raise Exception(f"Nie znalazłem odnośnika do PDF na stronie sektora {numer}")
    log(f"Znaleziono plik: {link.rsplit('/', 1)[-1]}")

    dane = _pobierz_bajty(link, timeout=60)
    if not dane.startswith(b"%PDF"):
        raise Exception("Pobrany plik nie jest dokumentem PDF")

    os.makedirs(PDF_DIR, exist_ok=True)
    # nazwe nadajemy sami - zgodna z tym, czego szuka sciezka_pdf()
    cel = os.path.join(PDF_DIR, f"Harmonogram sektor {numer}.pdf")
    with open(cel, "wb") as f:
        f.write(dane)
    log(f"Zapisano {os.path.basename(cel)} ({len(dane) // 1024} kB)")
    return cel


def pobierz_i_przetworz(numery: list[int]) -> dict:
    """Pobiera harmonogramy ze strony gminy i od razu je odczytuje."""
    global _ostatnia_synchronizacja
    wynik = {"status": "success", "logs": [], "pobrane": [], "bledy": []}

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

        import koma_parser
        rozpoznawacz = koma_parser.Rozpoznawacz()
        from pathlib import Path

        for i, numer in enumerate(numery):
            _ustaw_postep(10 + int(i / len(numery) * 60),
                          f"Pobieranie sektora {numer}...")
            if i:
                time.sleep(ODSTEP_MIEDZY_SEKTORAMI)
            try:
                sciezka = pobierz_pdf_sektora(numer, log)
            except Exception as e:
                log(f"BŁĄD (sektor {numer}): {e}")
                wynik["bledy"].append(f"sektor {numer}: {e}")
                continue

            _ustaw_postep(10 + int((i + 0.5) / len(numery) * 60),
                          f"Odczytywanie sektora {numer}...")
            dane = koma_parser.przetworz(Path(sciezka), rozpoznawacz)
            rozpoznany = dane.get("numer_sektora")
            if rozpoznany is None:
                raise Exception("Nie rozpoznano numeru sektora w pobranym PDF")
            if rozpoznany != numer:
                # numer sektora bierzemy z tresci dokumentu, wiec poprawiamy tez
                # nazwe pliku, zeby nie rozjechala sie z zawartoscia
                log(f"Uwaga: pobrany plik to sektor {rozpoznany}, a nie {numer}.")
                wlasciwa = os.path.join(PDF_DIR, f"Harmonogram sektor {rozpoznany}.pdf")
                if os.path.abspath(wlasciwa) != os.path.abspath(sciezka):
                    os.replace(sciezka, wlasciwa)
            with open(os.path.join(DATA_DIR, f"sektor-{rozpoznany}.json"),
                      "w", encoding="utf-8") as f:
                json.dump(dane, f, ensure_ascii=False, indent=2)
            log(f"Odczytano: {dane.get('sektor')} {dane.get('rok')}, "
                f"{dane.get('liczba_odbiorow')} terminów"
                + (f", ostrzeżeń: {len(dane.get('ostrzezenia', []))}"
                   if dane.get("ostrzezenia") else ""))
            wynik["pobrane"].append({
                "sektor": dane.get("sektor"),
                "numer": rozpoznany,
                "rok": dane.get("rok"),
                "liczba_odbiorow": dane.get("liczba_odbiorow"),
                "ostrzezenia": dane.get("ostrzezenia", []),
            })

        if wynik["bledy"] and not wynik["pobrane"]:
            raise Exception("; ".join(wynik["bledy"]))

        _ustaw_postep(100, "Zakończono pomyślnie!", status="finished")
        with _progress_lock:
            _progress["result"] = wynik
    except Exception as e:
        wynik["status"] = "error"
        wynik["message"] = str(e)
        with _progress_lock:
            _progress.update({"status": "error", "message": str(e), "result": wynik})
    return wynik


def uruchom_pobieranie(numery: list[int]) -> None:
    """Startuje pobieranie w tle (postep czytany przez `stan_postepu`)."""
    with _progress_lock:
        _progress.update({"status": "running", "percent": 0,
                          "message": "Łączenie ze stroną gminy...", "result": None})
    threading.Thread(target=pobierz_i_przetworz, args=(numery,), daemon=True).start()


# --- ponowne przetworzenie PDF-ow (bez internetu) ---

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
