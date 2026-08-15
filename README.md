# ♻️ Warsaw Waste Schedule Exporter

Aplikacja webowa (Flask), która automatyzuje pobieranie harmonogramu wywozu odpadów ze strony **Warszawa 19115**, przetwarza pobrany plik PDF (nakłada etykiety na ikony) i synchronizuje terminy z **Kalendarzem Google**.

Aplikacja jest przystosowana do działania na domowym serwerze (np. Proxmox) w kontenerze **Docker**.

## 🚀 Funkcje

*   **Automatyczny Scraping:** Wchodzi na stronę 19115, wpisuje adres i pobiera harmonogram.
*   **Analiza PDF:** Pobiera PDF, analizuje kolory pikseli w kalendarzu i tworzy nową wersję pliku z czytelnymi podpisami (np. "PAPIER", "SZKŁO").
*   **Google Calendar Sync:** Dodaje wydarzenia do kalendarza "Wywóz Śmieci" (z odpowiednimi kolorami i powiadomieniami).
*   **Automat:** Działa w tle i codziennie rano sprawdza, czy wczoraj był odbiór – jeśli tak, pobiera nowy harmonogram (dla aktualizacji danych na przyszłość).
*   **Nowoczesne UI:** Tryb ciemny (Dark Mode), pasek postępu w czasie rzeczywistym, animacje kafelków.
*   **Docker:** Łatwe wdrożenie i izolacja środowiska (Selenium + Chrome w kontenerze).
*   **Przełącznik regionu:** Warszawa (19115) albo Jeziorowskie / gmina Stare Juchy (KOMA) – patrz niżej.

---

## 🌲 Jeziorowskie (gmina Stare Juchy)

Drugi region, przełączany przyciskiem w nagłówku. Firma KOMA publikuje harmonogram
jako **PDF, w którym tabela jest obrazkiem** – nie ma tam warstwy tekstowej z datami.
Dlatego terminy odczytuje moduł `koma_parser.py`, w całości lokalnie i deterministycznie:
**bez OCR, bez AI i bez usług zewnętrznych**.

Jak to działa:

1.  Z PDF-a wyciągany jest wbudowany obrazek.
2.  Kolumny tabeli znajdowane są po kolorach nagłówków (czarny = Zmieszane, brązowy = Bio,
    żółty = Metale i tworzywa, niebieski = Papier, zielony = Szkło, szare = Gabaryty i Popiół),
    a wiersze po białych separatorach w kolumnie miesięcy.
3.  W komórkach wyszukiwane są spójne plamy atramentu; przecinki dzielą je na liczby.
4.  Cyfry rozpoznawane są przez porównanie z wzorcami **renderowanymi z czcionki systemowej**
    (Arial Bold) – piksel w piksel.
5.  Odczyt jest sprawdzany: rosnące dni w komórce, zgodność nazwy miesiąca z pozycją wiersza,
    stały dzień tygodnia dla frakcji i sensowne odstępy. Zastrzeżenia trafiają do pola
    `ostrzezenia` i są pokazywane w panelu.

Odczyt zweryfikowano ręcznie, komórka po komórce, dla sektorów I, II i III na rok 2026 –
**252 komórki, zero rozbieżności**.

**Wzorce znaków są dołączone do repozytorium** (`wzorce.npz`, ok. 60 kB). To istotne dla
Dockera: w obrazie nie ma Arial Bold, a zamienniki (Liberation, Arimo) rysują cyfry
inaczej i odczyt się sypie – „11, 25” było czytane jako „31, 26”. Plik wzorców odwiązuje
wynik od czcionek zainstalowanych w systemie; sprawdzone, że z Arialem, z samym
Liberation i bez żadnej czcionki wynik jest identyczny. Wzorce odtwarza się poleceniem:

```bash
python koma_parser.py --generuj-wzorce        # wymaga Arial Bold w systemie
```

