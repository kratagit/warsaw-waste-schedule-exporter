import os
import sys
import time
import datetime
import pickle
import json
import glob
import threading
import math
import fitz  # PyMuPDF
from flask import Flask, render_template, request, jsonify, redirect, url_for, session, send_from_directory, Response

# Harmonogram gminy Stare Juchy (Jeziorowskie) - modul niezalezny od czesci warszawskiej
import jeziorowskie

# Selenium
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
#from webdriver_manager.chrome import ChromeDriverManager

# Google Calendar API
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build

app = Flask(__name__)
# KLUCZOWE DLA LOGOWANIA:
app.secret_key = 'bardzo_tajny_klucz_sesji_zmien_mnie_na_losowy_ciag'
os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1' # Pozwala na logowanie bez HTTPS (lokalnie)

# --- KONFIGURACJA ---
TARGET_URL = "https://warszawa19115.pl/harmonogramy-wywozu-odpadow"
SCOPES = ['https://www.googleapis.com/auth/calendar']
CALENDAR_NAME = "Wywóz Śmieci"
# Sciezki plikow ze stanem mozna przestawic zmiennymi srodowiskowymi - w
# kontenerze wskazujemy na /app/data, zeby przezyly aktualizacje obrazu.
# Bez zmiennych zachowanie jest takie jak dotad (pliki obok app.py).
CREDENTIALS_FILE = os.environ.get("CREDENTIALS_FILE", "credentials.json")
TOKEN_FILE = os.environ.get("TOKEN_FILE", "token.pickle")
STATE_FILE = os.environ.get("STATE_FILE", "last_state.json")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, 'static')
if not os.path.exists(STATIC_DIR):
    os.makedirs(STATIC_DIR)

MONTH_MAP = {
    'stycznia': 1, 'lutego': 2, 'marca': 3, 'kwietnia': 4, 'maja': 5, 'czerwca': 6,
    'lipca': 7, 'sierpnia': 8, 'września': 9, 'października': 10, 'listopada': 11, 'grudnia': 12,
    'styczeń': 1, 'luty': 2, 'marzec': 3, 'kwiecień': 4, 'maj': 5, 'czerwiec': 6,
    'lipiec': 7, 'sierpień': 8, 'wrzesień': 9, 'październik': 10, 'listopad': 11, 'grudzień': 12
}

WASTE_COLORS = {
    "Papier": "7", 
    "Metale i tworzywa sztuczne": "5", 
    "Szkło": "10",
    "Bio": "8", 
    "Zmieszane": "8", 
    "Zielone": "2"
}

# --- GLOBALNY STAN POSTĘPU ---
progress_state = {
    "status": "idle",
    "percent": 0,
    "message": "Gotowy",
    "result": None
}
progress_lock = threading.Lock()

#: Gdzie szukamy przegladarki na Windows. Chrome ma pierwszenstwo, ale gdy go
#: nie ma, wystarczy Edge - to tez Chromium, wiec scraping dziala tak samo.
CHROME_PATHS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.join(os.environ.get("LOCALAPPDATA", ""), r"Google\Chrome\Application\chrome.exe"),
]
EDGE_PATHS = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]


def find_browser_binary(paths):
    """Zwraca pierwsza istniejaca sciezke do przegladarki albo None."""
    for p in paths:
        if p and os.path.exists(p):
            return p
    return None


def build_browser_options(options_class):
    """Buduje te same opcje uruchomienia dla Chrome i dla Edge."""
    o = options_class()
    o.add_argument("--headless=new")
    o.add_argument("--window-size=1920,1080")
    o.add_argument("--log-level=3")
    o.add_argument("--no-sandbox")
    o.add_argument("--disable-dev-shm-usage")
    o.add_argument("--disable-gpu")
    prefs = {"download.default_directory": STATIC_DIR, "download.prompt_for_download": False, "plugins.always_open_pdf_externally": True}
    o.add_experimental_option("prefs", prefs)
    return o


