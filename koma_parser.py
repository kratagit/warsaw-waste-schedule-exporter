#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Ekstrakcja harmonogramu odbioru odpadow komunalnych (KOMA) z pliku PDF do JSON.

Harmonogram w PDF jest wklejony jako obrazek rastrowy - nie ma warstwy tekstowej
z datami. Program czyta go w calosci deterministycznie, bez OCR i bez AI:

  1. wyciaga wbudowany obrazek JPEG ze strumienia PDF,
  2. znajduje siatke tabeli: kolumny po jednolitych kolorach naglowka
     (czarny/brazowy/zolty/niebieski/zielony/szary), wiersze po bialych
     separatorach w kolumnie miesiecy,
  3. w kazdej komorce wyodrebnia plamy atramentu (connected components),
     grupuje je w tokeny rozdzielone przecinkami,
  4. rozpoznaje token dopasowujac go do wzorcow "1".."31" wyrenderowanych
     z czcionki systemowej (Arial Bold) - template matching na bitmapach,
  5. sektor i wykaz miejscowosci bierze z warstwy tekstowej PDF,
  6. skleja daty, waliduje je i zapisuje JSON.

Uzycie:
    python harmonogram.py "Harmonogram sektor 2.pdf"
    python harmonogram.py *.pdf --out wszystkie.json
    python harmonogram.py plik.pdf --raport      # tabela kontrolna na ekran

