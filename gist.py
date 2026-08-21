"""Wspolna obsluga publikacji plikow do GitHub Gist.

Korzystaja z tego dwa niezalezne harmonogramy - warszawski i jeziorowskie.
Kazdy ma wlasny plik konfiguracyjny i wlasny Gist, wiec publikacja jednego
nie rusza drugiego. Modul nie wie nic o odpadach - dostaje gotowa tresc.
"""

import datetime
import json
import os
import urllib.error
import urllib.request

API = "https://api.github.com/gists"


def oczysc_token(wartosc: str) -> str:
    """Sprowadza token do czystej postaci - bez spacji i cudzyslowow.

    Cudzyslowy zdejmujemy, bo Docker Compose przekazuje wartosc z pliku .env
    doslownie: GITHUB_GIST_TOKEN="ghp_..." dotarloby tu razem z apostrofami
    i GitHub odrzucilby je jako bledne dane logowania.
    """
    wartosc = (wartosc or "").strip()
    while len(wartosc) >= 2 and wartosc[0] == wartosc[-1] and wartosc[0] in "\"'":
        wartosc = wartosc[1:-1].strip()
    return wartosc


def token_ze_srodowiska() -> str:
    return oczysc_token(os.environ.get("GITHUB_GIST_TOKEN")
                        or os.environ.get("GITHUB_TOKEN") or "")


def wczytaj_config(plik: str) -> dict:
    """Wczytuje stan publikacji (gist_id, raw_url, last_updated) i token."""
    cfg = {"token": "", "token_source": "none", "gist_id": "",
           "raw_url": "", "last_updated": "", "frakcje": []}

    if os.path.exists(plik):
        try:
            with open(plik, "r", encoding="utf-8") as f:
                zapisane = json.load(f)
            if isinstance(zapisane, dict):
                cfg.update(zapisane)
                if cfg.get("token"):
                    cfg["token_source"] = "file"
        except Exception:
            pass

    # Priorytet ma zmienna srodowiskowa (.env / system)
    env_token = token_ze_srodowiska()
    if env_token:
        cfg["token"] = env_token
        cfg["token_source"] = "env"

    return cfg


def zapisz_config(plik: str, cfg: dict) -> None:
    """Zapisuje stan publikacji. Tokenu z .env nie utrwalamy na dysku."""
    os.makedirs(os.path.dirname(plik), exist_ok=True)
    do_zapisu = dict(cfg)
    if token_ze_srodowiska():
        do_zapisu.pop("token", None)
    do_zapisu.pop("token_source", None)
    try:
        with open(plik, "w", encoding="utf-8") as f:
            json.dump(do_zapisu, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def wywolaj_api(url: str, token: str, method: str = "GET",
                data: dict | None = None) -> dict:
    """Wysyla zadanie do GitHub REST API."""
    headers = {
        "Authorization": f"Bearer {token.strip()}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "Warsaw-Waste-Schedule-Exporter",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    body = json.dumps(data).encode("utf-8") if data is not None else None
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        komunikat = f"Błąd GitHub API (HTTP {e.code})"
        try:
            tresc = json.loads(e.read().decode("utf-8"))
            if "message" in tresc:
                komunikat += f": {tresc['message']}"
        except Exception:
            pass
        raise Exception(komunikat) from e
    except urllib.error.URLError as e:
        raise Exception(f"Brak połączenia z GitHub ({e.reason})") from e


def publikuj(plik_konfiguracyjny: str, nazwa_pliku: str, opis: str,
             tresc: str, token: str | None = None,
             frakcje: list[str] | None = None) -> dict:
    """Tworzy lub aktualizuje niepubliczny Gist z podana trescia.

    Args:
        frakcje: lista frakcji zawartych w publikowanym pliku. Zapisujemy ja
                 w konfiguracji, zeby panel mogl rozpoznac, ze uzytkownik
                 zmienil filtry i opublikowany kalendarz jest nieaktualny.

    Zwraca slownik ze stalym adresem raw_url i identyfikatorem Gista.
    """
    cfg = wczytaj_config(plik_konfiguracyjny)
    tok = oczysc_token(token) or oczysc_token(cfg.get("token", ""))
    if not tok:
        raise Exception("Wprowadź GitHub Personal Access Token (z uprawnieniem 'gist').")

    payload = {
        "description": opis,
        "public": False,
        "files": {nazwa_pliku: {"content": tresc}},
    }

    gist_id = cfg.get("gist_id")
    odpowiedz = None

    if gist_id:
        try:
            odpowiedz = wywolaj_api(f"{API}/{gist_id}", tok, method="PATCH", data=payload)
        except Exception as e:
            # Gist mogl zostac usuniety recznie - wtedy zakladamy nowy
            if "404" in str(e) or "Not Found" in str(e):
                gist_id = None
            else:
                raise

    if not gist_id or not odpowiedz:
        odpowiedz = wywolaj_api(API, tok, method="POST", data=payload)
        gist_id = odpowiedz.get("id")

    wlasciciel = odpowiedz.get("owner", {}).get("login", "")
    if wlasciciel and gist_id:
        # adres bez numeru rewizji - zawsze wskazuje najnowsza wersje
        raw_url = f"https://gist.githubusercontent.com/{wlasciciel}/{gist_id}/raw/{nazwa_pliku}"
    else:
        raw_url = odpowiedz.get("files", {}).get(nazwa_pliku, {}).get("raw_url", "")

    teraz = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cfg.update({"token": tok, "gist_id": gist_id, "raw_url": raw_url,
                "last_updated": teraz, "frakcje": sorted(frakcje or [])})
    zapisz_config(plik_konfiguracyjny, cfg)

    return {
        "status": "success",
        "gist_id": gist_id,
        "raw_url": raw_url,
        "last_updated": teraz,
        "frakcje": sorted(frakcje or []),
        "message": "Pomyślnie opublikowano kalendarz na GitHub Gist!",
    }
