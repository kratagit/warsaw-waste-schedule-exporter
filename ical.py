"""Budowanie plikow iCalendar (.ics) zgodnych z RFC 5545 i Google Calendar.

Uzywaja tego oba harmonogramy - warszawski i jeziorowskie. Modul zna sie
wylacznie na formacie; co jest wydarzeniem, decyduje strona wywolujaca.
"""

import datetime

POLSKIE_ZNAKI = str.maketrans({
    "ą": "a", "ć": "c", "ę": "e", "ł": "l", "ń": "n",
    "ó": "o", "ś": "s", "ź": "z", "ż": "z",
})


def escapuj(tekst: str) -> str:
    """Escapuje znaki specjalne dla pol tekstowych (SUMMARY, DESCRIPTION)."""
    if not tekst:
        return ""
    tekst = str(tekst).replace("\\", r"\\")
    tekst = tekst.replace(";", r"\;")
    tekst = tekst.replace(",", r"\,")
    tekst = tekst.replace("\r\n", r"\n").replace("\n", r"\n").replace("\r", r"\n")
    return tekst


def zawin(linia: str) -> str:
    """Zawija wiersz zgodnie z RFC 5545 (maksymalnie 75 oktetow na wiersz)."""
    b = linia.encode("utf-8")
    if len(b) <= 75:
        return linia
    czesci = []
    while len(b) > 75:
        podzial = 75
        while podzial > 0:
            try:
                b[:podzial].decode("utf-8")
                break
            except UnicodeDecodeError:
                podzial -= 1
        czesci.append(b[:podzial].decode("utf-8"))
        b = b" " + b[podzial:]
    if b:
        czesci.append(b.decode("utf-8"))
    return "\r\n".join(czesci)


def slug(tekst: str) -> str:
    """Zamienia nazwe frakcji na bezpieczny fragment identyfikatora UID."""
    tekst = (tekst or "").lower().translate(POLSKIE_ZNAKI)
    return "".join(z if z.isalnum() else "-" for z in tekst).strip("-")


#: Jak czesto klient ma odpytywac kanal. Google to ignoruje i odswieza po
#: swojemu (nawet do doby), ale Apple Calendar, Thunderbird i Outlook - nie.
ODSWIEZANIE = "PT6H"


def zbuduj(nazwa_kalendarza: str, prodid: str, wydarzenia: list[dict],
           sekwencja: int = 0) -> str:
    """Skleja kompletny plik .ics z listy wydarzen calodniowych.

    Kazde wydarzenie to slownik: uid, data (datetime.date), summary, opis
    oraz opcjonalne "odwolane".

    Odfiltrowane frakcje NIE sa pomijane, tylko oznaczane jako odwolane
    (STATUS:CANCELLED). Samo znikniecie wpisu z kanalu jest dla czytnikow slaba
    przeslanka - Google potrafi trzymac takie wydarzenia jeszcze dlugo. Jawne
    odwolanie to polecenie, nie domysl.

    Args:
        sekwencja: numer wersji nadawany wszystkim wydarzeniom. Musi rosnac przy
                   kazdej publikacji, inaczej czytnik moze zignorowac zmiane -
                   zarowno odwolanie, jak i pozniejsze przywrocenie frakcji.
    """
    now_utc = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    linie = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:{prodid}",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{escapuj(nazwa_kalendarza)}",
        "X-WR-TIMEZONE:Europe/Warsaw",
        f"REFRESH-INTERVAL;VALUE=DURATION:{ODSWIEZANIE}",
        f"X-PUBLISHED-TTL:{ODSWIEZANIE}",
    ]

    for w in wydarzenia:
        start = w["data"]
        koniec = start + datetime.timedelta(days=1)
        linie.extend([
            "BEGIN:VEVENT",
            f"UID:{w['uid']}",
            f"DTSTAMP:{now_utc}",
            f"SEQUENCE:{sekwencja}",
            f"DTSTART;VALUE=DATE:{start.strftime('%Y%m%d')}",
            f"DTEND;VALUE=DATE:{koniec.strftime('%Y%m%d')}",
            f"SUMMARY:{escapuj(w['summary'])}",
            f"DESCRIPTION:{escapuj(w.get('opis', ''))}",
            "STATUS:CANCELLED" if w.get("odwolane") else "STATUS:CONFIRMED",
            "TRANSP:TRANSPARENT",
            "END:VEVENT",
        ])

    linie.append("END:VCALENDAR")
    return "\r\n".join(zawin(l) for l in linie) + "\r\n"