Wymaga: pdfplumber, Pillow, numpy  (pip install -r requirements.txt)
"""

from __future__ import annotations

import argparse
import datetime as dt
import glob
import io
import json
import re
import sys
from collections import deque
from pathlib import Path

import numpy as np
import pdfplumber
from PIL import Image, ImageDraw, ImageFont

# ---------------------------------------------------------------------------
# Konfiguracja: jak wyglada dokument
# ---------------------------------------------------------------------------

#: Kolory tla naglowkow kolumn -> identyfikator frakcji.
KOLORY_FRAKCJI = [
    ((42, 50, 53), "zmieszane", "Zmieszane"),
    ((145, 97, 51), "bio", "Bio"),
    ((255, 207, 6), "metale_i_tworzywa_sztuczne", "Metale i tworzywa sztuczne"),
    ((33, 84, 163), "papier", "Papier"),
    ((21, 174, 70), "szklo", "Szkło"),
]

#: Kolumny "Gabaryty" i "Popiol" maja identyczny szary naglowek - rozroznia je
#: kolejnosc wystepowania w tabeli.
KOLOR_SZARY = (203, 203, 203)
FRAKCJE_SZARE = [("gabaryty", "Gabaryty"), ("popiol", "Popiół")]

MIESIACE = [
    "Styczeń", "Luty", "Marzec", "Kwiecień", "Maj", "Czerwiec",
    "Lipiec", "Sierpień", "Wrzesień", "Październik", "Listopad", "Grudzień",
]

DNI_TYGODNIA = [
    "poniedziałek", "wtorek", "środa", "czwartek",
    "piątek", "sobota", "niedziela",
]

#: Kandydaci na sciezke czcionki - potrzebna do wygenerowania wzorcow.
CZCIONKI = [
    r"C:\Windows\Fonts\arialbd.ttf",
    r"C:\Windows\Fonts\ARIALBD.TTF",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/Library/Fonts/Arial Bold.ttf",
]

#: Minimalny udzial pikseli niebialych w kolumnie naglowka i minimalna
#: szerokosc kolumny w pikselach - sluzy do znalezienia granic kolumn.
MIN_UDZIAL_KOLUMNY = 0.20
MIN_SZEROKOSC_KOLUMNY = 40

#: Przy jakiej wysokosci (px) porownywane sa bitmapy tokenu i wzorca.
WYS_POROWNANIA = 24

#: Powyzej tej proporcji szerokosc/wysokosc plama zawiera wiecej niz jedna
#: cyfre (pojedyncza cyfra Arial Bold ma proporcje ok. 0,35-0,65).
PROPORCJA_ZLEPIENIA = 0.85

#: Wszystkie cyfry - kandydaci przy czytaniu znak po znaku.
CYFRY = [str(c) for c in range(10)]

#: Ponizej tego marginesu przewagi nad drugim kandydatem zglaszamy watpliwosc.
#: Poprawne odczyty schodza najnizej do ok. 0,12, bledne mialy 0,00-0,05.
MIN_MARGINES = 0.10


class BladHarmonogramu(Exception):
    """Dokument nie ma oczekiwanej struktury."""


# ---------------------------------------------------------------------------
# Krok 1: obrazek i tekst z PDF
# ---------------------------------------------------------------------------

def wczytaj_pdf(sciezka: Path) -> tuple[np.ndarray, str]:
    """Zwraca (tablica RGB strony jako obrazek, tekst warstwy tekstowej)."""
    try:
        pdf = pdfplumber.open(str(sciezka))
    except Exception as e:
        raise BladHarmonogramu(f"nie udalo sie otworzyc pliku PDF ({e})") from e
    with pdf:
        if not pdf.pages:
            raise BladHarmonogramu("PDF nie ma zadnej strony")
        strona = pdf.pages[0]
        tekst = strona.extract_text() or ""
        if not strona.images:
            raise BladHarmonogramu(
                "Na stronie nie ma obrazka - ten program czyta harmonogramy "
                "wklejone jako grafika rastrowa"
            )
        # najwiekszy obrazek na stronie = sam harmonogram
        obraz = max(strona.images, key=lambda im: im["width"] * im["height"])
        dane = obraz["stream"].get_rawdata()
        try:
            im = Image.open(io.BytesIO(dane))
            im.load()
        except Exception:
            # obrazek nie jest samodzielnym JPEG/PNG - renderujemy strone
            im = strona.to_image(resolution=200).original
        return np.asarray(im.convert("RGB")).astype(np.int16), tekst


# ---------------------------------------------------------------------------
# Krok 2: siatka tabeli
# ---------------------------------------------------------------------------

def znajdz_naglowek(a: np.ndarray) -> tuple[int, int]:
    """Zwraca (pierwszy, ostatni) wiersz pikseli naglowka tabeli.

    Naglowek to jedyne miejsce na stronie, gdzie nasycone kolory zajmuja duza
    czesc szerokosci - kolorowe pola frakcji ciagna sie przez wieksza czesc
    tabeli. Logo firmy jest wezsze, wiec tego progu nie przekracza.
    """
    nasycenie = a.max(axis=2) - a.min(axis=2)
    udzial = (nasycenie > 40).mean(axis=1)
    kandydaci = [y for y in range(a.shape[0]) if udzial[y] > 0.30]
    if not kandydaci:
        raise BladHarmonogramu("nie znaleziono kolorowego naglowka tabeli")

    # najdluzszy ciagly przedzial (dopuszczamy male przerwy od napisow/ikon)
    najlepszy = biezacy = [kandydaci[0], kandydaci[0]]
    for y in kandydaci[1:]:
        if y - biezacy[1] <= 10:
            biezacy[1] = y
        else:
            if biezacy[1] - biezacy[0] > najlepszy[1] - najlepszy[0]:
                najlepszy = biezacy
            biezacy = [y, y]
    if biezacy[1] - biezacy[0] > najlepszy[1] - najlepszy[0]:
        najlepszy = biezacy
    return najlepszy[0], najlepszy[1]


def znajdz_kolumny(a: np.ndarray, y_gora: int, y_dol: int) -> list[dict]:
    """Kolumny tabeli wyznaczone na podstawie naglowka.

    Kazda kolumna ma w naglowku wypelnione tlo (kolorowe albo szare), a rozdziela
    je cienka biala przerwa. Liczymy wiec dla kazdej kolumny pikseli udzial
    pikseli niebialych: wewnatrz kolumny jest wysoki (biale ikony i napisy go
    tylko obnizaja), w przerwie spada niemal do zera.

    Kolor kolumny to mediana *niebialych* pikseli - dzieki temu biale ikony i
    podpisy nie zaburzaja rozpoznania koloru tla.
    """
    pas = a[y_gora:y_dol + 1]
    niebiale = pas.min(axis=2) < 245
    udzial = niebiale.mean(axis=0)

    kolumny, x, W = [], 0, a.shape[1]
    while x < W:
        if udzial[x] <= MIN_UDZIAL_KOLUMNY:
            x += 1
            continue
        start = x
        while x < W and udzial[x] > MIN_UDZIAL_KOLUMNY:
            x += 1
        if x - start < MIN_SZEROKOSC_KOLUMNY:
            continue
        piksele = pas[:, start:x][niebiale[:, start:x]]
        kolumny.append({
            "x0": start, "x1": x,
            "kolor": tuple(int(round(v)) for v in np.median(piksele, axis=0)),
        })

    if len(kolumny) < 3:
        raise BladHarmonogramu("nie udalo sie podzielic tabeli na kolumny")
    return kolumny


def przypisz_frakcje(kolumny: list[dict]) -> tuple[dict, list[dict]]:
    """Rozpoznaje, ktora kolumna to ktora frakcja (po kolorze naglowka).

    Zwraca (kolumna_miesiecy, lista kolumn frakcji z kluczami id/nazwa).
    """
    def dystans(a, b):
        return max(abs(int(a[i]) - int(b[i])) for i in range(3))

    kolumna_miesiecy = kolumny[0]
    frakcje, szare = [], 0
    for kol in kolumny[1:]:
        kolor = kol["kolor"]
        trafienie = None
        for wzor, ident, nazwa in KOLORY_FRAKCJI:
            if dystans(kolor, wzor) <= 60:
                trafienie = (ident, nazwa)
                break
        if trafienie is None and dystans(kolor, KOLOR_SZARY) <= 25:
            if szare < len(FRAKCJE_SZARE):
                trafienie = FRAKCJE_SZARE[szare]
            szare += 1
        if trafienie is None:
            continue  # kolumna nieznana - pomijamy zamiast zgadywac
        frakcje.append({**kol, "id": trafienie[0], "nazwa": trafienie[1]})
    if not frakcje:
        raise BladHarmonogramu("zaden naglowek kolumny nie pasuje do znanych frakcji")
    return kolumna_miesiecy, frakcje


def znajdz_wiersze(a: np.ndarray, kolumna_miesiecy: dict, y_dol_naglowka: int) -> list[tuple[int, int]]:
    """Wiersze miesiecy = pola miedzy bialymi separatorami w kolumnie miesiecy."""
    jasnosc = a.mean(axis=2)
    x0 = kolumna_miesiecy["x0"] + 3
    x1 = kolumna_miesiecy["x1"] - 3
    pasek = jasnosc[:, x0:x1]
    jasne = (pasek > 248).mean(axis=1) > 0.97

    separatory = []
    y = y_dol_naglowka
    H = a.shape[0]
    while y < H:
        if jasne[y]:
            start = y
            while y < H and jasne[y]:
                y += 1
            separatory.append((start, y))
        else:
            y += 1

    wiersze = []
    for i in range(len(separatory) - 1):
        gora, dol = separatory[i][1], separatory[i + 1][0]
        if separatory[i][1] - separatory[i][0] > 25 and wiersze:
            break  # gruba biel = tabela sie skonczyla
        wys = dol - gora
        if 15 <= wys <= 120:
            wiersze.append((gora, dol))
        elif wiersze:
            break
    if not wiersze:
        raise BladHarmonogramu("nie znaleziono wierszy miesiecy")
    return wiersze


# ---------------------------------------------------------------------------
# Krok 3: plamy atramentu w komorce
# ---------------------------------------------------------------------------

def _komponenty(maska: np.ndarray) -> list[tuple[int, int, int, int, int]]:
    """8-spojne plamy w masce. Zwraca (x0, x1, y0, y1, liczba_pikseli)."""
    h, w = maska.shape
    odwiedzone = np.zeros_like(maska, dtype=bool)
    plamy = []
    for i in range(h):
        for j in range(w):
            if not maska[i, j] or odwiedzone[i, j]:
                continue
            kolejka = deque([(i, j)])
            odwiedzone[i, j] = True
            piksele = []
            while kolejka:
                ci, cj = kolejka.popleft()
                piksele.append((ci, cj))
                for di in (-1, 0, 1):
                    for dj in (-1, 0, 1):
                        ni, nj = ci + di, cj + dj
                        if (0 <= ni < h and 0 <= nj < w
                                and maska[ni, nj] and not odwiedzone[ni, nj]):
                            odwiedzone[ni, nj] = True
                            kolejka.append((ni, nj))
            ys = [p[0] for p in piksele]
            xs = [p[1] for p in piksele]
            plamy.append((min(xs), max(xs), min(ys), max(ys), len(piksele)))
    return plamy


def plamy_w_komorce(jasnosc: np.ndarray, y0: int, y1: int, x0: int, x1: int):
    """Zwraca (wycinek jasnosci, lista plam) dla jednej komorki tabeli."""
    margines = 2
    wycinek = jasnosc[y0 + margines:y1 - margines, x0 + margines:x1 - margines]
    if wycinek.size == 0:
        return wycinek, []
    tlo = float(np.median(wycinek))
    prog = tlo - 90
    maska = wycinek < prog
    if not maska.any():
        return wycinek, []
    plamy = [p for p in _komponenty(maska)
             if p[4] >= 8 and not (p[3] - p[2] + 1 < 4 and p[1] - p[0] + 1 > 20)]
    return wycinek, sorted(plamy)


# ---------------------------------------------------------------------------
# Krok 4: rozpoznawanie znakow przez dopasowanie do wzorcow z czcionki
# ---------------------------------------------------------------------------

class Rozpoznawacz:
    """Dopasowuje wycinek obrazu do napisow wyrenderowanych z czcionki.

    Zadnego uczenia maszynowego: wzorce sa generowane z pliku czcionki,
    normalizowane do wspolnej wysokosci i porownywane piksel w piksel.
    """

    #: Wzorce renderujemy duzo wieksze niz docelowe i zmniejszamy z
    #: antyaliasingiem - im wyzsza rozdzielczosc renderu, tym wierniejszy
    #: kształt po zmniejszeniu (przy 120 px myliło sie 6 z 8).
    RENDER_PX = 160

    def __init__(self, sciezka_czcionki: str | None = None):
        self.sciezka = sciezka_czcionki or self._znajdz_czcionke()
        self.font = ImageFont.truetype(self.sciezka, self.RENDER_PX)
        self._cache: dict[str, np.ndarray] = {}

    @staticmethod
    def _znajdz_czcionke() -> str:
        for kandydat in CZCIONKI:
            if Path(kandydat).exists():
                return kandydat
        raise BladHarmonogramu(
            "nie znaleziono pogrubionej czcionki bezszeryfowej (Arial Bold / "
            "Liberation Sans Bold) - wskaz ja opcja --czcionka"
        )

    # -- normalizacja ------------------------------------------------------

    @staticmethod
    def _atrament(szare: np.ndarray) -> np.ndarray:
        """Obraz -> ilosc atramentu 0..1, przyciety do zawartosci."""
        ink = np.clip((255.0 - szare) / 255.0, 0, 1)
        ink = ink - ink.min()
        if ink.max() > 0:
            ink = ink / ink.max()
        znaczace = ink > 0.35
        if not znaczace.any():
            return np.zeros((1, 1))
        ys, xs = np.where(znaczace)
        return ink[ys.min():ys.max() + 1, xs.min():xs.max() + 1]

    @staticmethod
    def _sygnatura(ink: np.ndarray, wysokosc: int = WYS_POROWNANIA) -> np.ndarray:
        """Skaluje atrament do zadanej wysokosci, zachowujac proporcje."""
        h, w = ink.shape
        if h == 0 or w == 0:
            return np.zeros((wysokosc, 1))
        nowa_szer = max(1, int(round(w * wysokosc / h)))
        obraz = Image.fromarray((ink * 255).astype(np.uint8))
        obraz = obraz.resize((nowa_szer, wysokosc), Image.LANCZOS)
        return np.asarray(obraz).astype(np.float64) / 255.0

    def _wzorzec(self, tekst: str) -> np.ndarray:
        if tekst not in self._cache:
            pom = Image.new("L", (16, 16), 255)
            bbox = ImageDraw.Draw(pom).textbbox((0, 0), tekst, font=self.font)
            obraz = Image.new("L", (bbox[2] - bbox[0] + 60, bbox[3] - bbox[1] + 60), 255)
            ImageDraw.Draw(obraz).text((30 - bbox[0], 30 - bbox[1]), tekst,
                                       font=self.font, fill=0)
            self._cache[tekst] = self._sygnatura(self._atrament(np.asarray(obraz)))
        return self._cache[tekst]

    @staticmethod
    def _rozbieznosc(a: np.ndarray, b: np.ndarray) -> float:
        """Sredni blad kwadratowy po wyrownaniu szerokosci (do srodka)."""
        szer = max(a.shape[1], b.shape[1])

        def dopelnij(m):
            brak = szer - m.shape[1]
            lewo = brak // 2
            return np.pad(m, ((0, 0), (lewo, brak - lewo)))

        return float(np.mean((dopelnij(a) - dopelnij(b)) ** 2))

    # -- API ---------------------------------------------------------------

    def rozpoznaj(self, szare: np.ndarray, kandydaci: list[str]) -> tuple[str, float]:
        """Zwraca (najlepszy kandydat, margines przewagi nad drugim)."""
        sygnatura = self._sygnatura(self._atrament(szare))
        wyniki = sorted((self._rozbieznosc(sygnatura, self._wzorzec(k)), k)
                        for k in kandydaci)
        najlepszy, drugi = wyniki[0], wyniki[1] if len(wyniki) > 1 else None
        if drugi is None or najlepszy[0] <= 1e-9:
            return najlepszy[1], 1.0
        margines = (drugi[0] - najlepszy[0]) / drugi[0]
        return najlepszy[1], margines


# ---------------------------------------------------------------------------
# Krok 5: czytanie zawartosci komorek
# ---------------------------------------------------------------------------

def czytaj_dni(wycinek, plamy, rozp: Rozpoznawacz, ostrzezenia: list[str], gdzie: str) -> list[int]:
    """Odczytuje z komorki liste dni miesiaca (np. "1, 15, 29" -> [1, 15, 29])."""
    if not plamy:
        return []

    wysokosci = sorted(p[3] - p[2] + 1 for p in plamy)
    max_wys = wysokosci[-1]

    # przecinki: niskie plamy przy dolnej krawedzi tekstu
    tokeny, biezacy = [], []
    for p in plamy:
        wys = p[3] - p[2] + 1
        szer = p[1] - p[0] + 1
        czy_przecinek = wys <= 0.62 * max_wys and szer <= 0.75 * max_wys
        if czy_przecinek:
            if biezacy:
                tokeny.append(biezacy)
                biezacy = []
        else:
            biezacy.append(p)
    if biezacy:
        tokeny.append(biezacy)

    kandydaci = [str(d) for d in range(1, 32)]
    dni = []
    for token in tokeny:
        etykieta, margines = _rozpoznaj_liczbe(wycinek, token, rozp, kandydaci)
        if margines < MIN_MARGINES:
            ostrzezenia.append(
                f"{gdzie}: niepewne odczytanie \"{etykieta}\" "
                f"(margines {margines:.0%})"
            )
        dni.append(int(etykieta))

    # w tabeli dni w komorce sa wypisane rosnaco - inna kolejnosc znaczy,
    # ze ktoras cyfra zostala odczytana blednie
    if dni != sorted(dni):
        ostrzezenia.append(
            f"{gdzie}: dni nie sa rosnace ({', '.join(map(str, dni))}) "
            f"- prawdopodobny blad odczytu"
        )
    return dni


def _rozpoznaj_liczbe(wycinek, token, rozp: Rozpoznawacz,
                      kandydaci: list[str]) -> tuple[str, float]:
    """Odczytuje liczbe zapisana jako grupa plam.

    Jesli kazda cyfra jest osobna plama, czytamy je pojedynczo - porownanie
    jednej cyfry z dziesiecioma wzorcami rozstrzyga sie na calej powierzchni
    znaku, wiec jest znacznie pewniejsze niz dopasowanie calej liczby, gdzie
    np. "20" i "29" roznia sie tylko polowa obrazu. Zlepione cyfry (jedna
    szeroka plama) dopasowujemy jako caly napis.
    """
    rozdzielone = all(
        (p[1] - p[0] + 1) / max(1, p[3] - p[2] + 1) <= PROPORCJA_ZLEPIENIA
        for p in token
    )
    if rozdzielone:
        cyfry, marginesy = "", []
        for x0, x1, y0, y1, _ in token:
            etykieta, margines = rozp.rozpoznaj(wycinek[y0:y1 + 1, x0:x1 + 1], CYFRY)
            cyfry += etykieta
            marginesy.append(margines)
        if cyfry and str(int(cyfry)) in kandydaci:
            return str(int(cyfry)), min(marginesy)

    x0 = min(p[0] for p in token)
    x1 = max(p[1] for p in token)
    y0 = min(p[2] for p in token)
    y1 = max(p[3] for p in token)
    return rozp.rozpoznaj(wycinek[y0:y1 + 1, x0:x1 + 1], kandydaci)


def czytaj_miesiac(wycinek, plamy, rozp: Rozpoznawacz) -> tuple[str | None, float]:
    """Rozpoznaje nazwe miesiaca w lewej kolumnie tabeli."""
    if not plamy:
        return None, 0.0
    x0 = min(p[0] for p in plamy)
    x1 = max(p[1] for p in plamy)
    y0 = min(p[2] for p in plamy)
    y1 = max(p[3] for p in plamy)
    return rozp.rozpoznaj(wycinek[y0:y1 + 1, x0:x1 + 1], MIESIACE)


def czytaj_rok(a: np.ndarray, kolumna_miesiecy: dict, y_gora: int, y_dol: int,
               rozp: Rozpoznawacz) -> int | None:
    """Rok jest wpisany w naglowek kolumny miesiecy.

    Rok czytamy cyfra po cyfrze, a nie jako caly napis: przy porownywaniu
    calego "2026" z kandydatami rozniacymi sie jedna cyfra trzy czwarte obrazu
    jest identyczne, wiec roznice wynikow sa mikroskopijne i latwo o pomylke.
    """
    jasnosc = a.mean(axis=2)
    wycinek, plamy = plamy_w_komorce(jasnosc, y_gora, y_dol,
                                     kolumna_miesiecy["x0"], kolumna_miesiecy["x1"])
    if not plamy:
        return None

    cyfry = [str(c) for c in range(10)]
    if len(plamy) == 4:  # cztery osobne cyfry - najpewniejszy przypadek
        odczyt = ""
        for x0, x1, y0, y1, _ in plamy:
            etykieta, _margines = rozp.rozpoznaj(wycinek[y0:y1 + 1, x0:x1 + 1], cyfry)
            odczyt += etykieta
        if 2000 <= int(odczyt) <= 2100:
            return int(odczyt)

    # cyfry sa zlepione - dopasowujemy caly napis
    x0 = min(p[0] for p in plamy)
    x1 = max(p[1] for p in plamy)
    y0 = min(p[2] for p in plamy)
    y1 = max(p[3] for p in plamy)
    kandydaci = [str(r) for r in range(2020, 2041)]
    etykieta, _ = rozp.rozpoznaj(wycinek[y0:y1 + 1, x0:x1 + 1], kandydaci)
    return int(etykieta)


# ---------------------------------------------------------------------------
# Krok 6: warstwa tekstowa, skladanie wyniku, walidacja
# ---------------------------------------------------------------------------

RZYMSKIE = {"I": 1, "II": 2, "III": 3, "IV": 4, "V": 5, "VI": 6,
            "VII": 7, "VIII": 8, "IX": 9, "X": 10}


def czytaj_opis(tekst: str) -> dict:
    """Sektor i wykaz miejscowosci z warstwy tekstowej PDF."""
    sektor = numer = None
    dopasowanie = re.search(r"SEKTOR\s+([IVXLC]+|\d+)", tekst, re.IGNORECASE)
    if dopasowanie:
        sektor = dopasowanie.group(0).strip()
        oznaczenie = dopasowanie.group(1).upper()
        numer = int(oznaczenie) if oznaczenie.isdigit() else RZYMSKIE.get(oznaczenie)

    # Wykaz miejscowosci bywa zlamany na kilka linii - linia zakonczona
    # przecinkiem jest kontynuowana w nastepnej, wiec takie linie scalamy.
    bloki: list[list[str]] = []
    biezacy: list[str] = []
    for linia in tekst.splitlines():
        linia = linia.strip()
        if not linia or re.search(r"SEKTOR", linia, re.IGNORECASE):
            if biezacy:
                bloki.append(biezacy)
                biezacy = []
            continue
        if biezacy and not biezacy[-1].endswith(","):
            bloki.append(biezacy)
            biezacy = []
        biezacy.append(linia)
    if biezacy:
        bloki.append(biezacy)

    # Wykaz to najdluzszy blok tekstu poza naglowkiem z numerem sektora. Nie
    # wymagamy listy z przecinkami: sektory z zabudowa wielorodzinna maja tu
    # opis w rodzaju "Stare Juchy: Spoldzielnie i wspolnoty mieszkaniowe".
    wykaz = max((" ".join(b) for b in bloki), key=len, default="").strip()
    wykaz = wykaz.rstrip(".").strip()
    miejscowosci = [cz.strip() for cz in wykaz.split(",") if cz.strip()]
    return {
        "sektor": sektor,
        "numer_sektora": numer,
        "wykaz": wykaz or None,
        "miejscowosci": miejscowosci,
    }


def zwaliduj(odbiory: list[dict], ostrzezenia: list[str]) -> None:
    """Sprawdza sensownosc odczytu: staly dzien tygodnia i odstepy frakcji."""
    wg_frakcji: dict[str, list[dt.date]] = {}
    for wpis in odbiory:
        data = dt.date.fromisoformat(wpis["data"])
        for frakcja in wpis["frakcje"]:
            wg_frakcji.setdefault(frakcja, []).append(data)

    for frakcja, daty in wg_frakcji.items():
        daty.sort()
        # Odbiory jednej frakcji trzymaja sie tego samego dnia tygodnia, ale
        # swieta przesuwaja czesc terminow o dzien - dwa rozne dni sa wiec
        # normalne, trzy i wiecej sygnalizuja blad odczytu.
        dni_tyg = {d.weekday() for d in daty}
        if len(daty) >= 4 and len(dni_tyg) > 2:
            nazwy = ", ".join(sorted(DNI_TYGODNIA[d] for d in dni_tyg))
            ostrzezenia.append(
                f"frakcja \"{frakcja}\": odbiory wypadaja w {len(dni_tyg)} roznych "
                f"dniach tygodnia ({nazwy}) - warto sprawdzic odczyt"
            )
        odstepy = {(b - a).days for a, b in zip(daty, daty[1:])}
        dziwne = [o for o in odstepy if o < 3]
        if dziwne:
            ostrzezenia.append(
                f"frakcja \"{frakcja}\": podejrzanie male odstepy miedzy "
                f"odbiorami: {sorted(dziwne)} dni"
            )


def przetworz(sciezka: Path, rozp: Rozpoznawacz) -> dict:
    """Pelny odczyt jednego pliku PDF -> slownik gotowy do zapisania w JSON."""
    a, tekst = wczytaj_pdf(sciezka)
    jasnosc = a.mean(axis=2)
    ostrzezenia: list[str] = []

    y_naglowka_gora, y_naglowka_dol = znajdz_naglowek(a)
    kolumny = znajdz_kolumny(a, y_naglowka_gora, y_naglowka_dol)
    kolumna_miesiecy, kolumny_frakcji = przypisz_frakcje(kolumny)
    wiersze = znajdz_wiersze(a, kolumna_miesiecy, y_naglowka_dol + 1)

    if len(wiersze) != 12:
        ostrzezenia.append(
            f"znaleziono {len(wiersze)} wierszy miesiecy zamiast 12"
        )

    rok = czytaj_rok(a, kolumna_miesiecy, y_naglowka_gora, y_naglowka_dol, rozp)
    if rok is None:
        raise BladHarmonogramu("nie udalo sie odczytac roku z naglowka tabeli")

    # tabela: wiersz (miesiac) x kolumna (frakcja) -> lista dni
    tabela: list[dict] = []
    for indeks, (y0, y1) in enumerate(wiersze):
        wycinek, plamy = plamy_w_komorce(jasnosc, y0, y1,
                                         kolumna_miesiecy["x0"], kolumna_miesiecy["x1"])
        nazwa, _margines = czytaj_miesiac(wycinek, plamy, rozp)
        # Przy pelnych dwunastu wierszach kolejnosc miesiecy jest pewniejsza
        # niz odczyt slowa, wiec numer bierzemy z pozycji, a rozpoznana nazwa
        # sluzy tylko do kontroli. Przy niepelnej tabeli jest odwrotnie.
        if len(wiersze) == 12:
            numer = indeks + 1
            if nazwa and nazwa != MIESIACE[indeks]:
                ostrzezenia.append(
                    f"wiersz {indeks + 1}: w tabeli powinien byc "
                    f"\"{MIESIACE[indeks]}\", a napis odczytano jako \"{nazwa}\" "
                    f"- przyjmuje kolejnosc z tabeli"
                )
        elif nazwa:
            numer = MIESIACE.index(nazwa) + 1
        else:
            numer = indeks + 1
            ostrzezenia.append(
                f"wiersz {indeks + 1}: nie rozpoznano nazwy miesiaca "
                f"- przyjmuje {MIESIACE[indeks]} z kolejnosci"
            )

        komorki = {}
        for kolumna in kolumny_frakcji:
            wyc, pl = plamy_w_komorce(jasnosc, y0, y1, kolumna["x0"], kolumna["x1"])
            gdzie = f"{MIESIACE[numer - 1]}/{kolumna['nazwa']}"
            komorki[kolumna["id"]] = czytaj_dni(wyc, pl, rozp, ostrzezenia, gdzie)
        tabela.append({"miesiac": numer, "nazwa": MIESIACE[numer - 1], "komorki": komorki})

    # sklejamy daty
    wg_frakcji: dict[str, list[str]] = {k["id"]: [] for k in kolumny_frakcji}
    wg_daty: dict[str, list[str]] = {}
    for wiersz in tabela:
        for ident, dni in wiersz["komorki"].items():
            for dzien in dni:
                try:
                    data = dt.date(rok, wiersz["miesiac"], dzien)
                except ValueError:
                    ostrzezenia.append(
                        f"{wiersz['nazwa']}/{ident}: dzien {dzien} nie istnieje "
                        f"w tym miesiacu - pomijam"
                    )
                    continue
                iso = data.isoformat()
                wg_frakcji[ident].append(iso)
                wg_daty.setdefault(iso, []).append(ident)

    kolejnosc = [k["id"] for k in kolumny_frakcji]
    odbiory = [
        {
            "data": iso,
            "dzien_tygodnia": DNI_TYGODNIA[dt.date.fromisoformat(iso).weekday()],
            "frakcje": sorted(fr, key=kolejnosc.index),
        }
        for iso, fr in sorted(wg_daty.items())
    ]
    for lista in wg_frakcji.values():
        lista.sort()

    zwaliduj(odbiory, ostrzezenia)

    return {
        "plik": sciezka.name,
        "rok": rok,
        **czytaj_opis(tekst),
        "frakcje": [{"id": k["id"], "nazwa": k["nazwa"]} for k in kolumny_frakcji],
        "odbiory": odbiory,
        "wg_frakcji": wg_frakcji,
        "wg_miesiecy": [
            {"miesiac": w["miesiac"], "nazwa": w["nazwa"], "dni": w["komorki"]}
            for w in tabela
        ],
        "liczba_odbiorow": len(odbiory),
        "ostrzezenia": ostrzezenia,
    }


# ---------------------------------------------------------------------------
# Raport kontrolny
# ---------------------------------------------------------------------------

def wypisz_raport(wynik: dict) -> None:
    print(f"\n=== {wynik['plik']} ===")
    print(f"rok: {wynik['rok']}   sektor: {wynik['sektor']}")
    if wynik["wykaz"]:
        print("wykaz: " + wynik["wykaz"])

    frakcje = wynik["frakcje"]
    szer = 13
    print("\n" + "miesiac".ljust(12) + "".join(f["nazwa"][:szer - 1].ljust(szer) for f in frakcje))
    for wiersz in wynik["wg_miesiecy"]:
        linia = wiersz["nazwa"].ljust(12)
        for f in frakcje:
            dni = wiersz["dni"].get(f["id"], [])
            linia += (", ".join(map(str, dni)) or "-").ljust(szer)
        print(linia)
    print(f"\nodbiorow (dni kalendarzowych): {wynik['liczba_odbiorow']}")
    if wynik["ostrzezenia"]:
        print("\nOSTRZEZENIA:")
        for o in wynik["ostrzezenia"]:
            print("  - " + o)
    else:
        print("walidacja: bez zastrzezen")


# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Wyciaga harmonogram odbioru odpadow z PDF do JSON."
    )
    parser.add_argument("pliki", nargs="+", help="pliki PDF (obsluguje wzorce *.pdf)")
    parser.add_argument("-o", "--out", help="zapisz wszystko do jednego pliku JSON")
    parser.add_argument("--raport", action="store_true",
                        help="wypisz tabele kontrolna na ekran")
    parser.add_argument("--czcionka", help="sciezka do pogrubionej czcionki .ttf")
    args = parser.parse_args(argv)

    sciezki: list[Path] = []
    for wzorzec in args.pliki:
        trafienia = [Path(p) for p in glob.glob(wzorzec)] or [Path(wzorzec)]
        sciezki.extend(sorted(trafienia))

    rozp = Rozpoznawacz(args.czcionka)
    wyniki, bledy = [], 0
    for sciezka in sciezki:
        if not sciezka.exists():
            print(f"BLAD {sciezka}: plik nie istnieje", file=sys.stderr)
            bledy += 1
            continue
        try:
            wynik = przetworz(sciezka, rozp)
        except BladHarmonogramu as e:
            print(f"BLAD {sciezka.name}: {e}", file=sys.stderr)
            bledy += 1
            continue
        except Exception as e:  # zly plik nie moze przerwac calej partii
            print(f"BLAD {sciezka.name}: nieoczekiwany problem - "
                  f"{type(e).__name__}: {e}", file=sys.stderr)
            bledy += 1
            continue
        wyniki.append(wynik)
        if args.raport:
            wypisz_raport(wynik)
        if not args.out:
            cel = sciezka.with_suffix(".json")
            cel.write_text(json.dumps(wynik, ensure_ascii=False, indent=2),
                           encoding="utf-8")
            print(f"{sciezka.name} -> {cel.name}"
                  f"  ({wynik['liczba_odbiorow']} odbiorow"
                  f"{', ' + str(len(wynik['ostrzezenia'])) + ' ostrzezen' if wynik['ostrzezenia'] else ''})")

    if args.out and wyniki:
        dane = wyniki[0] if len(wyniki) == 1 else {"dokumenty": wyniki}
        Path(args.out).write_text(json.dumps(dane, ensure_ascii=False, indent=2),
                                  encoding="utf-8")
        print(f"zapisano {args.out} ({len(wyniki)} dok.)")

    return 1 if bledy else 0


if __name__ == "__main__":
    sys.exit(main())