Panel pozwala wybrać sektor, obejrzeć najbliższe odbiory i cały rok w tabeli, otworzyć
źródłowy PDF, ponownie odczytać PDF-y (przycisk *Odczytaj PDF*) oraz wysłać terminy do
Google Calendar. Terminy Jeziorowskich trafiają do **osobnego kalendarza**
„Wywóz Śmieci (Jeziorowskie)”, więc nie mieszają się z warszawskimi.

### Dane (lokalne, poza repozytorium)

Katalog `data/` jest w `.gitignore` – harmonogramy **nie trafiają do repozytorium**.
Trzeba je mieć lokalnie:

```text
data/jeziorowskie/
├── sektor-1.json          # odczytany harmonogram
├── sektor-2.json
├── sektor-3.json
└── pdf/
    └── Harmonogram sektor N.pdf    # pliki źródłowe z KOMA
```

Wystarczy wrzucić PDF-y do `data/jeziorowskie/pdf/` i kliknąć **Odczytaj PDF** w panelu
(albo wywołać `POST /api/jeziorowskie/reparse`) – pliki `sektor-*.json` wygenerują się same.
Nazwa pliku musi zawierać numer sektora, np. `Harmonogram sektor 2.pdf`.

Gdy katalogu nie ma, panel po prostu zgłasza brak danych – reszta aplikacji działa normalnie.
Pobieranie harmonogramów wprost ze strony KOMA nie jest jeszcze zrobione.

---

## 🛠️ Wymagania

*   Serwer z zainstalowanym **Docker** i **Docker Compose**.
*   Konto Google (do utworzenia projektu w Google Cloud Console).
*   Plik `credentials.json` (instrukcja poniżej).

---

## 🔑 Konfiguracja Google Cloud (Kluczowe!)

Aby logowanie działało na Twoim serwerze, musisz poprawnie skonfigurować projekt Google.