def safe_print(msg):
    """Wypisuje komunikat tak, by polskie znaki nie wywrocily procesu.

    Gdy stdout nie jest terminalem (przekierowanie do pliku, usluga w tle),
    Windows koduje wyjscie w cp1252 i print z "BŁĄD" rzuca UnicodeEncodeError.
    Taki wyjatek potrafil zabic watek w trakcie obslugi bledu i zostawic
    aplikacje na zawsze w stanie "running" - z paskiem postepu, ktory nie rusza.
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


def update_progress(percent, message, status="running"):
    with progress_lock:
        progress_state["percent"] = percent
        progress_state["message"] = message
        progress_state["status"] = status

def reset_progress():
    with progress_lock:
        progress_state["percent"] = 0
        progress_state["message"] = "Inicjalizacja..."
        progress_state["status"] = "running"
        progress_state["result"] = None

# --- LOGIKA PDF I HELPERY ---

def color_distance(c1, c2):
    return math.sqrt(sum((a - b) ** 2 for a, b in zip(c1, c2)))

def find_matching_fraction(pix, bbox, legend):
    start_x = max(0, int(bbox.x0) + 2)
    end_x = min(pix.width, int(bbox.x1) - 2)
    start_y = max(0, int(bbox.y0) + 2)
    end_y = min(pix.height, int(bbox.y1) - 2)
    if start_x >= end_x or start_y >= end_y: return None
    for x in range(start_x, end_x, 2):
        for y in range(start_y, end_y, 2):
            pixel_color = pix.pixel(x, y)
            if sum(pixel_color) > 700: continue 
            for item in legend:
                if color_distance(pixel_color, item["color"]) < 45: return item["name"]
    return None

def process_pdf_labels(input_pdf_path, output_pdf_path):
    try:
        doc = fitz.open(input_pdf_path)
        FONT_SIZE = 10
        font_path = "C:/Windows/Fonts/arialbd.ttf"
        if not os.path.exists(font_path): font_path = "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"
        custom_font = None
        use_custom_font = False
        if os.path.exists(font_path):
            try: custom_font = fitz.Font(fontfile=font_path); use_custom_font = True
            except: pass
        legend = [
            {"name": "ZIELONE", "color": (83, 88, 90)}, {"name": "ZMIESZANE", "color": (33, 35, 35)},
            {"name": "PAPIER", "color": (0, 95, 170)}, {"name": "SZKŁO", "color": (45, 160, 45)},
            {"name": "PLASTIK", "color": (255, 205, 0)}, {"name": "PLASTIK", "color": (245, 170, 0)},
            {"name": "PLASTIK", "color": (230, 150, 0)}, {"name": "SKIP", "color": (140, 90, 60)}, 
            {"name": "SKIP", "color": (110, 70, 40)}, {"name": "SKIP", "color": (230, 90, 20)}, 
        ]
        for page in doc:
            pix = page.get_pixmap()
            images = page.get_image_info(xrefs=True)
            legend_y = page.rect.height * 0.9
            writer = fitz.TextWriter(page.rect)
            page_icons = []
            for img in images:
                bbox = fitz.Rect(img['bbox'])
                if bbox.width < 8 or bbox.width > 80: continue
                if bbox.y0 > legend_y: continue
                lbl = find_matching_fraction(pix, bbox, legend)
                if lbl: page_icons.append({"rect": bbox, "label": lbl})
            for icon in page_icons:
                if icon["label"] == "SKIP": continue
                txt = icon["label"]
                tlen = custom_font.text_length(txt, fontsize=FONT_SIZE) if use_custom_font else fitz.get_text_length(txt, fontsize=FONT_SIZE, fontname="Helvetica-Bold")
                right = icon["rect"].x0 - 5
                collision = True
                while collision:
                    collision = False
                    trect = fitz.Rect(right - tlen, icon["rect"].y0, right, icon["rect"].y1)
                    for obs in page_icons:
                        if obs is icon: continue
                        if trect.intersects(obs["rect"]):
                            right = obs["rect"].x0 - 5; collision = True; break
                fx, fy = right - tlen, icon["rect"].y1 - 2
                if use_custom_font: writer.append((fx, fy), txt, font=custom_font, fontsize=FONT_SIZE)
                else: page.insert_text((fx, fy), txt, fontsize=FONT_SIZE, fontname="Helvetica-Bold", color=(0,0,0))
            if use_custom_font: writer.write_text(page, color=(0, 0, 0))
        doc.save(output_pdf_path)
        doc.close()
        return True
    except: return False

def parse_polish_date(date_text):
    try:
        parts = date_text.lower().split()
        if len(parts) < 2: return None
        day = int(parts[0])
        month = MONTH_MAP.get(parts[1])
        if not month: return None
        now = datetime.datetime.now()
        year = now.year
        if now.month >= 11 and month <= 2: year += 1
        return datetime.date(year, month, day)
    except: return None

# --- AUTH & GOOGLE ---

def get_google_creds():
    creds = None
    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE, 'rb') as token: creds = pickle.load(token)
    
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                with open(TOKEN_FILE, 'wb') as token: pickle.dump(creds, token)
            except Exception as e:
                print(f"Błąd odświeżania tokenu: {e}")
                return None
        else:
            return None
    return creds

def get_google_service():
    creds = get_google_creds()
    if not creds: return None
    return build('calendar', 'v3', credentials=creds)

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, 'r', encoding='utf-8') as f: return json.load(f)
        except: pass
    return {"schedule": [], "logs": [], "pdf_available": False, "pdf_labeled_available": False, "auto_mode": False, "last_auto_run": "", "saved_address": ""}

def save_state(state):
    try:
        with open(STATE_FILE, 'w', encoding='utf-8') as f: json.dump(state, f, ensure_ascii=False)
    except: pass

# --- PROCES SYNCHRONIZACJI ---

def send_schedule_to_calendar(service_google, schedule_data, allowed_types, log,
                              progress_start=75, progress_end=95):
    """Wysyla terminy do kalendarza Google i zwraca liczbe dodanych wydarzen.

    Wydzielone z run_full_process, zeby ta sama logika obslugiwala dwa przypadki:
    pobranie danych razem z wysylka oraz sama wysylke wczesniej pobranych danych.
    """
    update_progress(progress_start, "Wysyłanie do Kalendarza...")
    cal_id = None
    page_token = None
    while True:
        clist = service_google.calendarList().list(pageToken=page_token).execute()
        for e in clist['items']:
            if e['summary'] == CALENDAR_NAME: cal_id = e['id']; break
        if cal_id: break
        page_token = clist.get('nextPageToken')
        if not page_token: break

    if not cal_id:
        cal_id = service_google.calendars().insert(body={'summary': CALENDAR_NAME, 'timeZone': 'Europe/Warsaw'}).execute()['id']
        log("Utworzono nowy kalendarz.")

    now_iso = datetime.datetime.utcnow().isoformat() + 'Z'
    existing = service_google.events().list(calendarId=cal_id, timeMin=now_iso, singleEvents=True).execute().get('items', [])

    count = 0
    rozpietosc = max(1, progress_end - progress_start)
    for i, (date_text, waste_type) in enumerate(schedule_data):
        update_progress(progress_start + int((i/len(schedule_data))*rozpietosc), f"Wysyłanie: {waste_type}...")
        if waste_type not in allowed_types:
            log(f" -> Pominięto (filtr): {waste_type}")
            continue
        edate = parse_polish_date(date_text)
        if not edate: continue
        estr = edate.isoformat()
        summary = f"Odbiór: {waste_type}"

        dup = False
        for ev in existing:
            if ev.get('start', {}).get('date') == estr and ev.get('summary') == summary: dup = True; break

        if not dup:
            body = {
                'summary': summary, 'start': {'date': estr}, 'end': {'date': estr},
                'colorId': WASTE_COLORS.get(waste_type, "8"), 'transparency': 'transparent',
                'reminders': {'useDefault': False, 'overrides': [
                    {'method': 'popup', 'minutes': 300},
                    {'method': 'email', 'minutes': 300}
                ]}
            }
            service_google.events().insert(calendarId=cal_id, body=body).execute()
            log(f" -> DODANO: {waste_type} ({estr})")
            count += 1
        else:
            log(f" -> Duplikat: {waste_type}")
    return count


def push_state_to_calendar(allowed_types):
    """Wysyla do kalendarza harmonogram juz pobrany do aplikacji (bez scrapingu)."""
    state = load_state()
    results = dict(state)
    results["logs"] = []
    results["status"] = "success"
    results.pop("message", None)

    def log(msg):
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        safe_print(f"[{ts}] {msg}")
        results["logs"].append(f"[{ts}] {msg}")

    try:
        service_google = get_google_service()
        if not service_google:
            raise Exception("Brak autoryzacji Google. Kliknij 'Połącz z Google' w panelu.")

        schedule_data = [(item["dateText"], item["wasteType"]) for item in state.get("schedule", [])]
        if not schedule_data:
            raise Exception("Brak pobranych danych. Najpierw kliknij 'Synchronizuj'.")

        update_progress(10, "Łączenie z Kalendarzem Google...")
        log(f"--- WYSYŁKA DO KALENDARZA: {len(schedule_data)} terminów ---")
        count = send_schedule_to_calendar(service_google, schedule_data, allowed_types, log,
                                          progress_start=20, progress_end=95)

        results["added_events"] = count
        results["allowed_types"] = allowed_types
        results['timestamp'] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log(f"--- SUKCES: Dodano {count} wydarzeń ---")
        save_state(results)

        with progress_lock:
            progress_state["percent"] = 100
            progress_state["message"] = "Zakończono pomyślnie!"
            progress_state["status"] = "finished"
            progress_state["result"] = results
    except Exception as e:
        log(f"BŁĄD: {str(e)}")
        with progress_lock:
            progress_state["status"] = "error"
            progress_state["message"] = str(e)
            results["status"] = "error"
            results["message"] = str(e)
            progress_state["result"] = results
        save_state(results)


def run_full_process(address, allowed_types, send_to_calendar=True):
    current_state = load_state()
    results = {
        "status": "success", "logs": [], "added_events": 0, "schedule": [],
        "pdf_available": False, "pdf_labeled_available": False,
        "auto_mode": current_state.get("auto_mode", False),
        "last_auto_run": current_state.get("last_auto_run", ""),
        "saved_address": address,
        "allowed_types": allowed_types
    }
    def log(msg):
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        safe_print(f"[{ts}] {msg}")
        results["logs"].append(f"[{ts}] {msg}")

    try:
        # Autoryzacja Google jest potrzebna tylko wtedy, gdy od razu wysylamy
        # terminy do kalendarza. Samo pobranie danych ze strony dziala bez logowania.
        service_google = None
        if send_to_calendar:
            service_google = get_google_service()
            if not service_google:
                 raise Exception("Brak autoryzacji Google. Kliknij 'Połącz z Google' w panelu.")

        update_progress(5, "Start przeglądarki...")
        log(f"--- START DLA: {address} ---")
        
        chrome_options = build_browser_options(Options)

        # --- ZMIANA: WARUNKOWE UŻYCIE STEROWNIKA ---
        # Na Dockerze (Linux) używamy systemowego. Na Windowsie - Webdriver Manager.
        if os.path.exists("/usr/bin/chromium") and os.path.exists("/usr/bin/chromedriver"):
            chrome_options.binary_location = "/usr/bin/chromium"
            service = Service("/usr/bin/chromedriver")
            log("Używam systemowego Chromium (Docker/Linux).")
            driver = webdriver.Chrome(service=service, options=chrome_options)
        else:
            chrome_path = find_browser_binary(CHROME_PATHS)
            edge_path = find_browser_binary(EDGE_PATHS)
            if not chrome_path and not edge_path:
                raise Exception(
                    "Nie znaleziono przeglądarki. Zainstaluj Google Chrome "
                    "(winget install Google.Chrome) albo Microsoft Edge."
                )

            if chrome_path:
                chrome_options.binary_location = chrome_path
                # Importujemy tylko tutaj, żeby Docker nie wywalił błędu przy starcie
                try:
                    from webdriver_manager.chrome import ChromeDriverManager
                    service = Service(ChromeDriverManager().install())
                    log("Używam Chrome + Webdriver Manager (Windows/Local).")
                    driver = webdriver.Chrome(service=service, options=chrome_options)
                except ImportError:
                    # Selenium 4.6+ samo pobiera sterownik (Selenium Manager)
                    log("Brak webdriver-manager - używam wbudowanego Selenium Manager.")
                    driver = webdriver.Chrome(options=chrome_options)
            else:
                # Edge jest oparty na Chromium, więc scraping działa tak samo,
                # a na Windowsie jest dostępny bez instalowania czegokolwiek.
                from selenium.webdriver.edge.options import Options as EdgeOptions
                edge_options = build_browser_options(EdgeOptions)
                edge_options.binary_location = edge_path
                log("Nie znaleziono Chrome - używam Microsoft Edge.")
                driver = webdriver.Edge(options=edge_options)
        log("Sterownik przeglądarki uruchomiony poprawnie.")
        
        schedule_data = [] 
        try:
            update_progress(10, "Pobieranie strony...")
            log(f"Strona: {TARGET_URL}")
            driver.get(TARGET_URL)
            wait = WebDriverWait(driver, 20)
            log("Strona załadowana.")

            update_progress(15, "Akceptacja cookies...")
            try:
                consent = WebDriverWait(driver, 3).until(EC.element_to_be_clickable((By.XPATH, "//button[contains(.,'Zgoda na wszystkie')]")))
                consent.click()
                log("Zaakceptowano pliki cookie.")
            except: 
                log("Brak banera cookies.")

            update_progress(20, "Szukanie adresu...")
            input_el = wait.until(EC.element_to_be_clickable((By.ID, "addressAutoComplete")))
            driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", input_el)
            input_el.clear()
            input_el.send_keys(address)
            time.sleep(1.5)

            try:
                suggestion = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, "li.yui3-aclist-item")))
                txt_sug = suggestion.text
                suggestion.click()
                log(f"Wybrano: {txt_sug}")
            except: raise Exception("Brak podpowiedzi adresu!")

            update_progress(30, "Pobieranie harmonogramu...")
            wait.until(EC.element_to_be_clickable((By.ID, "buttonNext"))).click()
            time.sleep(3)

            # PDF
            update_progress(40, "Pobieranie PDF...")
            try:
                for f in glob.glob(os.path.join(STATIC_DIR, "*.pdf")): os.remove(f)
                wait.until(EC.element_to_be_clickable((By.ID, "downloadPdfLink"))).click()
                timeout = 15
                downloaded_file = None
                while timeout > 0:
                    files = glob.glob(os.path.join(STATIC_DIR, "*.pdf"))
                    if files:
                        downloaded_file = files[0]
                        if not downloaded_file.endswith(".crdownload"): break
                    time.sleep(1)
                    timeout -= 1
                if downloaded_file:
                    original_pdf = os.path.join(STATIC_DIR, "harmonogram.pdf")
                    labeled_pdf = os.path.join(STATIC_DIR, "harmonogram_opisany.pdf")
                    if os.path.exists(original_pdf) and original_pdf != downloaded_file: os.remove(original_pdf)
                    os.rename(downloaded_file, original_pdf)
                    results["pdf_available"] = True
                    log("Pobrano PDF.")
                    update_progress(50, "Generowanie opisów PDF...")
                    if process_pdf_labels(original_pdf, labeled_pdf):
                        results["pdf_labeled_available"] = True
                        log("PDF opisany pomyślnie.")
            except Exception as e: log(f"Błąd PDF: {e}")

            # HTML
            update_progress(60, "Analiza danych...")
            element_ids = {"paper-date": "Papier", "mixed-date": "Zmieszane", "metals-date": "Metale i tworzywa sztuczne", "glass-date": "Szkło", "bio-date": "Bio", "green-date": "Zielone"}
            for html_id, waste_name in element_ids.items():
                try:
                    el = driver.find_element(By.ID, html_id)
                    txt = el.text.strip()
                    if txt:
                        schedule_data.append((txt, waste_name))
                        results["schedule"].append({"dateText": txt, "wasteType": waste_name})
                        log(f" -> Znaleziono: {waste_name} ({txt})")
                except: pass
        finally:
            driver.quit()

        if not schedule_data: raise Exception("Brak dat na stronie")

        # Calendar
        if send_to_calendar:
            count = send_schedule_to_calendar(service_google, schedule_data, allowed_types, log)
            results["added_events"] = count
            log(f"--- SUKCES: Dodano {count} wydarzeń ---")
        else:
            update_progress(90, "Zapisywanie danych...")
            log(f"--- SUKCES: Pobrano {len(schedule_data)} terminów (bez wysyłki do kalendarza) ---")

        results['timestamp'] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        save_state(results)
        
        with progress_lock:
            progress_state["percent"] = 100
            progress_state["message"] = "Zakończono pomyślnie!"
            progress_state["status"] = "finished"
            progress_state["result"] = results

    except Exception as e:
        log(f"BŁĄD: {str(e)}")
        with progress_lock:
            progress_state["status"] = "error"
            progress_state["message"] = str(e)
            results["status"] = "error"
            results["message"] = str(e)
            progress_state["result"] = results
        save_state(results)

def auto_scheduler():
    while True:
        try:
            state = load_state()
            if state.get("auto_mode", False) and progress_state["status"] != "running":
                today = datetime.date.today().isoformat()
                if state.get("last_auto_run") != today:
                    addr = state.get("saved_address")
                    if addr and get_google_creds():
                        yesterday = datetime.date.today() - datetime.timedelta(days=1)
                        should_run = False
                        for item in state.get("schedule", []):
                            ed = parse_polish_date(item['dateText'])
                            if ed and ed == yesterday: should_run = True; break
                        
                        if should_run:
                            reset_progress()
                            state['last_auto_run'] = today
                            save_state(state)
                            allowed_types = state.get("allowed_types", list(WASTE_COLORS.keys()))
                            run_full_process(addr, allowed_types)
            time.sleep(3600)
        except: time.sleep(60)

threading.Thread(target=auto_scheduler, daemon=True).start()

# --- ROUTES ---

@app.after_request
def bez_cache_html(odpowiedz):
    """Strona nie ma byc cache'owana - inaczej po zmianach w interfejsie
    przegladarka pokazuje stara wersje i wyglada to jakby nic sie nie stalo."""
    if odpowiedz.mimetype == 'text/html':
        odpowiedz.headers['Cache-Control'] = 'no-store, must-revalidate'
    return odpowiedz

@app.route('/')
def home():
    return render_template('index.html')

@app.route('/login')
def login():
    flow = Flow.from_client_secrets_file(
        CREDENTIALS_FILE,
        scopes=SCOPES,
        redirect_uri=url_for('oauth2callback', _external=True)
    )
    # Dodano prompt='consent', aby wymusić zwrócenie refresh_token
    auth_url, state = flow.authorization_url(access_type='offline', include_granted_scopes='true', prompt='consent')
    session['state'] = state
    # PKCE: authorization_url() losuje code_verifier i wysyła do Google jego skrót.
    # Callback tworzy nowy obiekt Flow, który tego losowania nie zna, więc verifier
    # musi przejechać przez sesję - inaczej wymiana kodu konczy sie bledem
    # "(invalid_grant) Missing code verifier".
    session['code_verifier'] = flow.code_verifier
    return redirect(auth_url)

@app.route('/oauth2callback')
def oauth2callback():
    state = session.get('state')
    if not state:
        return redirect(url_for('login'))   # sesja wygasła - zaczynamy od nowa
    flow = Flow.from_client_secrets_file(
        CREDENTIALS_FILE,
        scopes=SCOPES,
        state=state,
        redirect_uri=url_for('oauth2callback', _external=True)
    )
    flow.code_verifier = session.get('code_verifier')
    flow.fetch_token(authorization_response=request.url)
    session.pop('code_verifier', None)
    with open(TOKEN_FILE, 'wb') as token: pickle.dump(flow.credentials, token)
    return redirect(url_for('home'))

@app.route('/api/auth-status')
def auth_status():
    return jsonify({"authenticated": get_google_creds() is not None})

@app.route('/api/sync', methods=['POST'])
def api_sync():
    # sendToCalendar=False -> samo pobranie harmonogramu ze strony, bez Google
    send_to_calendar = (request.json or {}).get('sendToCalendar', True)
    if send_to_calendar and not get_google_creds():
        return jsonify({"status": "error", "message": "Brak logowania"})
    if progress_state["status"] == "running": return jsonify({"status": "error", "message": "Proces trwa"})
    reset_progress()
    threading.Thread(target=run_full_process, args=(request.json.get('address'), request.json.get('allowedTypes'), send_to_calendar)).start()
    return jsonify({"status": "started"})

@app.route('/api/calendar-sync', methods=['POST'])
def api_calendar_sync():
    """Wysyla do kalendarza harmonogram juz pobrany do aplikacji."""
    if not get_google_creds(): return jsonify({"status": "error", "message": "Brak logowania"})
    if progress_state["status"] == "running": return jsonify({"status": "error", "message": "Proces trwa"})
    state = load_state()
    if not state.get("schedule"):
        return jsonify({"status": "error", "message": "Brak pobranych danych. Najpierw kliknij 'Synchronizuj'."})
    reset_progress()
    allowed = (request.json or {}).get('allowedTypes') or list(WASTE_COLORS.keys())
    threading.Thread(target=push_state_to_calendar, args=(allowed,)).start()
    return jsonify({"status": "started"})

@app.route('/api/progress', methods=['GET'])
def api_progress():
    with progress_lock:
        return jsonify(progress_state)

@app.route('/api/toggle-auto', methods=['POST'])
def toggle_auto():
    en = request.json.get('enable', False)
    st = load_state()
    st['auto_mode'] = en
    save_state(st)
    return jsonify({"status": "success", "auto_mode": en})

@app.route('/api/save-address', methods=['POST'])
def save_address():
    """Zapisuje adres po stronie serwera, zeby byl ten sam na kazdym urzadzeniu."""
    adres = ((request.json or {}).get('address') or '').strip()
    st = load_state()
    st['saved_address'] = adres
    save_state(st)
    return jsonify({"status": "success", "saved_address": adres})

@app.route('/api/last-state', methods=['GET'])
def last_state(): return jsonify(load_state())

# --- JEZIOROWSKIE (sektor II gminy Stare Juchy) ---
# Osobne trasy i osobny kalendarz - nie dotykaja logiki warszawskiej.

@app.route('/api/jeziorowskie/config', methods=['GET'])
def jez_config():
    return jsonify(jeziorowskie.konfiguracja())

@app.route('/api/jeziorowskie/schedule', methods=['GET'])
def jez_schedule():
    dane = jeziorowskie.harmonogram()
    if not dane:
        return jsonify({"status": "error",
                        "message": "Brak danych - kliknij Synchronizuj"}), 404
    return jsonify(dane)

@app.route('/api/jeziorowskie/fetch', methods=['POST'])
def jez_fetch():
    """Pobiera harmonogram ze strony gminy i od razu go odczytuje."""
    if jeziorowskie.czy_trwa():
        return jsonify({"status": "error", "message": "Proces trwa"})
    jeziorowskie.uruchom_pobieranie()
    return jsonify({"status": "started"})

@app.route('/api/jeziorowskie/sync', methods=['POST'])
def jez_sync():
    """Wysyla terminy do osobnego kalendarza Google."""
    service_google = get_google_service()
    if not service_google:
        return jsonify({"status": "error", "message": "Brak logowania"})
    if jeziorowskie.czy_trwa():
        return jsonify({"status": "error", "message": "Proces trwa"})

    body = request.json or {}
    dozwolone = body.get('allowedTypes') or [f["id"] for f in jeziorowskie.FRAKCJE]
    jeziorowskie.uruchom_synchronizacje(service_google, dozwolone)
    return jsonify({"status": "started"})

@app.route('/api/jeziorowskie/progress', methods=['GET'])
def jez_progress():
    return jsonify(jeziorowskie.stan_postepu())

@app.route('/api/jeziorowskie/pdf', methods=['GET'])
def jez_pdf():
    sciezka = jeziorowskie.sciezka_pdf()
    if not sciezka:
        return jsonify({"status": "error", "message": "Brak pliku PDF"}), 404
    return send_from_directory(os.path.dirname(sciezka), os.path.basename(sciezka))

@app.route('/api/jeziorowskie/export.ics', methods=['GET'])
def jez_export_ics():
    """Eksportuje harmonogram Jeziorowskie do pliku .ics (iCalendar)."""
    frakcje_param = request.args.get('types')
    dozwolone = [f.strip() for f in frakcje_param.split(',') if f.strip()] if frakcje_param else None

    ics_text = jeziorowskie.generuj_ics(dozwolone)
    if not ics_text:
        return jsonify({"status": "error", "message": "Brak danych harmonogramu do wyeksportowania"}), 404

    dane = jeziorowskie.harmonogram()
    rok = dane.get("rok", "") if dane else ""
    filename = f"harmonogram-jeziorowskie-{rok}.ics" if rok else "harmonogram-jeziorowskie.ics"

    response = Response(ics_text, mimetype="text/calendar; charset=utf-8")
    response.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response

@app.route('/api/jeziorowskie/logs', methods=['GET'])
def jez_logs():
    """Zwraca listę logów dla regionu Jeziorowskie."""
    return jsonify({"logs": jeziorowskie.wczytaj_logi()})

@app.route('/api/clear-logs', methods=['POST'])
def clear_logs():
    """Czyści logi dla regionu Warszawa."""
    st = load_state()
    st['logs'] = []
    save_state(st)
    return jsonify({"status": "success"})

@app.route('/api/jeziorowskie/clear-logs', methods=['POST'])
def jez_clear_logs():
    """Czyści logi dla regionu Jeziorowskie."""
    jeziorowskie.wyczysc_logi()
    return jsonify({"status": "success"})

if __name__ == '__main__':
    # ssl_context='adhoc' generuje szybki certyfikat w locie
    app.run(host='0.0.0.0', port=5000, debug=True, use_reloader=False, ssl_context='adhoc')