1.  Wejdź na [Google Cloud Console](https://console.cloud.google.com/apis/credentials).
2.  Utwórz nowy projekt.
3.  Włącz bibliotekę **Google Calendar API**.
4.  W zakładce **OAuth consent screen**:
    *   Ustaw typ na **External**.
    *   Po uzupełnieniu danych, w sekcji "Publishing status" kliknij **PUBLISH APP** (Opublikuj aplikację). *To ważne, aby token nie wygasał co 7 dni!*
5.  W zakładce **Credentials**:
    *   Kliknij **Create Credentials** -> **OAuth Client ID**.
    *   Typ aplikacji: **Web application**.
    *   W polu **Authorized redirect URIs** musisz wpisać adres swojego serwera z końcówką `.nip.io` (wymóg HTTPS) oraz ścieżką callbacka.
    
    **Format:**
    ```text
    https://192.168.X.X.nip.io:5000/oauth2callback
    ```
    *(Zamień `192.168.X.X` na IP swojego serwera w sieci lokalnej).*

6.  Pobierz plik JSON, zmień jego nazwę na `credentials.json` i zachowaj go. **Nie wrzucaj go do repozytorium!**

---

## 🐳 Instalacja i Uruchomienie (Docker)

Obraz budowany jest automatycznie przy każdym wrzuceniu zmian na gałąź `main`
(GitHub Actions → `ghcr.io/kratagit/warsaw-waste-schedule-exporter:latest`),
więc na serwerze nic się nie kompiluje – wystarczy pobrać gotowy obraz.

### 1. Pobranie kodu
Zaloguj się na serwer i sklonuj repozytorium:
```bash
git clone https://github.com/kratagit/warsaw-waste-schedule-exporter.git waste_app
cd waste_app
```

### 2. Wgranie kluczy
Prześlij plik `credentials.json` (pobrany w poprzednim kroku) do podkatalogu `data/`:

```bash
mkdir -p data
# skopiuj credentials.json do waste_app/data/
```

Cały stan aplikacji – klucze, token logowania, ostatni harmonogram i pliki
Jeziorowskich – leży w `data/`, dzięki czemu przeżywa aktualizacje obrazu.

### 3. Uruchomienie kontenera

```bash
docker compose up -d
```

Jeśli obraz jest prywatny, najpierw zaloguj się do rejestru
(`docker login ghcr.io -u TWOJA_NAZWA`, hasło = token GitHuba z prawem
`read:packages`). Chcesz budować lokalnie zamiast pobierać gotowy obraz?
W `docker-compose.yml` zakomentuj `image:` i odkomentuj `build: .`.

### 4. Pierwsze logowanie
Otwórz przeglądarkę i wejdź na adres (pamiętaj o `https` i `nip.io`!):

👉 **`https://192.168.X.X.nip.io:5000`**

1.  Zobaczysz ostrzeżenie o certyfikacie ("Połączenie nie jest prywatne") – to normalne, ponieważ generujemy certyfikat lokalnie. Kliknij **Zaawansowane -> Przejdź do strony**.
2.  Kliknij przycisk **"Połącz z Google Calendar"**.
3.  Zaloguj się na swoje konto Google.
4.  Gotowe! Plik sesji `token.pickle` zostanie utworzony automatycznie na serwerze.

---

## 🔄 Jak aktualizować aplikację?

Po wrzuceniu zmian na `main` GitHub Actions sam buduje nowy obraz i publikuje go
jako `:latest` (oraz `:sha-…` dla konkretnego commita). Postęp widać w zakładce
**Actions**. Na serwerze wystarczy:

```bash
cd waste_app
docker compose pull      # pobierz świeży obraz
docker compose up -d     # zrestartuj kontener na nowym obrazie
```

Dane z `data/` i `static/` są montowane wolumenami, więc logowanie do Google i
pobrane harmonogramy zostają na miejscu. Wycofanie do wcześniejszej wersji:
podmień w `docker-compose.yml` tag `:latest` na `:sha-<skrót commita>`.

---

## 📂 Struktura plików (Dla przypomnienia)

*   `app.py` - Główny kod aplikacji (Flask, Selenium logic, Google API).
*   `templates/index.html` - Frontend (HTML, TailwindCSS, JS).
*   `Dockerfile` - Przepis na system (Python 3.11 + Chrome + Sterowniki + Czcionki).
*   `docker-compose.yml` - Konfiguracja uruchamiania kontenera i mapowania wolumenów.
*   `requirements.txt` - Lista bibliotek Python (wersja czysta, bez śmieci z Windowsa).
*   `credentials.json` - **(Ignorowany przez git)** Twój klucz z Google Cloud.
*   `token.pickle` - **(Ignorowany przez git)** Plik sesji generowany po zalogowaniu.
*   `last_state.json` - **(Ignorowany przez git)** Plik zapamiętujący ostatni wynik i ustawienia automatu.
*   `static/` - Folder, do którego pobierany jest PDF.

---

## ⚠️ Rozwiązywanie problemów

1.  **Błąd "Not Found /oauth2callback" po logowaniu:**
    *   Sprawdź, czy w Google Cloud Console wpisałeś DOKŁADNIE ten sam adres URI, którego używasz w przeglądarce (musi być `https`, musi być `nip.io`, musi być port `:5000`).

2.  **Aplikacja mieli "Ładowanie..." na przycisku:**
    *   Prawdopodobnie brak pliku `credentials.json` na serwerze. Sprawdź logi:
    ```bash
    docker compose logs -f --tail=50
    ```

3.  **Błąd "SessionNotCreatedException" (Selenium):**
    *   Wersja Chrome w kontenerze nie zgadza się ze sterownikiem. Rozwiązanie: Przebuduj kontener (`docker compose up -d --build`), `Dockerfile` w tym projekcie automatycznie pobiera pasujące wersje z repozytorium Debiana.

4.  **Token wygasa po 7 dniach:**
    *   Nie kliknąłeś "Publish App" w Google Cloud Console (OAuth consent screen). Zmień status na "Production".
