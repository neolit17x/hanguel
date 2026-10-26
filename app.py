
from flask import Flask, request, redirect, url_for, session, render_template_string, flash, abort, Response, send_from_directory
import sqlite3, os, sys, json, io, urllib.request, urllib.error, csv, time, threading
from functools import wraps
from datetime import datetime, timezone

# Google Drive sync uses the Desktop OAuth client downloaded from Google Cloud.
DRIVE_BACKUP_NAME = 'hangeul-data-backup.json'
CREDENTIALS_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'credentials.json')
TOKEN_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'token.json')
DRIVE_SCOPES = ['https://www.googleapis.com/auth/drive.file']

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(APP_DIR, 'vocabulary.db')
app = Flask(__name__, static_folder=os.path.join(APP_DIR, 'static'))
app.secret_key = os.environ.get('SECRET_KEY', 'change-this-secret-key-before-deploying')
ADMIN_USERNAME = os.environ.get('ADMIN_USERNAME', 'admin')
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', 'change-me-now')

def auto_pronunciation(text):
    """Basic Korean Hangul-to-Latin romanization (Revised Romanization style).
    This is an estimate; Korean sound changes can make the spoken form differ.
    """
    initials = ['g','kk','n','d','tt','r','m','b','pp','s','ss','','j','jj','ch','k','t','p','h']
    vowels = ['a','ae','ya','yae','eo','e','yeo','ye','o','wa','wae','oe','yo','u','wo','we','wi','yu','eu','ui','i']
    finals = ['', 'k','k','ks','n','nj','nh','t','l','lk','lm','lb','ls','lt','lp','lh','m','p','ps','t','t','ng','t','t','k','t','p','t']
    out=[]
    for ch in text:
        code=ord(ch)
        if 0xAC00 <= code <= 0xD7A3:
            idx=code-0xAC00
            cho=idx//588; jung=(idx%588)//28; jong=idx%28
            out.append(initials[cho] + vowels[jung] + finals[jong])
        else:
            out.append(ch)
    return ''.join(out).strip()

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with db() as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS words (
            id INTEGER PRIMARY KEY AUTOINCREMENT, korean TEXT NOT NULL, uzbek TEXT NOT NULL,
            pronunciation TEXT DEFAULT '', category TEXT DEFAULT 'Umumiy', example TEXT DEFAULT '', image_url TEXT DEFAULT '',
            part_of_speech TEXT DEFAULT 'Ko‘rsatilmagan', created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
        conn.execute('''CREATE TABLE IF NOT EXISTS sections (
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE, description TEXT DEFAULT '',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
        conn.execute('''CREATE TABLE IF NOT EXISTS topic_folders (
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, description TEXT DEFAULT '',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
        conn.execute('''CREATE TABLE IF NOT EXISTS topic_days (
            id INTEGER PRIMARY KEY AUTOINCREMENT, folder_id INTEGER NOT NULL,
            day_number INTEGER NOT NULL, name TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(folder_id) REFERENCES topic_folders(id) ON DELETE CASCADE,
            UNIQUE(folder_id, day_number))''')
        conn.execute('''CREATE TABLE IF NOT EXISTS topic_words (
            id INTEGER PRIMARY KEY AUTOINCREMENT, day_id INTEGER NOT NULL,
            round_number INTEGER NOT NULL CHECK(round_number IN (1,2)),
            position INTEGER NOT NULL, korean TEXT NOT NULL, uzbek TEXT NOT NULL,
            pronunciation TEXT DEFAULT '', example TEXT DEFAULT '',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(day_id) REFERENCES topic_days(id) ON DELETE CASCADE,
            UNIQUE(day_id, round_number, position))''')
        conn.execute('''CREATE TABLE IF NOT EXISTS day_completions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            day_id INTEGER NOT NULL UNIQUE,
            xp_awarded INTEGER NOT NULL DEFAULT 10,
            completed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(day_id) REFERENCES topic_days(id) ON DELETE CASCADE)''')
        conn.execute('''CREATE TABLE IF NOT EXISTS round_activity_completions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            day_id INTEGER NOT NULL,
            round_number INTEGER NOT NULL CHECK(round_number IN (1,2)),
            activity TEXT NOT NULL CHECK(activity IN ('flashcard','matching','viktorina','writing')),
            completed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(day_id, round_number, activity),
            FOREIGN KEY(day_id) REFERENCES topic_days(id) ON DELETE CASCADE)''')
        conn.execute('''CREATE TABLE IF NOT EXISTS round_completions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            day_id INTEGER NOT NULL,
            round_number INTEGER NOT NULL CHECK(round_number IN (1,2)),
            xp_awarded INTEGER NOT NULL DEFAULT 10,
            completed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(day_id, round_number),
            FOREIGN KEY(day_id) REFERENCES topic_days(id) ON DELETE CASCADE)''')
        conn.execute('''CREATE TABLE IF NOT EXISTS grammar_lessons (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            pattern TEXT DEFAULT '',
            explanation TEXT NOT NULL,
            examples TEXT DEFAULT '',
            notes TEXT DEFAULT '',
            position INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
        conn.execute('''CREATE TABLE IF NOT EXISTS books (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            author TEXT DEFAULT '',
            level TEXT DEFAULT 'Beginner',
            description TEXT DEFAULT '',
            drive_file_id TEXT NOT NULL,
            cover_url TEXT DEFAULT '',
            position INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
        conn.execute('''CREATE TABLE IF NOT EXISTS dramas (
            id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, drama_url TEXT DEFAULT '',
            cover_url TEXT DEFAULT '', subtitle_filename TEXT DEFAULT '', folder_id INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
        conn.execute('''CREATE TABLE IF NOT EXISTS drama_words (
            id INTEGER PRIMARY KEY AUTOINCREMENT, drama_id INTEGER NOT NULL, korean TEXT NOT NULL,
            uzbek TEXT DEFAULT '', pronunciation TEXT DEFAULT '', example TEXT DEFAULT '',
            day_id INTEGER, round_number INTEGER, position INTEGER DEFAULT 0,
            translation_status TEXT DEFAULT 'needs_review', created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(drama_id, korean), FOREIGN KEY(drama_id) REFERENCES dramas(id) ON DELETE CASCADE)''')
        books_cols = {row[1] for row in conn.execute('PRAGMA table_info(books)').fetchall()}
        if 'telegram_url' not in books_cols:
            conn.execute("ALTER TABLE books ADD COLUMN telegram_url TEXT DEFAULT ''")
        topic_cols = {row[1] for row in conn.execute('PRAGMA table_info(topic_words)').fetchall()}
        if 'part_of_speech' not in topic_cols:
            conn.execute("ALTER TABLE topic_words ADD COLUMN part_of_speech TEXT DEFAULT 'Ko‘rsatilmagan'")
        if 'image_url' not in topic_cols:
            conn.execute("ALTER TABLE topic_words ADD COLUMN image_url TEXT DEFAULT ''")
        cols = {row[1] for row in conn.execute('PRAGMA table_info(words)').fetchall()}
        if 'image_url' not in cols:
            conn.execute("ALTER TABLE words ADD COLUMN image_url TEXT DEFAULT ''")
        if 'part_of_speech' not in cols:
            conn.execute("ALTER TABLE words ADD COLUMN part_of_speech TEXT DEFAULT 'Ko‘rsatilmagan'")
        # Bo‘sh bazaga avtomatik namunaviy so‘zlar qo‘shilmaydi.
        # Faqat admin kiritgan yoki Drive’dan tiklangan ma’lumotlar ishlatiladi.
        # Turn every existing category into its own section, preserving all words.
        cats = conn.execute("SELECT DISTINCT TRIM(category) FROM words WHERE TRIM(category) <> ''").fetchall()
        for row in cats:
            conn.execute('INSERT OR IGNORE INTO sections (name) VALUES (?)', (row[0],))

def _google_modules():
    """Import Google libraries only when Drive functionality is used."""
    try:
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from google.auth.transport.requests import Request
        from googleapiclient.discovery import build
        from googleapiclient.http import MediaIoBaseUpload, MediaIoBaseDownload
        return Credentials, InstalledAppFlow, Request, build, MediaIoBaseUpload, MediaIoBaseDownload
    except ImportError as exc:
        raise RuntimeError(
            'Google Drive kutubxonalari topilmadi. O‘rnatish: '
            'python3 -m pip install google-api-python-client google-auth-httplib2 google-auth-oauthlib'
        ) from exc


def drive_auth():
    """Authorize the Desktop OAuth client and save token.json locally."""
    Credentials, InstalledAppFlow, Request, build, MediaIoBaseUpload, MediaIoBaseDownload = _google_modules()
    if not os.path.exists(CREDENTIALS_PATH):
        raise RuntimeError(f'credentials.json topilmadi: {CREDENTIALS_PATH}')
    flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_PATH, DRIVE_SCOPES)
    creds = flow.run_local_server(host='localhost', port=0, access_type='offline', prompt='consent')
    with open(TOKEN_PATH, 'w', encoding='utf-8') as f:
        f.write(creds.to_json())
    try:
        os.chmod(TOKEN_PATH, 0o600)
    except OSError:
        pass
    print('Google Drive ruxsati saqlandi: token.json')


def _drive_service():
    Credentials, InstalledAppFlow, Request, build, MediaIoBaseUpload, MediaIoBaseDownload = _google_modules()
    if not os.path.exists(TOKEN_PATH):
        return None
    creds = Credentials.from_authorized_user_file(TOKEN_PATH, DRIVE_SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        with open(TOKEN_PATH, 'w', encoding='utf-8') as f:
            f.write(creds.to_json())
        try:
            os.chmod(TOKEN_PATH, 0o600)
        except OSError:
            pass
    if not creds.valid:
        return None
    return build('drive', 'v3', credentials=creds, cache_discovery=False)


def _database_payload():
    with db() as conn:
        words = [dict(row) for row in conn.execute('SELECT * FROM words ORDER BY id').fetchall()]
        sections = [dict(row) for row in conn.execute('SELECT * FROM sections ORDER BY id').fetchall()]
        topic_folders = [dict(row) for row in conn.execute('SELECT * FROM topic_folders ORDER BY id').fetchall()]
        topic_days = [dict(row) for row in conn.execute('SELECT * FROM topic_days ORDER BY id').fetchall()]
        topic_words = [dict(row) for row in conn.execute('SELECT * FROM topic_words ORDER BY id').fetchall()]
        day_completions = [dict(row) for row in conn.execute('SELECT * FROM day_completions ORDER BY id').fetchall()]
        round_activity_completions = [dict(row) for row in conn.execute('SELECT * FROM round_activity_completions ORDER BY id').fetchall()]
        round_completions = [dict(row) for row in conn.execute('SELECT * FROM round_completions ORDER BY id').fetchall()]
        grammar_lessons = [dict(row) for row in conn.execute('SELECT * FROM grammar_lessons ORDER BY position,id').fetchall()]
        books = [dict(row) for row in conn.execute('SELECT * FROM books ORDER BY position,id').fetchall()]
        dramas = [dict(row) for row in conn.execute('SELECT * FROM dramas ORDER BY id').fetchall()]
        drama_words = [dict(row) for row in conn.execute('SELECT * FROM drama_words ORDER BY id').fetchall()]
    return {
        'format_version': 1,
        'app': 'HANGEUL',
        'updated_at': datetime.now(timezone.utc).isoformat(),
        'words': words,
        'sections': sections,
        'topic_folders': topic_folders,
        'topic_days': topic_days,
        'topic_words': topic_words,
        'day_completions': day_completions,
        'round_activity_completions': round_activity_completions,
        'round_completions': round_completions,
        'grammar_lessons': grammar_lessons,
        'books': books,
        'dramas': dramas,
        'drama_words': drama_words,
    }


def _find_drive_backup(service):
    result = service.files().list(
        q="name = 'hangeul-data-backup.json' and trashed = false",
        spaces='drive', fields='files(id,name,modifiedTime)', orderBy='modifiedTime desc', pageSize=10
    ).execute()
    files = result.get('files', [])
    return files[0] if files else None


def drive_upload_backup():
    """Upload current SQLite content as one JSON file, creating/updating it in Drive."""
    service = _drive_service()
    if service is None:
        return False, 'Google Drive ulanmagan. Avval: python3 app.py --drive-auth'
    payload = json.dumps(_database_payload(), ensure_ascii=False, indent=2).encode('utf-8')
    from googleapiclient.http import MediaIoBaseUpload
    media = MediaIoBaseUpload(io.BytesIO(payload), mimetype='application/json', resumable=False)
    existing = _find_drive_backup(service)
    if existing:
        service.files().update(fileId=existing['id'], media_body=media, fields='id,name,modifiedTime').execute()
        return True, 'Ma’lumotlar Google Drive’ga yangilandi.'
    metadata = {'name': DRIVE_BACKUP_NAME, 'mimeType': 'application/json'}
    service.files().create(body=metadata, media_body=media, fields='id,name,modifiedTime').execute()
    return True, 'Google Drive’da birinchi backup yaratildi.'


def drive_restore_backup():
    """Restore the Drive JSON backup into SQLite; return False if no backup exists."""
    service = _drive_service()
    if service is None:
        return False, 'Google Drive token topilmadi yoki yaroqsiz.'
    backup = _find_drive_backup(service)
    if not backup:
        return False, 'Drive backup hali mavjud emas.'
    from googleapiclient.http import MediaIoBaseDownload
    request_download = service.files().get_media(fileId=backup['id'])
    buffer = io.BytesIO()
    downloader = MediaIoBaseDownload(buffer, request_download)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    try:
        payload = json.loads(buffer.getvalue().decode('utf-8'))
        if payload.get('format_version') != 1 or not isinstance(payload.get('words'), list) or not isinstance(payload.get('sections'), list):
            raise ValueError('Backup formatini tanib bo‘lmadi.')
        with db() as conn:
            conn.execute('DELETE FROM words')
            conn.execute('DELETE FROM sections')
            for section in payload['sections']:
                conn.execute(
                    'INSERT INTO sections (id,name,description,created_at) VALUES (?,?,?,?)',
                    (section.get('id'), section['name'], section.get('description', ''), section.get('created_at'))
                )
            for word in payload['words']:
                conn.execute(
                    'INSERT INTO words (id,korean,uzbek,pronunciation,category,example,image_url,part_of_speech,created_at) VALUES (?,?,?,?,?,?,?,?,?)',
                    (word.get('id'), word['korean'], word['uzbek'], word.get('pronunciation', ''),
                     word.get('category', 'Umumiy'), word.get('example', ''), word.get('image_url', ''),
                     word.get('part_of_speech', 'Ko‘rsatilmagan'), word.get('created_at'))
                )
            # Restore drama data only when the backup contains it; older backups remain compatible.
            if 'dramas' in payload:
                conn.execute('DELETE FROM drama_words')
                conn.execute('DELETE FROM dramas')
                for d in payload.get('dramas', []):
                    conn.execute('INSERT INTO dramas(id,title,drama_url,cover_url,subtitle_filename,folder_id,created_at) VALUES(?,?,?,?,?,?,?)',
                        (d.get('id'), d['title'], d.get('drama_url',''), d.get('cover_url',''), d.get('subtitle_filename',''), d.get('folder_id'), d.get('created_at')))
                for w in payload.get('drama_words', []):
                    conn.execute('INSERT INTO drama_words(id,drama_id,korean,uzbek,pronunciation,example,day_id,round_number,position,translation_status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                        (w.get('id'), w['drama_id'], w['korean'], w.get('uzbek',''), w.get('pronunciation',''), w.get('example',''), w.get('day_id'), w.get('round_number'), w.get('position',0), w.get('translation_status','needs_review'), w.get('created_at')))
            # Older backups omit the custom topic tree; preserve local folders in that case.
            if 'topic_folders' in payload:
                for table in ('topic_words', 'topic_days', 'topic_folders'):
                    conn.execute(f'DELETE FROM {table}')
                for folder in payload.get('topic_folders', []):
                    conn.execute('INSERT INTO topic_folders (id,name,description,created_at) VALUES (?,?,?,?)',
                        (folder.get('id'), folder['name'], folder.get('description',''), folder.get('created_at')))
                for day in payload.get('topic_days', []):
                    conn.execute('INSERT INTO topic_days (id,folder_id,day_number,name,created_at) VALUES (?,?,?,?,?)',
                        (day.get('id'), day['folder_id'], day['day_number'], day.get('name', f"DAY {day['day_number']}"), day.get('created_at')))
                for item in payload.get('topic_words', []):
                    conn.execute('INSERT INTO topic_words (id,day_id,round_number,position,korean,uzbek,pronunciation,example,created_at) VALUES (?,?,?,?,?,?,?,?,?)',
                        (item.get('id'), item['day_id'], item['round_number'], item['position'], item['korean'], item['uzbek'], item.get('pronunciation',''), item.get('example',''), item.get('created_at')))
                conn.execute('DELETE FROM day_completions')
                conn.execute('DELETE FROM round_activity_completions')
                conn.execute('DELETE FROM round_completions')
                for item in payload.get('day_completions', []):
                    conn.execute('INSERT OR IGNORE INTO day_completions (id,day_id,xp_awarded,completed_at) VALUES (?,?,?,?)',
                        (item.get('id'), item['day_id'], item.get('xp_awarded', 10), item.get('completed_at')))
                for item in payload.get('round_activity_completions', []):
                    conn.execute('INSERT OR IGNORE INTO round_activity_completions (id,day_id,round_number,activity,completed_at) VALUES (?,?,?,?,?)',
                        (item.get('id'), item['day_id'], item['round_number'], item['activity'], item.get('completed_at')))
                for item in payload.get('round_completions', []):
                    conn.execute('INSERT OR IGNORE INTO round_completions (id,day_id,round_number,xp_awarded,completed_at) VALUES (?,?,?,?,?)',
                        (item.get('id'), item['day_id'], item['round_number'], item.get('xp_awarded', 10), item.get('completed_at')))
        return True, f"Drive backup tiklandi ({len(payload['words'])} ta so‘z, {len(payload['sections'])} ta bo‘lim, {len(payload.get('topic_folders', []))} ta papka)."
    except (ValueError, KeyError, TypeError, json.JSONDecodeError, sqlite3.Error) as exc:
        return False, f'Drive backupni tiklab bo‘lmadi: {exc}'


def initialize_drive_data():
    """Use Drive as source of truth if a backup exists; otherwise upload local data."""
    if not os.path.exists(TOKEN_PATH):
        print('Google Drive ulanmagan. Mahalliy SQLite ishlatiladi; ulash uchun: python3 app.py --drive-auth')
        return
    try:
        restored, message = drive_restore_backup()
        if restored:
            print(message)
            return
        if message == 'Drive backup hali mavjud emas.':
            uploaded, upload_message = drive_upload_backup()
            print(upload_message if uploaded else f'Drive sync: {upload_message}')
        else:
            print(f'Drive sync: {message}')
    except Exception as exc:
        print(f'Drive sync xatosi (sayt mahalliy rejimda davom etadi): {exc}')


def sync_drive_after_change():
    """Push database changes to Drive without making the website unavailable on errors."""
    if not os.path.exists(TOKEN_PATH):
        return None
    try:
        ok, message = drive_upload_backup()
        return message if ok else f'Google Drive saqlash amalga oshmadi: {message}'
    except Exception as exc:
        return f'Google Drive saqlash xatosi: {exc}'


def admin_required(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        if not session.get('admin_logged_in'):
            return redirect(url_for('login', next=request.path))
        return fn(*args, **kwargs)
    return wrapped

STYLE = ":root{color-scheme:light;--bg:#f4f8ff;--panel:rgba(255,255,255,.92);--panel2:#f5f8ff;--surface:#fff;--input:#fff;--text:#0b2774;--muted:#3364cd;--purple:#7040f0;--accent:#2874f0;--border:#dbe7fa;--danger:#e34f6b;--shadow:rgba(55,107,179,.12)}\n:root[data-theme='dark']{color-scheme:dark;--bg:#0c1530;--panel:rgba(15,27,63,.94);--panel2:#172a58;--surface:#101a36;--input:#0b1430;--text:#edf4ff;--muted:#94b9ff;--purple:#8b7bff;--accent:#3e8bff;--border:#283b70;--danger:#ff718e;--shadow:rgba(0,0,0,.22)}\n*{box-sizing:border-box}html{min-width:320px}body{margin:0;min-width:320px;min-height:100vh;background:radial-gradient(circle at 77% 9%,rgba(119,220,255,.18),transparent 23%),linear-gradient(145deg,#f8fbff,#edf5ff);color:var(--text);font:15px/1.5 Inter,'Noto Sans KR',system-ui,-apple-system,sans-serif;transition:background .25s,color .25s}:root[data-theme='dark'] body{background:radial-gradient(ellipse at top,#202d62 0,#080d20 55%)}\na{color:inherit;text-decoration:none}button,input,textarea,select{font:inherit}button{cursor:pointer}.app-shell{min-height:100vh}.topbar{height:120px;position:sticky;top:0;z-index:20;display:flex;align-items:center;gap:28px;padding:0 28px;border-bottom:1px solid rgba(61,112,200,.1);background:rgba(255,255,255,.84);backdrop-filter:blur(18px)}:root[data-theme='dark'] .topbar{background:rgba(13,23,54,.9)}\n.brand{width:182px;display:flex;align-items:center;gap:11px;flex-shrink:0;color:var(--text)}.brand-mark{width:40px;height:40px;position:relative;display:flex;flex:0 0 40px;filter:drop-shadow(0 3px 4px rgba(29,74,230,.18))}.brand-mark img{width:100%;height:100%;display:block;object-fit:contain}.brand-mark:after{display:none}.brand-mark:after{content:'';position:absolute;width:4px;height:4px;border-radius:50%;background:#ff5cc7;right:-2px;top:0}.brand-mark i{position:absolute;top:2px;bottom:2px;width:21px;background:#fff;border:3px solid #1759ed}.brand-mark i:first-child{left:0;border-radius:3px 1px 5px 3px;transform:skewY(7deg);border-right-color:#ee4dc6}.brand-mark i:last-child{right:0;border-radius:1px 3px 3px 5px;transform:skewY(-7deg);border-left-color:#ee4dc6}.brand strong{display:block;font-size:15px;letter-spacing:.5px}.brand small{display:block;font-size:9px;line-height:1.3;color:#3364cd;margin-top:2px;white-space:nowrap}.top-search{height:42px;max-width:550px;flex:1;display:flex;align-items:center;gap:10px;padding:0 12px 0 16px;background:var(--panel);border:1px solid var(--border);border-radius:24px;box-shadow:0 4px 15px rgba(73,113,181,.08);color:#2361e8}.top-search input{min-width:80px;flex:1;border:0;outline:0;color:var(--text);background:transparent;font-size:12px}.top-search input::placeholder{color:#3372df}.top-search kbd{padding:3px 7px;border-radius:8px;color:#4c78d4;background:#f5f8fd;font-size:10px;font-family:inherit}.top-actions{display:flex;align-items:center;gap:10px;margin-left:auto}.top-actions .top-control{height:40px;display:inline-flex;align-items:center;justify-content:center;gap:7px;padding:0 12px;border:1px solid var(--border);border-radius:22px;background:var(--panel);color:#1147bc;box-shadow:0 3px 13px rgba(52,99,174,.08);font-size:12px}.theme-button{min-width:42px;font-size:18px!important}.achievements-top-link{white-space:nowrap;color:#7040f0!important}.achievements-top-link span{display:inline}.settings-top-link{width:40px;height:40px;min-width:40px;max-width:40px;flex:0 0 40px;box-sizing:border-box;display:inline-flex;align-items:center;justify-content:center;padding:0!important;border-radius:50%!important;font-size:18px!important}.profile-avatar{width:31px;height:31px;display:grid;place-items:center;border-radius:50%;color:#fff;background:linear-gradient(150deg,#5dcfff,#8971ed 55%,#f56bbc);font-weight:800}.layout{min-height:calc(100vh - 120px);display:grid;grid-template-columns:220px minmax(0,1fr)}.sidebar{display:flex;flex-direction:column;padding:16px 13px 0;border-right:1px solid rgba(60,105,190,.08);background:rgba(249,252,255,.72);overflow:hidden}:root[data-theme='dark'] .sidebar{background:rgba(13,23,54,.65)}.side-nav{display:grid;gap:4px}.side-nav a{height:48px;display:flex;align-items:center;gap:13px;padding:0 14px;border-radius:10px;color:#184ab7;font-size:16px;font-weight:650;transition:background .15s,transform .15s}.side-nav a:hover{background:rgba(45,124,255,.08);transform:translateX(2px)}.side-nav a.active{color:#fff;background:linear-gradient(105deg,#6239f3,#1abdf4);box-shadow:0 7px 18px rgba(54,110,235,.28)}.side-nav .nav-icon{width:22px;text-align:center;font-size:18px}.side-nav .nav-arrow{margin-left:auto;font-size:20px;font-weight:400}.encouragement{margin:22px 0 15px;min-height:70px;display:flex;align-items:center;padding:9px;border-radius:12px;background:linear-gradient(110deg,#eef6ff,#eaf5ff);box-shadow:0 8px 22px rgba(99,161,225,.1)}:root[data-theme='dark'] .encouragement{background:#172a58}.encouragement-flag{width:40px;height:40px;flex:0 0 40px;object-fit:contain;display:block;border-radius:50%}.encouragement p{font-size:9px;line-height:1.6;color:#1d55bd;margin:0 0 0 8px}.encouragement .heart{color:#ff65c8;font-size:14px;align-self:flex-end}.cat{width:39px;height:36px;position:relative;display:grid;place-items:center;border:1.5px solid #8f76f5;border-radius:45% 45% 50% 50%;background:#fff;color:#9e52e8;flex-shrink:0}.cat:before,.cat:after{content:'';position:absolute;top:-7px;width:12px;height:14px;background:#fff;border:1.5px solid #8f76f5;transform:rotate(45deg);z-index:-1}.cat:before{left:2px}.cat:after{right:2px}.cat i:before,.cat i:after{content:'';position:absolute;top:12px;width:3px;height:3px;border-radius:50%;background:#334cbc}.cat i:before{left:10px}.cat i:after{right:10px}.cat b{font-size:15px;font-weight:400;margin-top:7px}.sidebar-art{position:relative;min-height:230px;flex:1;margin:0 -13px;overflow:hidden;background:linear-gradient(#f6fbff,#c7e7ff 46%,#abc9fa)}.sidebar-art:before{content:'';position:absolute;inset:0;background:radial-gradient(circle at 16% 12%,#ffbfdf 0 2px,transparent 3px),radial-gradient(circle at 28% 17%,#ff8ccd 0 3px,transparent 4px),radial-gradient(circle at 13% 25%,#f89acb 0 3px,transparent 4px);background-size:38px 42px;opacity:.72}.moon-disc{position:absolute;width:100px;height:100px;border-radius:50%;background:#f5fbff;top:40px;left:45px;opacity:.8}.mountain{position:absolute;width:170px;height:140px;transform:rotate(45deg);background:linear-gradient(135deg,#dcdefe,#769fea);bottom:25px}.mountain.one{left:-70px}.mountain.two{right:-105px;bottom:65px;background:linear-gradient(135deg,#dff6ff,#809ff0)}.pagoda{position:absolute;right:24px;bottom:24px;width:68px;height:112px;border-bottom:8px solid #3857a8}.pagoda i{position:relative;display:block;height:15px;margin-top:10px;border-bottom:7px solid #375db9}.pagoda i:after{content:'';position:absolute;left:-10px;right:-10px;bottom:-4px;height:5px;border-radius:50%;background:#283f92}.pagoda b{display:block;width:5px;height:30px;margin:auto;background:#334c9e}.sidebar-art strong{position:absolute;bottom:49px;left:22px;color:#0755e9;font-size:21px}.sidebar-art small{position:absolute;bottom:13px;left:23px;color:#2461c7;line-height:1.4;font-size:11px}.sidebar-art .blossom{position:absolute;color:#ff66c3}.b1{top:19px;left:24px}.b2{top:48px;left:80px}.b3{top:95px;left:16px}.content-area{min-width:0;padding:18px 24px 30px;background:url('/rasmlar/Pastel%20Seoul%20Springtime%20Panorama.png') center/cover fixed}:root[data-theme='dark'] .content-area{background:url('/rasmlar/Moonlit%20Seoul%20by%20the%20Han%20River.png') center/cover fixed}.flash-hero{height:110px;position:relative;overflow:hidden;margin:0 0 18px;border-radius:15px;background:linear-gradient(90deg,rgba(8,17,50,.62),rgba(8,17,50,.12)),url('/rasmlar/Hanok%20Terrace%20Overlooking%20Seoul%20at%20Sunset.png') center/cover;box-shadow:0 7px 20px rgba(74,133,210,.12)}:root[data-theme='dark'] .flash-hero{background:linear-gradient(90deg,rgba(8,17,50,.62),rgba(8,17,50,.12)),url('/rasmlar/Moonlit%20Hanok%20Terrace%20Overlooking%20Seoul.png') center/cover}.flash-hero:after{content:'';position:absolute;inset:0;background:radial-gradient(circle at 10% 25%,rgba(255,255,255,.9) 0 4px,transparent 5px),radial-gradient(circle at 87% 22%,#ff9cce 0 4px,transparent 5px);background-size:85px 65px;opacity:.65}.flash-hero-copy{position:relative;z-index:2;height:100%;display:flex;align-items:center;gap:17px;padding-left:36px}.flash-hero-icon{width:58px;height:58px;display:grid;place-items:center;border:1px solid rgba(255,255,255,.5);border-radius:50%;color:#6350ee;background:rgba(255,255,255,.24);backdrop-filter:blur(8px);-webkit-backdrop-filter:blur(8px);box-shadow:0 6px 18px rgba(32,52,115,.16);font-size:25px}.flash-hero-icon img{width:100%;height:100%;display:block;object-fit:contain;border-radius:0}.flash-hero h1{margin:0 0 4px;font-size:clamp(24px,3vw,30px);color:#fff}.flash-hero p{margin:0;font-size:12px;color:rgba(255,255,255,.9)}.flash-hero-art{display:none;position:absolute;inset:0;overflow:hidden}.flash-sunset{position:absolute;width:155px;height:155px;border-radius:50%;background:rgba(255,255,255,.44);left:45%;top:26px}.flash-peak{position:absolute;bottom:-80px;width:200px;height:150px;transform:rotate(45deg);background:linear-gradient(135deg,#e8f6ff,#a9b8f3)}.flash-peak.p1{left:43%}.flash-peak.p2{left:63%;bottom:-105px}.flash-temple{position:absolute;right:72px;bottom:4px;width:145px;height:70px;color:#23499e}.flash-temple i,.flash-temple b{position:absolute;left:16px;right:16px;height:11px;border-radius:50%;background:#3753a6;transform:skewX(-18deg)}.flash-temple i{top:6px}.flash-temple b{top:27px;left:3px;right:3px}.flash-temple em{position:absolute;left:33px;right:33px;top:35px;height:35px;border:7px double #4761b8;border-bottom:0}.flash-flower{position:absolute;color:#ff72c0}.flash-flower.f1{right:24px;top:17px}.flash-flower.f2{right:190px;top:12px}.flash-flower.f3{right:48px;bottom:12px}\n.wrap{max-width:1050px;margin:auto;padding:24px 18px 60px}.top{display:flex;align-items:center;justify-content:space-between;gap:14px;margin-bottom:28px}.btn{border:1px solid var(--border);border-radius:12px;padding:10px 14px;background:var(--panel2);color:var(--text);cursor:pointer;font:inherit;display:inline-flex;align-items:center;justify-content:center;gap:7px}.btn.primary{background:var(--purple);border-color:var(--purple);color:#fff;font-weight:750}.btn.danger{color:var(--danger)}.hero{margin:12px 0 22px}.eyebrow{color:var(--purple);font-size:12px;font-weight:800;text-transform:uppercase;letter-spacing:1.5px}.hero h1{font-size:clamp(28px,5vw,42px);line-height:1.1;margin:8px 0}.muted{color:var(--muted)}.panel{background:var(--panel);border:1px solid var(--border);border-radius:20px;padding:20px;box-shadow:0 18px 45px var(--shadow)}.panel h2{font-size:18px;margin:0 0 15px}.field{display:grid;gap:6px;margin:12px 0}.field label{font-size:13px;color:var(--muted)}input,textarea,select{width:100%;padding:11px 12px;border-radius:10px;border:1px solid var(--border);background:var(--input);color:var(--text);font:inherit}textarea{min-height:76px;resize:vertical}.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:12px}.word,.section-card{background:var(--panel);border:1px solid var(--border);border-radius:16px;padding:17px}.section-card{transition:border-color .15s,transform .15s}.section-card:hover{border-color:var(--purple);transform:translateY(-2px)}.word-image{width:100%;max-height:170px;object-fit:cover;border-radius:12px;margin:0 0 10px}.audio-btn{margin-top:8px}.section-audio{display:inline-flex;align-items:center;justify-content:center;width:44px;height:44px;flex:0 0 44px;border:1px solid var(--border);background:var(--panel2);color:#1769e8;border-radius:50%;padding:0;font-size:22px;cursor:pointer}.hangul{font-size:30px;font-weight:800}.pron{color:var(--purple);font-size:13px}.translation{font-size:17px;margin:5px 0 12px}.tag{display:inline-block;background:#e3efff;color:#1763d8;border-radius:999px;padding:3px 9px;font-size:12px}.example{border-top:1px solid var(--border);margin-top:12px;padding-top:10px;color:var(--muted);font-size:14px}.searchrow{display:flex;gap:10px;margin-bottom:15px}.searchrow input{flex:1}.notice{padding:11px 14px;border-radius:12px;background:#eaf2ff;border:1px solid var(--border);margin-bottom:15px;color:var(--text);opacity:1;transform:translateY(0);transition:opacity .25s ease,transform .25s ease,max-height .25s ease,margin .25s ease,padding .25s ease}.notice.notice-hidden{opacity:0;transform:translateY(-5px);max-height:0!important;margin-top:0!important;margin-bottom:0!important;padding-top:0!important;padding-bottom:0!important;border-width:0;overflow:hidden}.empty{padding:32px;text-align:center;color:var(--muted);border:1px dashed var(--border);border-radius:14px}.word-actions{display:flex;gap:8px;margin-top:12px;flex-wrap:wrap}.word-actions form{margin:0}.small{font-size:13px}.center{max-width:420px;margin:60px auto}.center .panel{padding:26px}.footer{margin-top:22px;color:var(--muted);font-size:12px;text-align:center}.section-icon{font-size:28px}.inline-form{display:inline}.section-head{display:flex;align-items:center;justify-content:space-between;gap:10px;flex-wrap:wrap}\n.marathon-page{max-width:940px;margin:0 auto;color:var(--text)}.marathon-shell{background:var(--panel);border:1px solid rgba(103,145,205,.16);border-radius:22px;padding:20px;box-shadow:0 8px 28px var(--shadow)}.marathon-head{display:none}.marathon-meta{padding:4px 4px 17px}.marathon-progress-row{display:flex;align-items:center;gap:14px}.marathon-progress{display:grid;grid-template-columns:repeat(25,minmax(0,1fr));gap:3px;flex:1}.marathon-segment{height:8px;border-radius:99px;background:#dfe5ef}.marathon-segment.active{background:#2874f0}.marathon-count{font-size:14px;white-space:nowrap;color:var(--muted);font-weight:700}.flashcard{background:var(--surface);border:1px solid var(--border);border-radius:20px;padding:clamp(20px,4vw,34px);box-shadow:0 12px 32px var(--shadow)}.word-badge{display:inline-flex;align-items:center;gap:6px;background:#e3efff;color:#1763d8;border-radius:999px;padding:6px 13px;font-weight:750;font-size:13px}.flash-word{text-align:center;padding:18px 0 20px}.flash-word .korean{font-family:'Noto Sans KR',sans-serif;font-size:clamp(42px,8vw,66px);font-weight:750;letter-spacing:1px;line-height:1.2;color:var(--text);overflow-wrap:anywhere}.flash-word .uzbek{font-size:clamp(22px,4vw,30px);color:var(--muted);margin-top:8px}.flash-example{background:var(--panel2);border-left:5px solid var(--accent);border-radius:4px 14px 14px 4px;padding:16px 18px;margin:4px 0 18px;color:var(--text);overflow-wrap:anywhere}.example-ko{font-size:clamp(17px,3vw,22px);font-weight:650}.example-uz{font-size:15px;color:var(--muted);margin-top:7px}.flash-actions{display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:10px}.flash-link{border:0;background:transparent;color:#1769e8;font:inherit;font-weight:750;cursor:pointer;padding:10px 12px}.flash-audio{display:inline-flex;align-items:center;justify-content:center;width:48px;height:48px;flex:0 0 48px;border:1px solid #d7e5fb;background:#f5f9ff;color:#1769e8;border-radius:50%;padding:0;font-size:22px;cursor:pointer}.flash-audio:hover{background:#e8f1ff}.marathon-footer{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-top:16px}.marathon-btn{border:1px solid #cbd5e5;border-radius:999px;background:var(--panel2);color:var(--text);font:inherit;font-weight:750;padding:13px 16px;cursor:pointer;text-align:center}.marathon-btn.next{background:linear-gradient(100deg,#701bf1,#2047f0 55%,#00b9ed);border-color:transparent;color:#fff}.marathon-btn:disabled{opacity:.45;cursor:not-allowed}.translation-form{display:none;margin-top:12px}.translation-form.open{display:block}.translation-form textarea{background:var(--input);color:var(--text);border-color:var(--border)}.translation-form button{border:0;border-radius:999px;padding:10px 16px;background:linear-gradient(100deg,#701bf1,#00b9ed);color:#fff;font-weight:700}\n@media(max-width:950px){.topbar{gap:14px;padding:0 16px}.brand{width:150px}.layout{grid-template-columns:190px minmax(0,1fr)}.content-area{padding:16px}.top-search kbd{display:none}.top-actions{gap:6px}.top-actions .top-control{padding:0 9px}}\n@media(max-width:680px){.topbar{height:auto;min-height:68px;flex-wrap:wrap;padding:10px 13px;gap:9px}.brand{width:auto;flex:1}.brand small{font-size:8px}.top-actions{margin-left:0}.top-actions .language-control,.top-actions .profile-label{display:none}.top-search{order:3;flex-basis:100%;max-width:none;height:38px}.layout{display:block;min-height:calc(100vh - 68px)}.sidebar{padding:8px 10px;border-right:0;border-bottom:1px solid var(--border);overflow-x:auto}.side-nav{display:flex;min-width:max-content;gap:5px}.side-nav a{height:40px;padding:0 11px;gap:7px}.side-nav .nav-icon{width:auto}.side-nav .nav-arrow,.encouragement,.sidebar-art{display:none}.content-area{padding:12px}.flash-hero{height:104px}.flash-hero-copy{padding-left:16px;gap:10px}.flash-hero-icon{width:45px;height:45px;font-size:20px}.flash-hero h1{font-size:22px}.flash-hero p{max-width:240px;font-size:10px}.flash-temple{right:5px;transform:scale(.75);transform-origin:bottom right}.flash-peak.p2{left:75%}.marathon-shell{padding:12px;border-radius:17px}.flashcard{border-radius:17px;padding:20px 16px}.marathon-footer{gap:9px}.marathon-btn{padding:12px 8px}.searchrow{flex-wrap:wrap}.searchrow input{flex-basis:100%}.searchrow .btn{flex:1}}\n\n\n.flash-hero-art{background-image:linear-gradient(90deg,rgba(249,229,255,.12),transparent 68%),url('/rasmlar/flash-card-banner.png');background-repeat:no-repeat;background-size:cover,auto 180px;background-position:center,right 45px center}.flash-hero-art .flash-sunset,.flash-hero-art .flash-peak,.flash-hero-art .flash-temple,.flash-hero-art .flash-flower{display:none}\n@media(max-width:680px){.flash-hero-art{background-size:cover,auto 148px;background-position:center,right -8px center}}\n\n\n.sidebar-art{background-image:linear-gradient(0deg,rgba(5,26,90,.32),transparent 42%),url('/rasmlar/Pastel%20Seoul%20Springtime%20Panorama.png');background-size:cover;background-position:center 54%}\n.sidebar-art:before,.sidebar-art .moon-disc,.sidebar-art .mountain,.sidebar-art .pagoda,.sidebar-art .blossom{display:none}\n.sidebar-art strong,.sidebar-art small{color:#fff;text-shadow:0 2px 8px rgba(5,25,80,.65)}\n\n.admin-word-list{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,205px),1fr));gap:10px;margin-top:12px;align-items:stretch}.admin-word-entry{display:flex;flex-direction:column;align-items:stretch;gap:9px;padding:14px!important;min-width:0;min-height:178px;border:1px solid var(--border);border-radius:15px!important;background:linear-gradient(145deg,var(--panel),var(--panel2));transition:transform .18s,border-color .18s,box-shadow .18s}.admin-word-entry:hover{transform:translateY(-2px);border-color:rgba(40,116,240,.4);box-shadow:0 12px 28px var(--shadow)}.admin-word-info{display:grid;grid-template-columns:minmax(0,1fr);gap:2px;align-items:start}.admin-word-info .hangul{font-size:27px;line-height:1.25;font-weight:800;letter-spacing:.2px;color:var(--text);overflow-wrap:anywhere}.admin-word-info .pron{font-size:11px;color:var(--muted);overflow-wrap:anywhere}.admin-translation{font-size:14px;font-weight:750;overflow-wrap:anywhere}.admin-word-meta{display:flex;flex-wrap:wrap;gap:5px}.admin-word-meta .tag{font-size:10px;padding:4px 7px;border-radius:7px}.admin-word-example{margin:0;padding:7px 9px;border-radius:9px;background:rgba(40,116,240,.06);font-size:11px;line-height:1.4;overflow-wrap:anywhere}.admin-word-entry .word-actions{display:flex;gap:6px;align-items:center;margin-top:auto!important;padding-top:3px}.admin-word-entry .word-actions>a.btn,.admin-word-entry .word-actions form{flex:1 1 0;min-width:0;margin:0!important}.admin-word-entry .word-actions .btn{width:100%;padding:7px 8px;font-size:11px;white-space:nowrap}.admin-vocab-heading{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap;margin-top:24px}.admin-vocab-heading h2{margin:0!important}.admin-vocab-heading p{margin:3px 0 0;font-size:11px}.admin-vocab-search{display:flex;align-items:center;gap:7px;min-width:180px;max-width:250px;flex:1;padding:0 10px;border:1px solid var(--border);border-radius:11px;background:var(--surface);color:var(--muted)}.admin-vocab-search input{width:100%;min-width:0;padding:9px 0;border:0;outline:0;background:transparent;color:var(--text);font-size:12px}.admin-word-entry[hidden]{display:none!important}@media(max-width:520px){.admin-word-list{grid-template-columns:repeat(2,minmax(0,1fr));gap:7px}.admin-word-entry{padding:10px!important;min-height:165px}.admin-word-entry .word-actions{flex-wrap:wrap}.admin-word-entry .word-actions .btn{font-size:10px;padding:6px 4px}.admin-word-info .hangul{font-size:23px}.admin-translation{font-size:12px}.admin-vocab-search{max-width:none;flex-basis:100%}}@media(max-width:360px){.admin-word-list{grid-template-columns:1fr}}\n\n/* Keep the sidebar artwork at a fixed size on tall pages such as Admin. */\n.sidebar-art{height:230px;min-height:230px;flex:0 0 230px;background-size:cover;background-position:center 54%}\n\n.admin-layout{display:grid;grid-template-columns:minmax(280px,.85fr) minmax(0,1.15fr);gap:16px;align-items:start}.admin-forms{min-width:0}.admin-sections-panel{min-width:0}.admin-section-list{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,280px),1fr));gap:12px;margin-top:14px}.admin-section-card{min-width:0;padding:15px;border-radius:14px;display:grid;gap:10px}.admin-section-heading{display:flex;justify-content:space-between;align-items:center;gap:10px}.admin-section-title{display:flex;align-items:center;gap:9px;min-width:0}.admin-section-title h3{margin:0;font-size:16px;overflow-wrap:anywhere}.admin-section-icon{width:34px;height:34px;display:grid;place-items:center;background:var(--panel2);border-radius:10px;flex:0 0 auto}.admin-section-description{margin:-4px 0 0;color:var(--muted);font-size:13px;overflow-wrap:anywhere}.admin-section-form{display:grid;gap:9px;margin:0}.admin-edit-fields{display:grid;grid-template-columns:1fr;gap:8px}.admin-section-form input{min-width:0}.admin-delete-section{margin:0}.admin-words-heading{margin-top:24px!important;display:flex;align-items:center;gap:9px}.sr-only{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap;border:0}@media(max-width:1050px){.admin-layout{grid-template-columns:minmax(240px,.8fr) minmax(0,1.2fr);gap:12px}}@media(max-width:820px){.admin-layout{grid-template-columns:1fr}.admin-section-list{grid-template-columns:repeat(auto-fit,minmax(min(100%,260px),1fr))}}\n\n.admin-section-edit{display:grid;gap:9px}.admin-section-edit>summary{list-style:none;cursor:pointer;justify-self:start}.admin-section-edit>summary::-webkit-details-marker{display:none}.admin-section-edit[open]{padding-top:2px}.admin-section-edit[open]>summary{margin-bottom:2px}\n\n:root[data-theme='dark'] .notice{background:#172a58;border-color:#3b5190;color:#edf4ff;box-shadow:0 6px 18px rgba(0,0,0,.16)}\n\n.admin-section-actions{display:flex;align-items:center;gap:8px;flex-wrap:wrap}.admin-section-actions .admin-section-edit{display:block}.admin-section-actions .admin-delete-section{display:block}.admin-section-actions .admin-section-edit>summary,.admin-section-actions .admin-delete-section .btn{min-height:42px;display:flex;align-items:center;justify-content:center}\n\n.flash-hero-art{background-image:linear-gradient(90deg,rgba(11,25,66,.65) 0%,rgba(11,25,66,.24) 48%,rgba(11,25,66,.04) 100%),url('/rasmlar/image%20copy%2015.png');background-size:cover;background-position:center 52%}.flash-hero-art>*{display:none}.flash-hero:after{display:none}.flash-hero-copy,.flash-hero-copy h1,.flash-hero-copy p{color:#fff;text-shadow:0 2px 8px rgba(5,18,50,.5)}.flash-hero-copy p{opacity:.96}\n\n/* Figma HANGEUL dashboard */\n:root{--bg:#f2f6ff;--panel:#fff;--panel2:#f0f5ff;--surface:#fff;--text:#18347e;--muted:#496cb8;--border:#dce6fa;--purple:#6d46f5;--accent:#2d7ef2;--shadow:rgba(43,77,148,.08)}\n:root[data-theme='dark']{--bg:#060d2a;--panel:#0b1740;--panel2:#142554;--surface:#0c1942;--text:#eef3ff;--muted:#a8b9e8;--border:#1d3475;--purple:#7b4cff;--accent:#388cf6;--shadow:rgba(0,0,0,.2)}\nbody,:root[data-theme='dark'] body{background:var(--bg)}.topbar{height:50px;gap:18px;padding:0 18px;background:rgba(255,255,255,.92);border-color:var(--border)}:root[data-theme='dark'] .topbar{background:rgba(4,12,42,.96)}.brand{width:196px;gap:9px}.brand-mark{width:36px;height:36px;flex-basis:36px}.brand strong{font-size:14px}.brand small{font-size:8px}.top-search{height:34px;max-width:520px;box-shadow:none;background:var(--panel2)}.top-actions{gap:8px}.top-actions .top-control{height:34px;padding:0 9px;box-shadow:none;background:var(--panel2)}.theme-switch{display:flex;gap:2px;padding:2px;border:1px solid var(--border);border-radius:999px;background:var(--panel2)}.theme-switch .top-control{height:28px;width:28px;padding:0;border:0;background:transparent;border-radius:50%}.theme-switch .top-control:hover{background:rgba(103,76,245,.15)}.notification-button{width:34px;padding:0!important;font-size:16px!important}.profile-avatar{width:26px;height:26px}.layout{min-height:calc(100vh - 50px);grid-template-columns:192px minmax(0,1fr)}.sidebar{position:sticky;top:50px;height:calc(100vh - 50px);padding:12px 12px 14px;background:#f7f9ff;border-color:var(--border)}:root[data-theme='dark'] .sidebar{background:#060e2d}.side-nav{gap:3px}.side-nav a{height:36px;padding:0 10px;gap:10px;border-radius:8px;font-size:11px;color:var(--text)}.side-nav .nav-icon{width:18px;font-size:17px}.side-nav .nav-arrow{font-size:18px}.side-nav a.active{background:linear-gradient(100deg,#6941ff,#2d98f0);box-shadow:0 5px 15px rgba(72,96,232,.22)}.encouragement{margin:auto 0 0;min-height:62px;padding:8px;background:#eaf1ff;box-shadow:none;border:1px solid var(--border)}:root[data-theme='dark'] .encouragement{background:#0c1942}.encouragement p{color:var(--muted);font-size:8px}.cat{width:36px;height:34px}.content-area{min-width:0;padding:12px 14px 20px}.content-area>.footer{display:none}.dashboard{display:grid;grid-template-columns:minmax(0,2fr) minmax(270px,.96fr);gap:12px;align-items:start;max-width:1600px;margin:0 auto}.dashboard-main,.dashboard-side{min-width:0;display:grid;gap:12px}.dashboard-welcome{height:60px;border:1px solid rgba(112,132,255,.24);border-radius:14px;overflow:hidden;background:linear-gradient(90deg,rgba(5,15,53,.94) 0%,rgba(11,24,79,.66) 52%,rgba(13,30,91,.12) 100%),url('/rasmlar/image%20copy%2015.png') center 47%/cover;display:flex;align-items:center;padding:8px 16px}.welcome-copy{display:flex;align-items:center;gap:12px;color:#fff}.wave{font-size:25px}.welcome-copy h1{margin:0;font-size:16px;line-height:1.2}.welcome-copy p{margin:4px 0 0;font-size:9px;color:#dbe6ff}.dashboard-metrics{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:9px}.metric-card{min-width:0;min-height:68px;display:flex;align-items:center;gap:9px;padding:10px;border:1px solid var(--border);border-radius:13px;background:var(--panel);box-shadow:0 5px 16px var(--shadow);position:relative}.metric-icon,.shortcut-icon{width:34px;height:34px;flex:0 0 34px;display:grid;place-items:center;border-radius:11px;font-size:19px;background:#fff0e8;color:#ff673e}.metric-icon.xp,.shortcut-icon.xp{background:#f0eaff;color:#814eff}.metric-icon.books{background:#dffff7;color:#00a991}.metric-icon.star{background:#fbe5ff;color:#c34bec}.metric-card>div{display:grid;min-width:0;line-height:1.25}.metric-card small{font-size:8px;color:var(--muted)}.metric-card strong{font-size:13px;line-height:1.2;color:var(--text)}.metric-card span:not(.metric-icon){font-size:8px;color:var(--muted)}.metric-arrow{margin-left:auto;font-size:20px;color:var(--muted)}.xp-track{height:5px;min-width:70px;margin-top:4px;background:var(--panel2);border-radius:9px;overflow:hidden}.xp-track i{display:block;height:100%;width:48%;background:linear-gradient(90deg,#a03cff,#318cff);border-radius:9px}.xp-track.teal i{width:24%;background:#2bd2c2}.dashboard-shortcuts{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:8px}.dashboard-shortcuts a{min-width:0;min-height:49px;display:flex;align-items:center;gap:8px;padding:8px 9px;background:var(--panel);border:1px solid var(--border);border-radius:12px;transition:transform .15s,border-color .15s}.dashboard-shortcuts a:hover{transform:translateY(-2px);border-color:var(--purple)}.dashboard-shortcuts a.featured{background:linear-gradient(115deg,#8834f7,#4977ff);border-color:transparent;color:#fff}.dashboard-shortcuts a.featured small{color:#edf0ff}.shortcut-icon{width:27px;height:27px;flex-basis:27px;background:#f1eaff;font-size:16px}.dashboard-shortcuts a>span:last-child{display:grid;min-width:0}.dashboard-shortcuts strong{font-size:9px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.dashboard-shortcuts small{font-size:7px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.daily-quiz{min-height:114px;position:relative;overflow:hidden;border:1px solid var(--border);border-radius:14px;padding:13px 16px;background:linear-gradient(100deg,rgba(255,255,255,.98) 0%,rgba(255,255,255,.94) 62%,rgba(255,255,255,.58) 100%),url('/rasmlar/flash-card-banner.png') center 52%/cover;box-shadow:0 8px 22px var(--shadow);color:#223a84}.daily-quiz:before{content:'✿  ✦';position:absolute;right:14px;top:8px;color:#e78bf0;opacity:.5;font-size:19px}.quiz-heading{display:flex;align-items:center;gap:9px;position:relative;z-index:1}.quiz-spark{position:absolute;left:13px;top:11px;color:#a758fa;font-size:15px}.quiz-heading{padding-left:22px}.quiz-heading strong{font-size:12px}.quiz-heading p{margin:2px 0 0;font-size:9px;color:#4766b8}.quiz-action{position:relative;z-index:1;margin:10px 22px 0 30px;min-height:54px;display:flex;align-items:center;gap:10px;padding:8px 10px;border:1px solid #e4eaff;border-radius:12px;background:rgba(255,255,255,.9)}.quiz-question{width:34px;height:34px;flex:0 0 34px;display:grid;place-items:center;border-radius:12px;background:#8c55f8;color:white;font-size:23px;font-weight:800}.quiz-action>div{display:grid}.quiz-action strong{font-size:9px}.quiz-action small{font-size:8px;color:#7184bc}.difficulty{padding:3px 8px;border-radius:99px;background:#d5fff0;color:#049f7c;font-size:8px;font-weight:700}.quiz-start{margin-left:auto;height:35px;padding:0 18px;border-radius:99px;font-size:9px;background:linear-gradient(100deg,#7c35f5,#398bf8)!important;border:0!important;white-space:nowrap}.quiz-start span{font-size:14px}.side-panel{padding:12px;border:1px solid var(--border);border-radius:13px;background:var(--panel);box-shadow:0 5px 16px var(--shadow)}.panel-title{display:flex;align-items:center;gap:7px;font-size:10px}.panel-title>span:first-child{color:#9a55f4;font-size:14px}.panel-title strong{font-size:10px}.panel-title>b{margin-left:auto;font-size:9px}.progress-track{height:7px;margin-top:10px;border-radius:99px;background:var(--panel2);overflow:hidden}.progress-track i{display:block;width:40%;height:100%;background:linear-gradient(90deg,#8e3df7,#3299fb);border-radius:inherit}.achievements-panel{padding-bottom:10px}.panel-title>a{margin-left:auto;color:var(--muted);font-size:8px}.achievement-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:6px;margin-top:10px}.achievement-grid article{min-width:0;min-height:65px;padding:7px 3px;display:grid;justify-items:center;align-content:center;gap:2px;border:1px solid var(--border);border-radius:9px;background:var(--panel2);text-align:center}.achievement-grid b{width:22px;height:22px;display:grid;place-items:center;border-radius:7px;color:white;font-size:14px;box-shadow:0 3px 8px rgba(40,60,130,.18)}.achievement-grid strong{font-size:7px;white-space:nowrap}.achievement-grid small{font-size:7px;color:var(--muted)}.badge-orange{background:linear-gradient(140deg,#ffb22e,#ff6b2c)}.badge-fire{background:linear-gradient(140deg,#ff9d37,#ff4b37)}.badge-blue{background:linear-gradient(140deg,#9a64ff,#346bfa)}.badge-yellow{background:linear-gradient(140deg,#ffd84f,#ff9f22)}.daily-stats .panel-title{margin-bottom:10px}.today-pill{margin-left:auto;padding:3px 8px;border:1px solid var(--border);border-radius:99px;background:var(--panel2);font-size:8px;color:var(--muted)}.daily-stat-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:6px}.daily-stat-grid article{min-width:0;padding:8px 7px;border:1px solid var(--border);border-radius:9px;background:var(--panel2);display:grid;gap:3px}.daily-stat-grid b{font-size:9px;color:#168de5}.daily-stat-grid small{font-size:7px;color:var(--muted);white-space:nowrap}.daily-stat-grid article:nth-child(2) b{color:#77a512}.daily-stat-grid .wrong-stat b{color:#ef5365}.achievement-tile{text-align:center}.achievement-icon{width:54px;height:54px;margin:0 auto 8px;display:grid;place-items:center;border-radius:16px;color:white;font-size:26px}.settings-actions{display:flex;gap:10px;flex-wrap:wrap}.stats-page-metrics{margin-top:20px}\n@media(max-width:1050px){.dashboard{grid-template-columns:minmax(0,1.6fr) minmax(245px,.9fr)}.brand{width:160px}.dashboard-shortcuts{grid-template-columns:repeat(2,minmax(0,1fr))}.metric-card{gap:7px;padding:8px}.metric-icon{width:29px;height:29px;flex-basis:29px}.dashboard-shortcuts small{font-size:7px}}\n@media(max-width:760px){.topbar{height:auto;min-height:56px;flex-wrap:wrap;padding:8px 12px;gap:8px}.brand{width:auto;flex:1}.brand small{display:none}.top-search{order:3;flex-basis:100%;max-width:none}.top-actions{margin-left:0}.layout{display:block;min-height:calc(100vh - 56px)}.sidebar{position:static;width:100%;height:auto;padding:6px 8px;border-right:0;border-bottom:1px solid var(--border);overflow-x:auto}.side-nav{display:flex;min-width:max-content;gap:3px}.side-nav a{height:35px;padding:0 9px;gap:6px}.side-nav .nav-icon{width:auto}.side-nav .nav-arrow,.encouragement{display:none}.content-area{padding:10px}.dashboard{grid-template-columns:1fr}.dashboard-side{grid-template-columns:1fr}.dashboard-metrics{grid-template-columns:repeat(2,minmax(0,1fr))}.dashboard-shortcuts{grid-template-columns:repeat(2,minmax(0,1fr))}.dashboard-welcome{height:68px}.quiz-action{margin-left:0;margin-right:0}.quiz-start{padding:0 11px}.achievement-grid{gap:5px}}\n@media(max-width:420px){.profile-label{display:none}.top-actions .top-control{padding:0 7px}.daily-quiz{padding:11px}.quiz-action{gap:7px}.quiz-question{width:30px;height:30px;flex-basis:30px}.difficulty{display:none}.dashboard-shortcuts a{padding:7px}.metric-card{min-height:64px}}\n\n/* Dashboard background artwork by theme */\n.dashboard-welcome{background-image:linear-gradient(90deg,rgba(19,49,131,.45) 0%,rgba(24,61,147,.2) 50%,rgba(30,81,170,.02) 100%),url('/rasmlar/sunset-banner.png');background-position:center;background-size:cover}\n:root[data-theme='dark'] .dashboard-welcome{background-image:linear-gradient(90deg,rgba(5,13,43,.56) 0%,rgba(5,13,43,.22) 52%,rgba(5,13,43,.04) 100%),url('/rasmlar/night_buttom.png')}\n.daily-quiz{background-image:linear-gradient(100deg,rgba(255,255,255,.93) 0%,rgba(255,255,255,.8) 48%,rgba(255,255,255,.16) 100%),url('/rasmlar/image%20copy%2016.png');background-size:cover;background-position:center}\n:root[data-theme='dark'] .daily-quiz{background-image:linear-gradient(100deg,rgba(7,17,51,.9) 0%,rgba(7,17,51,.72) 48%,rgba(7,17,51,.2) 100%),url('/rasmlar/image%20copy%2016.png');color:#eef3ff}\n:root[data-theme='dark'] .quiz-heading p{color:#ccd8ff}:root[data-theme='dark'] .quiz-action{background:rgba(15,31,76,.9);border-color:#344a88;color:#eef3ff}:root[data-theme='dark'] .quiz-action small{color:#b6c5f2}\n\n/* Quiz page styling */\n.quiz-page{width:min(100%,860px);margin:0 auto;padding:4px 0 28px}.quiz-back{display:inline-flex;align-items:center;gap:7px;color:var(--muted);font-size:12px;font-weight:650;margin:0 0 12px}.quiz-page-heading{margin:0 0 14px}.quiz-kicker{color:var(--purple);font-size:9px;font-weight:800;letter-spacing:1.4px}.quiz-page-heading h1{font-size:25px;margin:3px 0;color:var(--text)}.quiz-page-heading p{margin:0;color:var(--muted);font-size:12px}.quiz-shell,.quiz-result{border:1px solid var(--border);border-radius:18px;background:var(--panel);box-shadow:0 12px 35px var(--shadow);padding:clamp(16px,3vw,26px)}.quiz-topline{display:flex;align-items:center;gap:12px}.quiz-count{display:flex;align-items:baseline;gap:4px}.quiz-count strong{font-size:20px;color:var(--purple)}.quiz-count span,.quiz-direction{font-size:11px;color:var(--muted)}.quiz-score{display:flex;gap:9px;margin-left:auto;font-size:11px;font-weight:750}.score-good{color:#12a67b}.score-bad{color:#f05b70}.quiz-direction{padding:5px 9px;border:1px solid var(--border);border-radius:99px;background:var(--panel2)}.quiz-progress{display:flex;gap:5px;margin:10px 0 18px}.quiz-progress span{height:5px;flex:1;border-radius:99px;background:var(--border)}.quiz-progress span.done{background:linear-gradient(90deg,#7041f5,#3394f7)}.quiz-progress span.current{background:linear-gradient(90deg,#a48aff,#7041f5)}.quiz-directions{display:flex;justify-content:center;gap:8px;flex-wrap:wrap;margin:0 0 16px}.quiz-directions a{padding:8px 12px;border:1px solid var(--border);border-radius:10px;background:var(--panel2);color:var(--muted);font-size:11px;font-weight:650}.quiz-directions a.selected{background:linear-gradient(105deg,#7041f5,#3988f6);border-color:transparent;color:#fff;box-shadow:0 5px 15px rgba(78,87,229,.18)}.quiz-question-card{position:relative;min-height:170px;padding:25px 64px;display:flex;flex-direction:column;align-items:center;justify-content:center;text-align:center;border:1px solid var(--border);border-radius:16px;background:linear-gradient(135deg,rgba(127,99,255,.08),rgba(56,151,244,.07)),var(--panel2)}.quiz-question-label,.quiz-hint{font-size:11px;color:var(--muted)}.quiz-prompt{margin:8px 0;font-size:clamp(34px,7vw,50px);line-height:1.2;font-weight:800;overflow-wrap:anywhere;color:var(--text);font-family:'Noto Sans KR',Inter,sans-serif}.quiz-speak{position:absolute;right:16px;top:50%;transform:translateY(-50%);width:48px;height:48px;display:grid;place-items:center;border:1px solid var(--border);border-radius:50%;background:var(--panel);color:var(--accent);font-size:19px}.quiz-speak small{font-size:8px}.quiz-options-form{margin-top:16px}.quiz-options{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:9px}.quiz-option{min-height:54px;display:flex;align-items:center;gap:10px;padding:9px 12px;border:1px solid var(--border);border-radius:12px;background:var(--panel);cursor:pointer;transition:border-color .15s,background .15s,transform .15s}.quiz-option:hover{border-color:var(--purple);transform:translateY(-1px)}.quiz-option input{position:absolute;opacity:0;width:1px;height:1px}.quiz-option-number{width:26px;height:26px;display:grid;place-items:center;flex:0 0 26px;border-radius:8px;background:var(--panel2);color:var(--muted);font-size:11px;font-weight:750}.quiz-option-text{flex:1;min-width:0;font-weight:650;overflow-wrap:anywhere;font-family:'Noto Sans KR',Inter,sans-serif}.quiz-option-check{width:17px;height:17px;flex:0 0 17px;border:1.5px solid var(--border);border-radius:50%}.quiz-option:has(input:checked){border-color:var(--purple);background:rgba(112,65,245,.08)}.quiz-option:has(input:checked) .quiz-option-number{background:var(--purple);color:white}.quiz-option:has(input:checked) .quiz-option-check{border:5px solid var(--purple);background:white}.quiz-submit{width:100%;margin-top:13px;min-height:44px;border-radius:11px;background:linear-gradient(100deg,#753cf3,#3888f6)!important;border:0!important}.quiz-submit span{margin-left:5px;font-size:15px}.quiz-empty{max-width:550px;margin:40px auto;padding:30px;text-align:center;border:1px solid var(--border);border-radius:18px;background:var(--panel);box-shadow:0 12px 35px var(--shadow)}.quiz-empty-icon,.quiz-result-icon{font-size:38px}.quiz-empty h1{margin:8px 0}.quiz-empty p{color:var(--muted)}.quiz-result{text-align:center;max-width:760px;margin:0 auto;padding:16px 20px!important}.quiz-result-icon{font-size:27px;line-height:1}.quiz-result h1{margin:3px 0;font-size:21px}.quiz-result-subtitle{color:var(--muted);margin:0;font-size:12px}.quiz-result-stats{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:7px;margin:11px 0}.quiz-result-stats article{padding:8px 10px;border:1px solid var(--border);border-radius:10px;background:var(--panel2);display:grid;gap:2px}.quiz-result-stats small,.quiz-review-item small{color:var(--muted);font-size:10px}.quiz-result-stats strong{font-size:18px}.quiz-result-stats .correct strong,.review-correct{color:#12a67b}.quiz-result-stats .incorrect strong,.review-wrong{color:#f05b70}.quiz-review{text-align:left}.quiz-review h2{font-size:13px;margin:8px 0 5px}.quiz-review-item{display:flex;justify-content:space-between;align-items:center;gap:9px;padding:6px 9px;border:1px solid var(--border);border-radius:9px;background:var(--panel2);margin:7px 0}.quiz-review-item>div{display:grid;gap:3px;min-width:0}.quiz-review-item>span{white-space:nowrap;font-size:11px;font-weight:750}.quiz-review{max-height:230px;overflow-y:auto;padding-right:3px}.quiz-review-item>div{display:grid;gap:1px;min-width:0}.quiz-review-item strong{font-size:11px;line-height:1.35}.quiz-review-item>span{font-size:10px}.quiz-result-actions{display:flex;justify-content:center;gap:7px;flex-wrap:wrap;margin-top:10px}.quiz-result-actions .btn{padding:8px 13px;font-size:12px}\n@media(max-width:600px){.quiz-page{padding-top:0}.quiz-shell,.quiz-result{padding:14px;border-radius:15px}.quiz-topline{gap:7px}.quiz-direction{font-size:9px;padding:4px 7px}.quiz-options{grid-template-columns:1fr}.quiz-question-card{min-height:145px;padding:22px 44px}.quiz-speak{right:7px;width:38px;height:38px;font-size:16px}.quiz-result-stats{gap:6px}.quiz-result-stats article{padding:10px 6px}.quiz-result-stats small{font-size:9px}.quiz-review-item{align-items:flex-start}.quiz-result-actions>*{flex:1}}\n.top-search{width:40px;height:40px;max-width:550px;flex:0 0 40px;padding:0;border-radius:50%;overflow:hidden;transition:width .24s,flex-basis .24s,border-radius .2s}.top-search.expanded{width:clamp(150px,14.4vw,220px);height:40px;max-width:220px;min-width:0;flex:0 1 220px;padding:0 12px 0 2px;border-radius:28px}.top-search:not(.expanded) input,.top-search:not(.expanded) kbd{display:none}.search-toggle{width:38px;height:38px;flex:0 0 38px;display:flex;align-items:center;justify-content:center;text-align:center;margin:0;border:0;border-radius:50%;padding:0;background:transparent;color:#2361e8;font-size:25px;line-height:1}.search-toggle span{display:block;line-height:1;transform:translate(1px,-1px)}.top-search.expanded .search-toggle{border-radius:20px}.top-search input{min-width:0}.top-search.expanded input{display:block}.top-search.expanded kbd{display:block}@media(max-width:760px){.top-search:not(.expanded){order:initial;flex:0 0 42px;width:42px;max-width:42px;height:42px}.top-search.expanded{order:3;flex:1 0 100%;width:100%;max-width:none;height:42px}.search-toggle{width:40px;height:40px;flex-basis:40px}}\n.content-area.glass-content{background:var(--panel);border:1px solid rgba(103,145,205,.16);border-radius:22px;backdrop-filter:blur(14px);-webkit-backdrop-filter:blur(14px);box-shadow:0 8px 28px var(--shadow)}\n.practice-shell{background:var(--panel);border:1px solid rgba(103,145,205,.16);border-radius:22px;padding:20px;box-shadow:0 8px 28px var(--shadow);backdrop-filter:blur(14px);-webkit-backdrop-filter:blur(14px)}@media(max-width:520px){.practice-shell{padding:14px;border-radius:18px}}\n"
STYLE += r"""

/* So‘zlar boshqaruvi: desktopda to‘liq kenglik, qidiruv va ishlaydigan filtrlar. */
.admin-vocabulary-panel{grid-column:1/-1;margin-top:18px;padding:24px!important;border:1px solid rgba(90,139,225,.22);border-radius:22px!important;background:linear-gradient(145deg,rgba(255,255,255,.97),rgba(241,247,255,.94));box-shadow:0 14px 36px rgba(54,101,178,.12)}
:root[data-theme='dark'] .admin-vocabulary-panel{background:linear-gradient(145deg,rgba(17,29,64,.98),rgba(13,23,51,.97));border-color:rgba(101,142,232,.25)}
.admin-vocabulary-panel .admin-vocab-heading{display:flex;justify-content:space-between;gap:24px;margin:0 0 18px;padding:0 0 16px;border-bottom:1px solid var(--border);align-items:center}
.admin-vocabulary-panel .admin-words-heading{margin:0!important;display:flex;align-items:center;gap:10px;font-size:25px;letter-spacing:-.35px}
.admin-vocabulary-panel .admin-words-heading .tag{display:inline-flex;align-items:center;justify-content:center;min-width:31px;height:27px;padding:0 9px;border-radius:9px;background:linear-gradient(120deg,#6d49ef,#2e8df4);color:#fff;font-size:12px;box-shadow:0 4px 12px rgba(76,104,232,.2)}
.admin-vocabulary-panel .admin-vocab-heading p{margin:5px 0 0;font-size:12px}
.admin-vocabulary-panel .admin-vocab-tools{display:grid;grid-template-columns:minmax(300px,1.5fr) minmax(190px,1fr) minmax(190px,1fr);gap:12px;margin:0 0 18px;padding:14px;border:1px solid var(--border);border-radius:15px;background:var(--panel2)}
.admin-vocabulary-panel .admin-vocab-search{display:flex;align-items:center;gap:9px;min-width:0;min-height:43px;padding:0 13px;border:1px solid var(--border);border-radius:11px;background:var(--surface);box-shadow:0 4px 12px rgba(45,94,170,.06)}
.admin-vocabulary-panel .admin-vocab-search span{font-size:22px;color:var(--accent)}
.admin-vocabulary-panel .admin-vocab-search input{width:100%;min-width:0;border:0;outline:0;background:transparent;color:var(--text);font-size:13px}
.admin-vocabulary-panel .admin-vocab-tools select{width:100%;min-height:43px;padding:0 12px;border:1px solid var(--border);border-radius:11px;background:var(--surface);color:var(--text)}
.admin-vocabulary-panel .admin-vocab-result{margin:0 0 12px;color:var(--muted);font-size:12px}
.admin-vocabulary-panel .admin-word-list{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));align-items:stretch;gap:14px;margin-top:0}
.admin-vocabulary-panel .admin-word-entry{position:relative;gap:10px;min-height:0;padding:17px!important;border-radius:17px!important;border:1px solid rgba(112,153,225,.24);background:linear-gradient(150deg,var(--surface),var(--panel2));box-shadow:0 5px 16px rgba(46,88,157,.055)}
.admin-vocabulary-panel .admin-word-entry:before{content:'한글';position:absolute;right:13px;top:12px;display:grid;place-items:center;width:31px;height:31px;border-radius:10px;background:linear-gradient(140deg,rgba(114,75,242,.13),rgba(51,151,245,.15));color:var(--accent);font-size:10px;font-weight:850;letter-spacing:.2px}
.admin-vocabulary-panel .admin-word-entry:hover{transform:translateY(-2px);border-color:rgba(67,123,235,.48);box-shadow:0 13px 24px rgba(50,96,176,.13)}
.admin-vocabulary-panel .admin-word-info{padding-right:35px;gap:4px}
.admin-vocabulary-panel .admin-word-info .hangul{font-size:30px;line-height:1.25;letter-spacing:.1px}
.admin-vocabulary-panel .admin-word-info .pron{font-size:12px}
.admin-vocabulary-panel .admin-translation{font-size:15px;line-height:1.45}
.admin-vocabulary-panel .admin-word-meta{gap:6px}
.admin-vocabulary-panel .admin-word-meta .tag{max-width:100%;overflow-wrap:anywhere;padding:5px 8px;border-radius:8px;background:rgba(75,123,224,.09)}
.admin-vocabulary-panel .admin-word-example{margin:0;padding:10px 11px;border:1px solid rgba(83,133,221,.11);border-radius:10px;font-size:12px;line-height:1.5}
.admin-vocabulary-panel .admin-word-entry .word-actions{gap:8px;padding-top:9px;border-top:1px solid var(--border)}
.admin-vocabulary-panel .admin-word-entry .word-actions .btn{min-height:36px;padding:8px 11px;border-radius:9px;font-size:12px}
.admin-vocabulary-panel .admin-word-entry .word-actions>a.btn{background:rgba(61,119,236,.09);border-color:rgba(61,119,236,.15);color:var(--accent)}
.admin-vocabulary-panel .admin-word-entry .word-actions .btn.danger{background:rgba(227,79,107,.08)}
.admin-vocabulary-panel .admin-word-entry[hidden]{display:none!important}
.admin-vocabulary-panel .admin-no-results{padding:26px;text-align:center;border:1px dashed var(--border);border-radius:14px;color:var(--muted)}
"""
STYLE += r"""

/* HANGEUL admin: Fibonacci / golden-ratio composition for desktop. */
.fibonacci-admin{display:grid;grid-template-columns:minmax(0,1.618fr) minmax(330px,1fr);gap:21px;align-items:start;margin-top:18px}
.fibonacci-admin>.admin-vocabulary-panel{grid-column:auto;grid-row:1;margin:0!important;min-width:0;padding:25px!important;border-radius:24px!important;background:linear-gradient(145deg,rgba(255,255,255,.97),rgba(239,246,255,.96));box-shadow:0 18px 46px rgba(40,83,161,.13);border:1px solid rgba(91,132,218,.2)}
:root[data-theme='dark'] .fibonacci-admin>.admin-vocabulary-panel{background:linear-gradient(145deg,rgba(17,29,64,.98),rgba(13,23,51,.97))}
.fibonacci-admin .admin-vocab-heading{margin:0 0 20px;padding:0 0 17px;border-bottom:1px solid var(--border);align-items:flex-start}
.fibonacci-admin .admin-vocab-heading h2{font-size:25px;letter-spacing:-.45px}
.fibonacci-admin .admin-vocab-heading p{font-size:12px;line-height:1.6;max-width:460px}
.fibonacci-admin .admin-vocab-tools{display:grid;grid-template-columns:minmax(0,1.5fr) minmax(145px,1fr);gap:9px;margin:0 0 12px}
.fibonacci-admin .admin-vocab-search{max-width:none;min-width:0;height:43px;border-radius:12px;background:var(--surface)}
.fibonacci-admin .admin-vocab-tools select{width:100%;min-width:0;height:43px;border-radius:12px;font-size:12px}
.fibonacci-admin .admin-vocab-tools select:last-child{grid-column:2}
.fibonacci-admin .admin-word-list{grid-template-columns:repeat(2,minmax(0,1fr));gap:12px;margin-top:14px}
.fibonacci-admin .admin-word-entry{min-height:195px;padding:15px!important;border-radius:17px!important;box-shadow:0 5px 18px rgba(46,83,153,.055)}
.fibonacci-admin .admin-word-entry .hangul{font-size:29px}
.fibonacci-admin .admin-word-example{font-size:12px;line-height:1.55}
.fibonacci-admin .admin-word-entry .word-actions{margin-top:auto}
.fibonacci-admin .admin-vocab-result{font-size:11px;margin:4px 0 10px;color:var(--muted)}
.fibonacci-admin .admin-rail{display:grid;grid-template-columns:minmax(0,1fr);gap:18px;min-width:0}
.fibonacci-admin .admin-forms{display:grid;gap:16px;min-width:0}
.fibonacci-admin .admin-forms>.panel{margin:0!important;padding:20px!important;border-radius:20px!important;background:rgba(255,255,255,.9);box-shadow:0 12px 32px rgba(40,83,161,.09)}
:root[data-theme='dark'] .fibonacci-admin .admin-forms>.panel{background:rgba(17,29,64,.94)}
.fibonacci-admin .admin-forms h2,.fibonacci-admin .admin-sections-panel h2{font-size:17px;letter-spacing:-.2px;margin:0 0 7px}
.fibonacci-admin .admin-forms .muted{font-size:11px;line-height:1.55}
.fibonacci-admin .admin-sections-panel{padding:20px!important;border-radius:20px!important;background:rgba(255,255,255,.9);box-shadow:0 12px 32px rgba(40,83,161,.09)}
:root[data-theme='dark'] .fibonacci-admin .admin-sections-panel{background:rgba(17,29,64,.94)}
.fibonacci-admin .admin-sections-topic-row{display:grid;grid-template-columns:minmax(0,1.15fr) minmax(0,.85fr);gap:12px;align-items:stretch;min-width:0}
.fibonacci-admin .admin-topic-system-panel{min-width:0;padding:18px!important;border-radius:20px!important;background:rgba(255,255,255,.9);box-shadow:0 12px 32px rgba(40,83,161,.09);display:flex;flex-direction:column;align-items:flex-start;gap:10px}
:root[data-theme='dark'] .fibonacci-admin .admin-topic-system-panel{background:rgba(17,29,64,.94)}
.fibonacci-admin .admin-topic-system-panel h2{font-size:16px;line-height:1.35;margin:0}
.fibonacci-admin .admin-topic-system-panel p{font-size:11px;line-height:1.55;margin:0}
.fibonacci-admin .admin-topic-system-panel .btn{font-size:11px;line-height:1.4;white-space:normal;text-align:center;margin-top:auto}
.fibonacci-admin .admin-section-list{grid-template-columns:minmax(0,1fr);gap:9px;margin-top:12px}
.fibonacci-admin .admin-section-card{padding:12px!important;border-radius:14px!important;gap:8px;background:linear-gradient(135deg,var(--panel),var(--panel2))}
.fibonacci-admin .admin-section-title h3{font-size:14px}
.fibonacci-admin .admin-section-actions{display:flex;gap:8px;align-items:center}
.fibonacci-admin .admin-section-actions .btn{font-size:11px;padding:7px 10px}
/* Keep the section editor floating so opening it never pushes the panels below. */
.fibonacci-admin .admin-section-card{position:relative;overflow:visible}
.fibonacci-admin .admin-section-actions{position:relative;z-index:2}
.fibonacci-admin .admin-section-actions .admin-section-edit{position:relative}
.fibonacci-admin .admin-section-actions .admin-section-edit[open]{z-index:40}
.fibonacci-admin .admin-section-actions .admin-section-edit .admin-section-form{position:absolute;top:calc(100% + 8px);left:0;z-index:50;width:min(320px,calc(100vw - 48px));padding:14px;background:var(--surface);border:1px solid var(--border);border-radius:14px;box-shadow:0 16px 40px rgba(20,50,110,.24)}
.fibonacci-admin .admin-section-actions .admin-section-edit .admin-section-form .btn{width:100%;justify-content:center}
.fibonacci-admin .admin-word-entry[hidden]{display:none!important}
/* Desktop dashboard: first row = create + existing sections; topic system spans the next row; vocabulary + add-word share the following row. */
.fibonacci-admin{display:grid;grid-template-columns:repeat(2,minmax(0,1fr))!important;grid-template-areas:"create-section existing-sections" "vocabulary add-word";align-items:start;gap:18px!important}
.fibonacci-admin>.admin-vocabulary-panel{grid-area:vocabulary;min-width:0}
.fibonacci-admin>.admin-rail{display:contents}
.fibonacci-admin .admin-sections-topic-row{display:contents}
.fibonacci-admin .admin-sections-panel{grid-area:existing-sections;min-width:0}
.fibonacci-admin .admin-topic-system-panel{min-width:0}
.fibonacci-admin .admin-forms{display:contents}
.fibonacci-admin .admin-forms>.panel:first-child{grid-area:create-section;min-width:0}
.fibonacci-admin .admin-forms>.panel:last-child{grid-area:add-word;min-width:0}
.fibonacci-admin .admin-forms>.panel:last-child{margin-top:0!important}
.fibonacci-admin .admin-section-list{grid-template-columns:repeat(5,minmax(0,1fr));gap:9px}
@media(max-width:1000px){.fibonacci-admin{grid-template-columns:repeat(2,minmax(0,1fr))!important}.fibonacci-admin .admin-section-list{grid-template-columns:minmax(0,1fr)}}
@media(min-width:1500px){.fibonacci-admin .admin-word-list{grid-template-columns:repeat(2,minmax(0,1fr))}.fibonacci-admin .admin-vocab-tools{grid-template-columns:minmax(0,1.6fr) minmax(120px,.8fr) minmax(130px,.8fr)}.fibonacci-admin .admin-vocab-tools select:last-child{grid-column:auto}}
@media(max-width:760px){.fibonacci-admin{grid-template-columns:minmax(0,1fr)!important;grid-template-areas:"create-section" "existing-sections" "vocabulary" "add-word"}}
/* Existing section file cards: reduce each card width by about 60% versus the old two-column layout. */
.fibonacci-admin .admin-section-list{grid-template-columns:repeat(5,minmax(0,1fr));gap:9px}
.fibonacci-admin .admin-section-card{min-width:0;padding:9px!important;gap:7px}
.fibonacci-admin .admin-section-title{min-width:0;flex-wrap:wrap;gap:6px}
.fibonacci-admin .admin-section-title h3{min-width:0;overflow-wrap:anywhere}
.fibonacci-admin .admin-section-title .count-badge{white-space:nowrap}
.fibonacci-admin .admin-section-actions{flex-wrap:wrap;gap:5px}
.fibonacci-admin .admin-section-actions .btn{padding:6px 7px;font-size:10px}
@media(max-width:1250px){.fibonacci-admin .admin-section-list{grid-template-columns:repeat(3,minmax(0,1fr))}}
/* Inner scrolling only: keep the existing panel layout and card shapes unchanged. */
.fibonacci-admin .admin-sections-panel{min-width:0;overflow:visible}
.fibonacci-admin .admin-section-list{max-height:420px;overflow-y:auto;overscroll-behavior:contain;scrollbar-gutter:stable;padding:2px 9px 8px 0;scrollbar-width:thin;scrollbar-color:var(--accent) var(--panel2)}
.fibonacci-admin .admin-vocabulary-panel{min-width:0;max-height:none;overflow:visible}
.fibonacci-admin .admin-word-list{max-height:min(58vh,560px);overflow-y:auto;overscroll-behavior:contain;scrollbar-gutter:stable;scrollbar-width:thin;scrollbar-color:var(--accent) var(--panel2);align-content:start;padding:2px 9px 10px 0}
.fibonacci-admin .admin-section-list::-webkit-scrollbar,.fibonacci-admin .admin-word-list::-webkit-scrollbar{width:9px}
.fibonacci-admin .admin-section-list::-webkit-scrollbar-thumb,.fibonacci-admin .admin-word-list::-webkit-scrollbar-thumb{background:linear-gradient(180deg,var(--accent),var(--purple));border:2px solid var(--surface);border-radius:20px}
.fibonacci-admin .admin-section-list::-webkit-scrollbar-track,.fibonacci-admin .admin-word-list::-webkit-scrollbar-track{background:var(--panel2);border-radius:20px}


/* FIX: faqat 📁 Mavjud bo‘limlar ro‘yxati — 2x2 va ichki scroll */
.fibonacci-admin .admin-sections-panel .admin-section-list {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 10px;
    max-height: 390px;
    overflow-y: auto;
    overflow-x: hidden;
    align-content: start;
    min-height: 0;
    padding: 4px 8px 4px 4px;
    box-sizing: border-box;
    scrollbar-width: thin;
    overscroll-behavior: contain;
}
.fibonacci-admin .admin-sections-panel .admin-section-list > .admin-section-card {
    min-width: 0;
    box-sizing: border-box;
}
.fibonacci-admin .admin-sections-panel .admin-section-list::-webkit-scrollbar {
    width: 7px;
}
.fibonacci-admin .admin-sections-panel .admin-section-list::-webkit-scrollbar-thumb {
    background: #a9bce5;
    border-radius: 8px;
}

"""


BASE = '''<!doctype html><html lang="uz"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{{ title }} · Hangeul</title><link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin><link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=Noto+Sans+KR:wght@500;600;700&display=swap" rel="stylesheet"><style>''' + STYLE + '''</style><script>
(function(){try{const saved=localStorage.getItem('hangeul-theme');document.documentElement.dataset.theme=saved||'light';}catch(e){document.documentElement.dataset.theme='light';}})();
function setTheme(mode){document.documentElement.dataset.theme=mode;try{localStorage.setItem('hangeul-theme',mode);}catch(e){}}function toggleTheme(){setTheme(document.documentElement.dataset.theme==='dark'?'light':'dark');}

// Flash notices (including Google Drive sync status) disappear after 3 seconds.
document.addEventListener('DOMContentLoaded',function(){document.querySelectorAll('.notice').forEach(function(notice){window.setTimeout(function(){notice.classList.add('notice-hidden');window.setTimeout(function(){notice.remove();},300);},3000);});});

async function speakKorean(text){try{const response=await fetch('/audio?text='+encodeURIComponent(text));if(!response.ok){alert('Ovoz yaratilmadi: '+await response.text());return;}const audioUrl=URL.createObjectURL(await response.blob());const player=new Audio(audioUrl);player.onended=()=>URL.revokeObjectURL(audioUrl);await player.play();}catch(err){alert('Ovoz ijro etilmadi. Internet aloqasini tekshirib, qayta urinib ko‘ring.');}}
</script></head><body><div class="app-shell"><header id="page-top" class="topbar"><a class="brand" href="{{ url_for('vocabulary') }}"><span class="brand-mark"><img src="/rasmlar/logo_sun.png" alt="" aria-hidden="true"></span><span><strong>HANGEUL</strong><small>Koreys tili — yangi imkoniyatlar!</small></span></a><div class="top-actions"><div class="theme-switch" aria-label="Ko‘rinish rejimi"><button class="top-control" type="button" onclick="setTheme('light')" title="Kunduzgi rejim" aria-label="Kunduzgi rejim">☀️</button><button class="top-control" type="button" onclick="setTheme('dark')" title="Tungi rejim" aria-label="Tungi rejim">☾</button></div><a class="top-control achievements-top-link" href="{{ url_for('achievements') }}" title="Yutuqlar">🏆 <span>Yutuqlar</span></a><a class="top-control admin-top-link" href="{{ url_for('admin') if session.get('admin_logged_in') else url_for('login') }}"><span>Admin</span></a><a class="top-control settings-top-link" href="{{ url_for('settings') }}" title="Sozlamalar" aria-label="Sozlamalar">⚙</a></div></header><div class="layout"><aside class="sidebar"><nav class="side-nav" aria-label="Asosiy navigatsiya">{% if session.get('active_topic_day_id') and session.get('active_topic_round') %}<a class="{{ 'active' if request.endpoint == 'marathon' else '' }}" href="{{ url_for('marathon') }}"><span class="nav-icon">🃏</span><span>Flashcard</span></a><a class="{{ 'active' if request.endpoint == 'matching' else '' }}" href="{{ url_for('matching') }}"><span class="nav-icon">🧩</span><span>Aqli moslash</span></a><a class="{{ 'active' if request.endpoint == 'viktorina' else '' }}" href="{{ url_for('viktorina', restart=1) }}"><span class="nav-icon">🏆</span><span>VIKTORINA</span></a><a class="{{ 'active' if request.endpoint == 'writing' else '' }}" href="{{ url_for('writing') }}"><span class="nav-icon">✍</span><span>Wongoji test</span></a><a href="{{ url_for('topic_folder_view', folder_id=session.get('active_topic_folder_id')) }}"><span class="nav-icon">←</span><span>Ortga</span></a>{% else %}<a class="{{ 'active' if request.endpoint == 'vocabulary' else '' }}" href="{{ url_for('vocabulary') }}"><span class="nav-icon">⌂</span><span>So‘z qidirish</span></a><a class="{{ 'active' if request.endpoint in ['topics','section_words'] else '' }}" href="{{ url_for('topics') }}"><span class="nav-icon">▤</span><span>Mavzular</span><b class="nav-arrow">›</b></a><a class="{{ 'active' if request.endpoint in ['grammar_page','grammar_admin'] else '' }}" href="{{ url_for('grammar_page') }}"><span class="nav-icon">📝</span><span>Grammatika</span><b class="nav-arrow">›</b></a><a class="{{ 'active' if request.endpoint in ['books_page','books_admin'] else '' }}" href="{{ url_for('books_page') }}"><span class="nav-icon">📚</span><span>Kitoblar</span><b class="nav-arrow">›</b></a><a class="{{ 'active' if request.endpoint in ['dramas_page','dramas_admin','drama_study'] else '' }}" href="{{ url_for('dramas_page') }}"><span class="nav-icon">🎬</span><span>Dramalar</span><b class="nav-arrow">›</b></a>{% endif %}</nav><div class="encouragement"><img class="encouragement-flag" src="/rasmlar/flag.png" alt=""><p>Bugun ham o‘qisang,<br>orzularingga yaqinlashasan!</p><span class="heart">♥</span></div></aside><div class="content-area">{% with messages = get_flashed_messages() %}{% if messages %}{% for message in messages %}<div class="notice">{{ message }}</div>{% endfor %}{% endif %}{% endwith %}{{ body|safe }}<div class="footer">Hangeul · Koreys tilini bosqichma-bosqich o‘rganamiz</div></div></div></div></body></html>'''
def page(title, body, **context):
    return render_template_string(BASE, title=title, body=render_template_string(body, **context))


@app.route('/rasmlar/<path:filename>')
def local_image(filename):
    image_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'rasmlar')
    return send_from_directory(image_dir, filename)

_WORD_CATALOG_CTE = '''
WITH raw_words AS (
 SELECT DISTINCT TRIM(COALESCE(korean,'')) korean, TRIM(COALESCE(uzbek,'')) uzbek, TRIM(COALESCE(pronunciation,'')) pronunciation, COALESCE(NULLIF(TRIM(category),''),'Umumiy') context
 FROM words WHERE TRIM(COALESCE(korean,''))<>'' AND TRIM(COALESCE(uzbek,''))<>''
 UNION ALL
 SELECT DISTINCT TRIM(COALESCE(tw.korean,'')), TRIM(COALESCE(tw.uzbek,'')), TRIM(COALESCE(tw.pronunciation,'')), TRIM(f.name || ' / ' || d.name || ' / Round ' || tw.round_number)
 FROM topic_words tw JOIN topic_days d ON d.id=tw.day_id JOIN topic_folders f ON f.id=d.folder_id
 WHERE TRIM(COALESCE(tw.korean,''))<>'' AND TRIM(COALESCE(tw.uzbek,''))<>''
), grouped_words AS (
 SELECT MAX(korean) korean, MAX(uzbek) uzbek, MAX(NULLIF(pronunciation,'')) pronunciation, GROUP_CONCAT(DISTINCT context) context
 FROM raw_words GROUP BY lower(korean),lower(uzbek)
)
'''
WORD_CATALOG_PAGE_SIZE = 30
def _word_catalog_page(query='', page_number=1):
 query=(query or '').strip(); like=f'%{query.lower()}%'
 where="?='' OR lower(korean) LIKE ? OR lower(uzbek) LIKE ? OR lower(COALESCE(pronunciation,'')) LIKE ? OR lower(COALESCE(context,'')) LIKE ?"; params=(query,like,like,like,like)
 with db() as conn:
  total=conn.execute(_WORD_CATALOG_CTE+'SELECT COUNT(*) FROM grouped_words WHERE '+where,params).fetchone()[0]
  pages=max(1,(total+WORD_CATALOG_PAGE_SIZE-1)//WORD_CATALOG_PAGE_SIZE); page_number=min(max(1,int(page_number or 1)),pages)
  rows=conn.execute(_WORD_CATALOG_CTE+'SELECT korean,uzbek,pronunciation,context FROM grouped_words WHERE '+where+' ORDER BY korean COLLATE NOCASE,uzbek COLLATE NOCASE LIMIT ? OFFSET ?',params+(WORD_CATALOG_PAGE_SIZE,(page_number-1)*WORD_CATALOG_PAGE_SIZE)).fetchall()
 return [dict(r) for r in rows],total,pages,page_number
@app.route('/api/word-catalog')
def word_catalog_api():
 q=request.args.get('q','').strip()
 try: page_number=int(request.args.get('page','1'))
 except (TypeError,ValueError): page_number=1
 words,total,pages,page_number=_word_catalog_page(q,page_number)
 return Response(json.dumps({'words':words,'total':total,'pages':pages,'page':page_number},ensure_ascii=False),mimetype='application/json')
def word_catalog_home():
 q=request.args.get('q','').strip()
 try: page_number=int(request.args.get('page','1'))
 except (TypeError,ValueError): page_number=1
 words,total,pages,page_number=_word_catalog_page(q,page_number)
 body=r'''<main class="word-catalog"><section class="hero"><div class="eyebrow">HANGEUL • LUG‘AT</div><h1>So‘z qidirish</h1><p class="muted">Barcha mavzular, papkalar, DAY va Round’lardagi so‘zlarni koreyscha, o‘qilishi yoki tarjimasi orqali toping.</p></section>
<form class="searchrow" id="catalog-search" role="search"><input id="catalog-query" name="q" value="{{ q }}" placeholder="Koreyscha so‘z, o‘qilishi yoki o‘zbekcha tarjima…" autocomplete="off" aria-label="So‘z qidirish"><button class="btn primary" type="submit">Qidirish</button></form><p class="muted small" id="catalog-count" aria-live="polite">{{ total }} ta so‘z · {{ page_number }} / {{ pages }}-sahifa</p>
<div class="cards" id="catalog-results">{% for w in words %}<article class="word"><div style="display:flex;align-items:center;justify-content:space-between;gap:10px"><div class="hangul">{{ w.korean }}</div><button class="section-audio" type="button" onclick="speakKorean(this.dataset.korean)" data-korean="{{ w.korean }}" aria-label="Koreyscha talaffuzni eshitish" title="Talaffuzni eshitish">🔊</button></div><div class="pron">{{ w.pronunciation or 'O‘qilishi kiritilmagan' }}</div><div class="translation">{{ w.uzbek }}</div><span class="tag">{{ w.context or 'Mavzu ko‘rsatilmagan' }}</span></article>{% else %}<div class="empty">So‘z topilmadi</div>{% endfor %}</div><nav class="catalog-pagination" id="catalog-pagination" aria-label="So‘zlar sahifalari"><button class="btn" type="button" data-page="{{ page_number-1 }}" {% if page_number<=1 %}disabled{% endif %}>← Oldingi</button><span class="muted small">{{ page_number }} / {{ pages }}</span><button class="btn" type="button" data-page="{{ page_number+1 }}" {% if page_number>=pages %}disabled{% endif %}>Keyingi →</button></nav></main>
<style>.word-catalog{max-width:1200px;margin:0 auto}.word-catalog .hero{margin-bottom:18px}.word-catalog h1{font-size:clamp(28px,4vw,40px)}.catalog-pagination{display:flex;align-items:center;justify-content:center;gap:18px;margin:24px 0}.catalog-pagination button:disabled{opacity:.45;cursor:default}@media(max-width:600px){.word-catalog{padding:0 2px}.word-catalog .searchrow{gap:7px}.word-catalog .searchrow input{min-width:0}.word-catalog .cards{grid-template-columns:1fr}}</style>
<script>(()=>{const form=document.getElementById('catalog-search'),input=document.getElementById('catalog-query'),results=document.getElementById('catalog-results'),count=document.getElementById('catalog-count'),nav=document.getElementById('catalog-pagination');let timer;function card(w){const a=document.createElement('article');a.className='word';const head=document.createElement('div');head.style.display='flex';head.style.alignItems='center';head.style.justifyContent='space-between';head.style.gap='10px';const hangul=document.createElement('div');hangul.className='hangul';hangul.textContent=w.korean;const audio=document.createElement('button');audio.type='button';audio.className='section-audio';audio.textContent='🔊';audio.dataset.korean=w.korean;audio.setAttribute('aria-label','Koreyscha talaffuzni eshitish');audio.title='Talaffuzni eshitish';audio.addEventListener('click',()=>speakKorean(audio.dataset.korean));head.append(hangul,audio);a.append(head);for(const [c,v] of [['pron',w.pronunciation||'O‘qilishi kiritilmagan'],['translation',w.uzbek]]){const e=document.createElement('div');e.className=c;e.textContent=v;a.append(e)}const t=document.createElement('span');t.className='tag';t.textContent=w.context||'Mavzu ko‘rsatilmagan';a.append(t);return a}async function load(page=1){const q=input.value.trim(),u=new URL('/api/word-catalog',location.origin);u.searchParams.set('q',q);u.searchParams.set('page',page);try{const r=await fetch(u,{cache:'no-store'});if(!r.ok)throw Error();const d=await r.json();results.replaceChildren();if(d.words.length)d.words.forEach(w=>results.append(card(w)));else{const e=document.createElement('div');e.className='empty';e.textContent='So‘z topilmadi';results.append(e)}count.textContent=d.total+' ta so‘z · '+d.page+' / '+d.pages+'-sahifa';nav.replaceChildren();for(const [label,n,disabled] of [['← Oldingi',d.page-1,d.page<=1],['Keyingi →',d.page+1,d.page>=d.pages]]){const b=document.createElement('button');b.type='button';b.className='btn';b.textContent=label;b.disabled=disabled;b.dataset.page=n;nav.append(b);if(n===d.page){const x=document.createElement('span');x.className='muted small';x.textContent=d.page+' / '+d.pages;nav.insertBefore(x,b)}}const u2=new URL(location.href);if(q)u2.searchParams.set('q',q);else u2.searchParams.delete('q');if(d.page>1)u2.searchParams.set('page',d.page);else u2.searchParams.delete('page');history.replaceState(null,'',u2)}catch(e){count.textContent='Qidiruvni yuklab bo‘lmadi. Qayta urinib ko‘ring.'}}form.addEventListener('submit',e=>{e.preventDefault();load(1)});input.addEventListener('input',()=>{clearTimeout(timer);timer=setTimeout(()=>load(1),180)});nav.addEventListener('click',e=>{const b=e.target.closest('button[data-page]');if(b&&!b.disabled)load(Number(b.dataset.page))})})();</script>'''
 return page('So‘z qidirish',body,words=words,total=total,pages=pages,page_number=page_number,q=q)
app.add_url_rule('/', endpoint='vocabulary', view_func=word_catalog_home)

@app.route('/grammar')
def grammar_page():
    with db() as conn:
        lessons = conn.execute('SELECT * FROM grammar_lessons ORDER BY position,id').fetchall()
    body = r'''<main class="grammar-page"><section class="hero grammar-hero"><div class="eyebrow">HANGEUL • GRAMMATIKA</div><h1>📝 Koreys tili grammatikasi</h1><p class="muted">Qoidalarni, grammatik qoliplarni va misol gaplarni bosqichma-bosqich o‘rganing.</p>{% if session.get('admin_logged_in') %}<a class="btn primary" href="{{ url_for('grammar_admin') }}">✏️ Grammatikani boshqarish</a>{% endif %}</section>
{% if lessons %}<div class="grammar-list">{% for lesson in lessons %}<article class="panel grammar-card"><div class="grammar-number">{{ '%02d'|format(loop.index) }}</div><div class="grammar-content"><h2>{{ lesson.title }}</h2>{% if lesson.pattern %}<div class="grammar-pattern"><span>Qolip</span><strong>{{ lesson.pattern }}</strong></div>{% endif %}<div class="grammar-explanation">{{ lesson.explanation }}</div>{% if lesson.examples %}<section class="grammar-examples"><h3>Misollar</h3>{% for line in lesson.examples.split('\n') %}{% if line.strip() %}<p>{{ line }}</p>{% endif %}{% endfor %}</section>{% endif %}{% if lesson.notes %}<aside class="grammar-note"><strong>💡 Eslatma</strong><p>{{ lesson.notes }}</p></aside>{% endif %}</div></article>{% endfor %}</div>{% else %}<section class="panel grammar-empty"><div>📖</div><h2>Hali grammatika darslari yo‘q</h2><p class="muted">Admin panel orqali birinchi grammatik qoidani yozishingiz mumkin.</p>{% if session.get('admin_logged_in') %}<a class="btn primary" href="{{ url_for('grammar_admin') }}">＋ Birinchi darsni yozish</a>{% endif %}</section>{% endif %}</main>
<style>.grammar-page{max-width:1080px;margin:0 auto}.grammar-hero{margin-bottom:18px}.grammar-hero h1{font-size:clamp(27px,4vw,39px)}.grammar-list{display:grid;gap:14px}.grammar-card{display:grid;grid-template-columns:54px minmax(0,1fr);gap:17px;padding:22px!important;border-radius:19px!important}.grammar-number{width:48px;height:48px;display:grid;place-items:center;border-radius:15px;background:linear-gradient(135deg,#7445f4,#3697f6);color:white;font-weight:850;font-size:16px}.grammar-content{min-width:0}.grammar-content h2{margin:0 0 12px;font-size:22px}.grammar-pattern{display:flex;gap:12px;align-items:center;flex-wrap:wrap;padding:12px 14px;margin-bottom:13px;border:1px solid var(--border);border-radius:12px;background:var(--panel2)}.grammar-pattern span{font-size:11px;color:var(--muted);font-weight:750;text-transform:uppercase;letter-spacing:.06em}.grammar-pattern strong{font-size:18px;color:var(--accent)}.grammar-explanation{white-space:pre-wrap;line-height:1.8}.grammar-examples{margin-top:15px;padding:13px 15px;border-left:3px solid var(--purple);border-radius:0 12px 12px 0;background:var(--panel2)}.grammar-examples h3{margin:0 0 8px;font-size:13px}.grammar-examples p{margin:6px 0;white-space:pre-wrap}.grammar-note{margin-top:13px;padding:12px 14px;border-radius:12px;background:rgba(245,174,55,.10);border:1px solid rgba(245,174,55,.24)}.grammar-note p{margin:5px 0 0;white-space:pre-wrap}.grammar-empty{text-align:center;padding:36px!important}.grammar-empty>div{font-size:38px}.grammar-empty h2{margin:8px 0}.grammar-empty p{margin:0 0 15px}@media(max-width:600px){.grammar-card{grid-template-columns:1fr;padding:15px!important;gap:10px}.grammar-number{width:38px;height:38px;border-radius:12px}.grammar-content h2{font-size:19px}}</style>'''
    return page('Grammatika', body, lessons=lessons)

@app.route('/admin/grammar', methods=['GET','POST'])
@admin_required
def grammar_admin():
    if request.method == 'POST':
        action = request.form.get('action','')
        with db() as conn:
            if action in ('add','update'):
                title = request.form.get('title','').strip()
                pattern = request.form.get('pattern','').strip()
                explanation = request.form.get('explanation','').strip()
                examples = request.form.get('examples','').strip()
                notes = request.form.get('notes','').strip()
                if not title or not explanation:
                    flash('Dars nomi va tushuntirishini kiriting.')
                elif action == 'add':
                    position = conn.execute('SELECT COALESCE(MAX(position),0)+1 FROM grammar_lessons').fetchone()[0]
                    conn.execute('INSERT INTO grammar_lessons (title,pattern,explanation,examples,notes,position) VALUES (?,?,?,?,?,?)',(title,pattern,explanation,examples,notes,position))
                    flash('Grammatika darsi saqlandi.')
                else:
                    lesson_id = request.form.get('lesson_id',type=int)
                    conn.execute('UPDATE grammar_lessons SET title=?,pattern=?,explanation=?,examples=?,notes=?,updated_at=CURRENT_TIMESTAMP WHERE id=?',(title,pattern,explanation,examples,notes,lesson_id))
                    flash('Grammatika darsi yangilandi.')
            elif action == 'delete':
                conn.execute('DELETE FROM grammar_lessons WHERE id=?',(request.form.get('lesson_id',type=int),))
                flash('Grammatika darsi o‘chirildi.')
            elif action == 'move':
                lesson_id = request.form.get('lesson_id',type=int); direction = request.form.get('direction')
                rows = conn.execute('SELECT id,position FROM grammar_lessons ORDER BY position,id').fetchall()
                ids = [r['id'] for r in rows]
                if lesson_id in ids:
                    idx = ids.index(lesson_id); other = idx-1 if direction == 'up' else idx+1
                    if 0 <= other < len(ids):
                        a,b=rows[idx],rows[other]
                        conn.execute('UPDATE grammar_lessons SET position=? WHERE id=?',(b['position'],a['id']))
                        conn.execute('UPDATE grammar_lessons SET position=? WHERE id=?',(a['position'],b['id']))
        sync_msg = sync_drive_after_change()
        if sync_msg: flash(sync_msg)
        return redirect(url_for('grammar_admin'))
    edit_id = request.args.get('edit',type=int)
    with db() as conn:
        lessons = conn.execute('SELECT * FROM grammar_lessons ORDER BY position,id').fetchall()
        editing = conn.execute('SELECT * FROM grammar_lessons WHERE id=?',(edit_id,)).fetchone() if edit_id else None
    body = r'''<main class="grammar-admin"><section class="hero"><div class="eyebrow">ADMIN • GRAMMATIKA</div><h1>✏️ Grammatika darslarini yozish</h1><p class="muted">Har bir darsga nom, grammatik qolip, izoh, misollar va eslatma kiriting. Saqlangan darslar saytdagi Grammatika bo‘limida ko‘rinadi.</p><a class="btn" href="{{ url_for('grammar_page') }}">← Grammatika sahifasini ko‘rish</a></section><div class="grammar-admin-grid"><section class="panel grammar-editor"><h2>{{ '✏️ Darsni tahrirlash' if editing else '＋ Yangi grammatika darsi' }}</h2><form method="post"><input type="hidden" name="action" value="{{ 'update' if editing else 'add' }}">{% if editing %}<input type="hidden" name="lesson_id" value="{{ editing.id }}">{% endif %}<label for="grammar-title">Dars nomi *</label><input id="grammar-title" name="title" required value="{{ editing.title if editing else '' }}" placeholder="Masalan: 은/는 — mavzu qo‘shimchasi"><label for="grammar-pattern">Grammatik qolip</label><input id="grammar-pattern" name="pattern" value="{{ editing.pattern if editing else '' }}" placeholder="Masalan: Ot + 은/는 + ..."><label for="grammar-explanation">Tushuntirish *</label><textarea id="grammar-explanation" name="explanation" required rows="6" placeholder="Qoidani o‘zbek tilida tushuntiring...">{{ editing.explanation if editing else '' }}</textarea><label for="grammar-examples">Misollar (har birini yangi qatordan yozing)</label><textarea id="grammar-examples" name="examples" rows="5" placeholder="저는 학생이에요. — Men o‘quvchiman.&#10;이것은 책이에요. — Bu kitob.">{{ editing.examples if editing else '' }}</textarea><label for="grammar-notes">Eslatma (ixtiyoriy)</label><textarea id="grammar-notes" name="notes" rows="3" placeholder="Qachon ishlatiladi, istisnolar...">{{ editing.notes if editing else '' }}</textarea><div class="grammar-form-actions"><button class="btn primary" type="submit">💾 {{ 'O‘zgarishlarni saqlash' if editing else 'Darsni saqlash' }}</button>{% if editing %}<a class="btn" href="{{ url_for('grammar_admin') }}">Bekor qilish</a>{% endif %}</div></form></section><section class="panel grammar-admin-list"><h2>📚 Yozilgan darslar <span class="tag">{{ lessons|length }}</span></h2>{% if lessons %}<div class="grammar-admin-items">{% for lesson in lessons %}<article class="word grammar-admin-item"><div><h3>{{ lesson.title }}</h3>{% if lesson.pattern %}<p class="grammar-admin-pattern">{{ lesson.pattern }}</p>{% endif %}<p class="muted small">{{ lesson.explanation[:130] }}{% if lesson.explanation|length > 130 %}…{% endif %}</p></div><div class="grammar-admin-actions"><a class="btn" href="{{ url_for('grammar_admin',edit=lesson.id) }}">✏️ Tahrirlash</a><form method="post"><input type="hidden" name="action" value="move"><input type="hidden" name="lesson_id" value="{{ lesson.id }}"><input type="hidden" name="direction" value="up"><button class="btn" type="submit" aria-label="Yuqoriga ko‘tarish" title="Yuqoriga">↑</button></form><form method="post"><input type="hidden" name="action" value="move"><input type="hidden" name="lesson_id" value="{{ lesson.id }}"><input type="hidden" name="direction" value="down"><button class="btn" type="submit" aria-label="Pastga tushirish" title="Pastga">↓</button></form><form method="post" onsubmit="return confirm('Ushbu grammatik darsni o‘chirasizmi?')"><input type="hidden" name="action" value="delete"><input type="hidden" name="lesson_id" value="{{ lesson.id }}"><button class="btn danger" type="submit">🗑</button></form></div></article>{% endfor %}</div>{% else %}<p class="muted">Hali dars yozilmagan. Chapdagi formadan birinchi darsni yarating.</p>{% endif %}</section></div></main><style>.grammar-admin{max-width:1250px;margin:auto}.grammar-admin-grid{display:grid;grid-template-columns:minmax(300px,.9fr) minmax(0,1.1fr);gap:16px;align-items:start}.grammar-editor,.grammar-admin-list{padding:21px!important}.grammar-editor form{display:grid;gap:8px}.grammar-editor label{font-size:12px;font-weight:750;margin-top:7px}.grammar-editor input,.grammar-editor textarea{width:100%;min-width:0;padding:11px 12px;border:1px solid var(--border);border-radius:10px;background:var(--input);color:var(--text);resize:vertical}.grammar-editor input:focus,.grammar-editor textarea:focus{outline:2px solid rgba(112,64,240,.22);border-color:var(--purple)}.grammar-form-actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:8px}.grammar-admin-items{display:grid;gap:10px}.grammar-admin-item{padding:14px!important;gap:10px}.grammar-admin-item h3{margin:0;font-size:16px}.grammar-admin-pattern{margin:4px 0;font-weight:750;color:var(--accent)}.grammar-admin-item .muted{margin:5px 0}.grammar-admin-actions{display:flex;align-items:center;gap:6px;flex-wrap:wrap}.grammar-admin-actions form{margin:0}.grammar-admin-actions .btn{padding:7px 10px;font-size:12px}@media(max-width:900px){.grammar-admin-grid{grid-template-columns:1fr}}@media(max-width:600px){.grammar-editor,.grammar-admin-list{padding:14px!important}.grammar-admin-actions{gap:5px}}</style>'''
    return page('Grammatika boshqaruvi', body, lessons=lessons, editing=editing)

def _drive_file_id(value):
    # Accept a Google Drive share URL or a bare Drive file ID.
    import re
    value = (value or '').strip()
    if not value:
        return ''
    if 'drive.google.com' not in value and 'docs.google.com' not in value:
        # Permit a bare file ID only; arbitrary external URLs are rejected.
        return value if re.fullmatch(r'[A-Za-z0-9_-]{10,}', value) else ''
    match = re.search(r'/(?:file/d|document/d|spreadsheets/d|presentation/d)/([A-Za-z0-9_-]+)', value)
    if not match:
        match = re.search(r'[?&]id=([A-Za-z0-9_-]+)', value)
    return match.group(1) if match else ''

@app.route('/books')
def books_page():
    with db() as conn:
        books = [dict(row) for row in conn.execute('SELECT * FROM books ORDER BY position,id').fetchall()]
        dramas = [dict(row) for row in conn.execute('SELECT * FROM dramas ORDER BY id').fetchall()]
        drama_words = [dict(row) for row in conn.execute('SELECT * FROM drama_words ORDER BY id').fetchall()]
    for book in books:
        drive_id = book.get('drive_file_id') or ''
        book['preview_url'] = f"https://drive.google.com/file/d/{drive_id}/preview" if drive_id else ''
        book['download_url'] = f"https://drive.google.com/uc?export=download&id={drive_id}" if drive_id else ''
        book['telegram_url'] = book.get('telegram_url') or ''
    body = r'''<main class="books-page"><section class="hero books-hero"><div class="eyebrow">HANGEUL • DIGITAL LIBRARY</div><h1>📚 Koreys tili kitoblari</h1><p class="muted">Koreys tili kitoblarini muqovasi orqali toping va Telegramdagi asl postdan oching.</p>{% if session.get('admin_logged_in') %}<a class="btn primary" href="{{ url_for('books_admin') }}">⚙️ Kitoblarni boshqarish</a>{% endif %}</section>
<div class="books-toolbar"><div><strong>{{ books|length }} ta kitob</strong><p class="muted small">Kitoblar Telegramdagi asl post orqali ochiladi.</p></div><input id="book-search" type="search" placeholder="Kitob nomi, muallif yoki daraja..." aria-label="Kitob qidirish"></div>
{% if books %}<div class="books-grid" id="books-grid">{% for book in books %}<article class="panel book-card" data-search="{{ (book.title ~ ' ' ~ book.author ~ ' ' ~ book.level ~ ' ' ~ book.description)|lower }}"><div class="book-cover">{% if book.cover_url %}<img src="{{ book.cover_url }}" alt="{{ book.title }} muqovasi" loading="lazy">{% else %}<div class="book-cover-placeholder"><span>한글</span><small>HANGEUL LIBRARY</small><b>📖</b></div>{% endif %}<span class="book-level">{{ book.level }}</span></div><div class="book-info"><h2>{{ book.title }}</h2>{% if book.author %}<p class="book-author">{{ book.author }}</p>{% endif %}<p class="muted book-description">{{ book.description or 'Koreys tili bo‘yicha o‘quv materiali.' }}</p><div class="book-actions">{% if book.telegram_url %}<a class="btn primary" href="{{ book.telegram_url }}" target="_blank" rel="noopener noreferrer">✈️ Telegramda ochish</a>{% endif %}</div></div></article>{% endfor %}</div><p id="book-empty-search" class="empty" hidden>Bu qidiruvga mos kitob topilmadi.</p>{% else %}<section class="panel books-empty"><div>📚</div><h2>Hali kitob qo‘shilmagan</h2><p class="muted">Admin paneldan muqova rasmi va Telegram post havolasini qo‘shing.</p>{% if session.get('admin_logged_in') %}<a class="btn primary" href="{{ url_for('books_admin') }}">＋ Birinchi kitobni qo‘shish</a>{% endif %}</section>{% endif %}</main>
<style>.books-page{max-width:1200px;margin:auto}.books-hero{margin-bottom:18px}.books-toolbar{display:flex;justify-content:space-between;align-items:center;gap:14px;margin:0 0 16px;padding:15px 18px;border:1px solid var(--border);border-radius:15px;background:var(--panel)}.books-toolbar p{margin:2px 0 0}.books-toolbar input{width:min(360px,100%);padding:11px 13px;border:1px solid var(--border);border-radius:11px;background:var(--input);color:var(--text)}.books-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:16px}.book-card{overflow:hidden;padding:0!important;border-radius:17px!important}.book-cover{height:320px;position:relative;overflow:hidden;background:#fff;display:flex;align-items:center;justify-content:center;padding:10px;box-sizing:border-box}.book-cover img{display:block;width:auto;height:auto;max-width:100%;max-height:100%;object-fit:contain;object-position:center;background:#fff;flex:0 1 auto}.book-cover-placeholder{width:142px;height:174px;padding:18px 12px;display:flex;flex-direction:column;align-items:center;justify-content:space-between;border:1px solid rgba(255,255,255,.8);border-radius:8px;background:linear-gradient(150deg,#5c40d8,#398ce9);color:white;box-shadow:0 12px 28px #304a9b38;transform:rotate(-3deg)}.book-cover-placeholder span{font-size:31px;font-weight:850}.book-cover-placeholder small{font-size:8px;letter-spacing:1.5px}.book-cover-placeholder b{font-size:27px}.book-level{position:absolute;top:12px;left:12px;padding:5px 9px;border-radius:20px;background:rgba(255,255,255,.9);color:#3157b8;font-size:10px;font-weight:800}.book-info{padding:17px}.book-info h2{margin:0;font-size:18px;line-height:1.35}.book-author{margin:5px 0;color:var(--muted);font-size:12px}.book-description{font-size:12px;line-height:1.6;min-height:40px}.book-actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:14px}.book-actions .btn{font-size:11px;padding:9px 11px}.books-empty{text-align:center;padding:36px!important}.books-empty>div{font-size:40px}.books-empty h2{margin:8px 0}.books-reader{max-width:1200px;margin:auto}.books-reader-head{display:flex;justify-content:space-between;align-items:center;gap:12px;margin-bottom:14px}.books-reader-head h1{margin:0;font-size:clamp(21px,3vw,30px)}.books-reader-frame{height:min(76vh,900px);min-height:480px;overflow:hidden;border:1px solid var(--border);border-radius:16px;background:var(--panel);box-shadow:0 12px 32px var(--shadow)}.books-reader-frame iframe{width:100%;height:100%;border:0}.books-reader-help{font-size:12px;margin-top:12px}@media(max-width:1000px){.books-grid{grid-template-columns:repeat(2,minmax(0,1fr))}}@media(max-width:620px){.books-grid{grid-template-columns:1fr}.book-cover{height:320px}.books-toolbar{align-items:stretch;flex-direction:column}.books-toolbar input{width:100%}.books-reader-head{align-items:flex-start;flex-direction:column}.books-reader-frame{height:70vh;min-height:400px}}</style>
<script>(()=>{const input=document.getElementById('book-search'),cards=[...document.querySelectorAll('.book-card')],empty=document.getElementById('book-empty-search');if(!input)return;input.addEventListener('input',()=>{const q=input.value.trim().toLocaleLowerCase();let count=0;cards.forEach(card=>{const show=card.dataset.search.includes(q);card.hidden=!show;if(show)count++});if(empty)empty.hidden=count!==0})})();</script>'''
    return page('Koreys tili kitoblari', body, books=books)

@app.route('/books/<int:book_id>')
def book_reader(book_id):
    with db() as conn:
        book = conn.execute('SELECT * FROM books WHERE id=?', (book_id,)).fetchone()
    if not book:
        abort(404)
    book = dict(book)
    if not (book.get('drive_file_id') or '').strip():
        if book.get('telegram_url'):
            return redirect(book['telegram_url'])
        abort(404)
    preview_url = f"https://drive.google.com/file/d/{book['drive_file_id']}/preview"
    download_url = f"https://drive.google.com/uc?export=download&id={book['drive_file_id']}"
    body = r'''<main class="books-reader"><div class="books-reader-head"><div><p class="eyebrow">HANGEUL • KITOBXONA</p><h1>📖 {{ book.title }}</h1><p class="muted small">{{ book.author }}{% if book.author and book.level %} · {% endif %}{{ book.level }}</p></div><div class="book-actions"><a class="btn" href="{{ url_for('books_page') }}">← Kitoblarga qaytish</a><a class="btn primary" href="{{ download_url }}" target="_blank" rel="noopener">⬇️ Yuklab olish</a></div></div><div class="books-reader-frame"><iframe src="{{ preview_url }}" title="{{ book.title }} PDF kitobi" allow="autoplay" loading="lazy"></iframe></div><p class="muted books-reader-help">PDF ko‘rinmasa, Drive fayli uchun “Anyone with the link / Havolaga ega bo‘lgan har kim” ko‘rish ruxsatini yoqing. <a href="{{ download_url }}" target="_blank" rel="noopener"><u>Yuklab olish havolasi</u></a></p></main>'''
    return page(book['title'], body, book=book, preview_url=preview_url, download_url=download_url)

@app.route('/admin/books', methods=['GET','POST'])
@admin_required
def books_admin():
    if request.method == 'POST':
        action = request.form.get('action', 'add')
        with db() as conn:
            if action in ('add','update'):
                title = request.form.get('title','').strip()
                author = request.form.get('author','').strip()
                level = request.form.get('level','Beginner').strip() or 'Beginner'
                description = request.form.get('description','').strip()
                cover_url = request.form.get('cover_url','').strip()
                drive_id = ''
                telegram_url = request.form.get('telegram_url','').strip()
                if telegram_url and not (telegram_url.startswith('https://t.me/') or telegram_url.startswith('https://telegram.me/')):
                    telegram_url = ''
                if not title or not telegram_url:
                    flash('Kitob nomi va Telegram post havolasini kiriting.')
                elif action == 'add':
                    position = conn.execute('SELECT COALESCE(MAX(position),0)+1 FROM books').fetchone()[0]
                    conn.execute('INSERT INTO books (title,author,level,description,drive_file_id,cover_url,position,telegram_url) VALUES (?,?,?,?,?,?,?,?)',(title,author,level,description,drive_id,cover_url,position,telegram_url))
                    flash('Kitob qo‘shildi.')
                else:
                    conn.execute('UPDATE books SET title=?,author=?,level=?,description=?,drive_file_id=?,cover_url=?,telegram_url=? WHERE id=?',(title,author,level,description,drive_id,cover_url,telegram_url,request.form.get('book_id',type=int)))
                    flash('Kitob ma’lumotlari yangilandi.')
            elif action == 'delete':
                conn.execute('DELETE FROM books WHERE id=?',(request.form.get('book_id',type=int),))
                flash('Kitob ro‘yxatdan o‘chirildi.')
        sync_msg = sync_drive_after_change()
        if sync_msg: flash(sync_msg)
        return redirect(url_for('books_admin'))
    edit_id = request.args.get('edit',type=int)
    with db() as conn:
        books = [dict(row) for row in conn.execute('SELECT * FROM books ORDER BY position,id').fetchall()]
        dramas = [dict(row) for row in conn.execute('SELECT * FROM dramas ORDER BY id').fetchall()]
        drama_words = [dict(row) for row in conn.execute('SELECT * FROM drama_words ORDER BY id').fetchall()]
        editing = conn.execute('SELECT * FROM books WHERE id=?',(edit_id,)).fetchone() if edit_id else None
    if editing:
        editing = dict(editing)
        editing['telegram_url'] = editing.get('telegram_url') or ''
    body = r'''<main class="books-admin"><section class="hero"><div class="eyebrow">ADMIN • DIGITAL LIBRARY</div><h1>📚 Kitoblar boshqaruvi</h1><p class="muted">Kitob nomi, muqova rasmi URL’i va Telegram post havolasini kiriting.</p><a class="btn" href="{{ url_for('books_page') }}">← Kitoblar sahifasi</a></section><div class="books-admin-grid"><section class="panel books-admin-form"><h2>{{ '✏️ Kitobni tahrirlash' if editing else '＋ Yangi kitob qo‘shish' }}</h2><form method="post"><input type="hidden" name="action" value="{{ 'update' if editing else 'add' }}">{% if editing %}<input type="hidden" name="book_id" value="{{ editing.id }}">{% endif %}<label>Kitob nomi *</label><input name="title" required value="{{ editing.title if editing else '' }}" placeholder="Korean Grammar in Use — Beginner"><label>Muallif</label><input name="author" value="{{ editing.author if editing else '' }}" placeholder="Muallif nomi"><label>Daraja</label><select name="level">{% for level in ['Beginner','Elementary','Intermediate','Advanced','TOPIK I','TOPIK II','Barcha darajalar'] %}<option value="{{ level }}" {% if editing and editing.level==level %}selected{% endif %}>{{ level }}</option>{% endfor %}</select><label>Qisqacha izoh</label><textarea name="description" rows="3" placeholder="Kitob haqida...">{{ editing.description if editing else '' }}</textarea><label>Telegram post havolasi *</label><input type="url" name="telegram_url" value="{{ editing.telegram_url if editing else '' }}" placeholder="https://t.me/kanal/123"><p class="muted small">Kitob Telegram post havolasi orqali ochiladi. PDF faylni Google Drive’ga yuklash talab qilinmaydi.</p><label>Muqova rasmi URL (ixtiyoriy)</label><input type="url" name="cover_url" value="{{ editing.cover_url if editing else '' }}" placeholder="https://.../cover.jpg"><div class="book-actions"><button class="btn primary" type="submit">💾 {{ 'Saqlash' if editing else 'Kitobni qo‘shish' }}</button>{% if editing %}<a class="btn" href="{{ url_for('books_admin') }}">Bekor qilish</a>{% endif %}</div></form></section><section class="panel books-admin-list"><h2>📖 Mavjud kitoblar <span class="tag">{{ books|length }}</span></h2>{% if books %}<div class="books-admin-items">{% for book in books %}<article class="word books-admin-item"><div><h3>{{ book.title }}</h3><p class="muted small">{{ book.author or 'Muallif ko‘rsatilmagan' }} · {{ book.level }}</p></div><div class="book-actions">{% if book.telegram_url %}<a class="btn" href="{{ book.telegram_url }}" target="_blank" rel="noopener noreferrer">Telegramda ochish</a>{% endif %}<a class="btn" href="{{ url_for('books_admin',edit=book.id) }}">✏️ Tahrirlash</a><form method="post" onsubmit="return confirm('Kitobni ro‘yxatdan o‘chirasizmi?')"><input type="hidden" name="action" value="delete"><input type="hidden" name="book_id" value="{{ book.id }}"><button class="btn danger" type="submit">🗑</button></form></div></article>{% endfor %}</div>{% else %}<p class="muted">Hali kitob qo‘shilmagan.</p>{% endif %}</section></div></main><style>.books-admin{max-width:1200px;margin:auto}.books-admin-grid{display:grid;grid-template-columns:minmax(280px,.85fr) minmax(0,1.15fr);gap:16px;align-items:start}.books-admin-form,.books-admin-list{padding:20px!important}.books-admin-form form{display:grid;gap:8px}.books-admin-form label{font-size:12px;font-weight:750;margin-top:6px}.books-admin-form input,.books-admin-form textarea,.books-admin-form select{width:100%;min-width:0;padding:11px 12px;border:1px solid var(--border);border-radius:10px;background:var(--input);color:var(--text)}.books-admin-items{display:grid;gap:10px}.books-admin-item{padding:13px!important;display:grid;gap:10px}.books-admin-item h3{margin:0;font-size:15px}.books-admin-item p{margin:4px 0}.books-admin-item form{margin:0}.books-admin-item .btn{font-size:11px;padding:8px 10px}@media(max-width:900px){.books-admin-grid{grid-template-columns:1fr}}@media(max-width:600px){.books-admin-form,.books-admin-list{padding:14px!important}}</style>'''
    return page('Kitoblar boshqaruvi', body, books=books, editing=editing)

# --- DRAMALAR: subtitle parsing, vocabulary extraction, translation, and study rounds ---
def _parse_subtitles(raw_text):
    # Extract unique Korean tokens from subtitle text.
    import re, html
    text = html.unescape(raw_text.replace('\ufeff', ' '))
    tokens = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.upper().startswith('WEBVTT') or '-->' in line or re.fullmatch(r'\d+', line):
            continue
        line = re.sub(r'<[^>]+>', ' ', line)
        line = re.sub(r'\([^)]*\)|\[[^]]*\]', ' ', line)
        for token in re.findall(r'[가-힣]+', line):
            if len(token) >= 1 and token not in tokens:
                tokens.append(token)
    return tokens

def _translate_korean_to_uzbek(words):
    # Use installed Deep Translator when available.
    translated = {}
    try:
        from deep_translator import GoogleTranslator
        translator = GoogleTranslator(source='ko', target='uz')
        for word in words:
            try:
                translated[word] = (translator.translate(word) or '').strip()
            except Exception:
                translated[word] = ''
    except Exception:
        # First use any translations already in the site's vocabulary database.
        with db() as conn:
            for word in words:
                row = conn.execute('SELECT uzbek FROM words WHERE korean=? LIMIT 1', (word,)).fetchone()
                translated[word] = row['uzbek'] if row else ''
    return translated

@app.route('/dramas')
def dramas_page():
    with db() as conn:
        dramas = [dict(r) for r in conn.execute('SELECT d.*, (SELECT COUNT(*) FROM drama_words w WHERE w.drama_id=d.id) word_count FROM dramas d ORDER BY d.id DESC').fetchall()]
    body = r'''<main class="drama-page"><section class="hero"><div class="eyebrow">HANGEUL • KOREAN FROM DRAMAS</div><h1>🎬 Dramalar</h1><p class="muted">Dramalarni saqlang, koreyscha so‘zlarni ko‘ring va kunlik mashqlar orqali yodlang.</p>{% if session.get('admin_logged_in') %}<a class="btn primary" href="{{ url_for('dramas_admin') }}">＋ Drama qo‘shish / boshqarish</a>{% endif %}</section>{% if dramas %}<div class="drama-grid">{% for d in dramas %}<article class="panel drama-card">{% if d.cover_url %}<img class="drama-cover" src="{{ d.cover_url }}" alt="{{ d.title }}">{% else %}<div class="drama-cover drama-placeholder">🎬</div>{% endif %}<div class="drama-info"><h2>{{ d.title }}</h2><p class="muted small">{{ d.word_count }} ta ajratilgan so‘z</p><div class="book-actions">{% if d.drama_url %}<a class="btn" target="_blank" rel="noopener" href="{{ d.drama_url }}">▶ Dramani ochish</a>{% endif %}<a class="btn primary" href="{{ url_for('drama_study',drama_id=d.id) }}">📚 So‘zlarni yodlash</a></div></div></article>{% endfor %}</div>{% else %}<section class="panel books-empty"><div>🎬</div><h2>Hali drama qo‘shilmagan</h2><p class="muted">Admin panelda drama nomi, havolasi va muqova rasmini kiriting.</p></section>{% endif %}</main><style>.drama-page{max-width:1150px;margin:auto}.drama-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:16px}.drama-card{overflow:hidden;padding:0!important}.drama-cover{display:block;width:100%;height:230px;object-fit:contain;background:var(--panel2)}.drama-placeholder{display:grid;place-items:center;font-size:64px}.drama-info{padding:16px}.drama-info h2{margin:0}.drama-info .book-actions{margin-top:12px}</style>'''
    return page('Dramalar', body, dramas=dramas)

@app.route('/admin/dramas', methods=['GET','POST'])
@admin_required
def dramas_admin():
    message = ''
    if request.method == 'POST':
        title = request.form.get('title','').strip()
        drama_url = request.form.get('drama_url','').strip()
        cover_url = request.form.get('cover_url','').strip()
        upload = request.files.get('subtitle')
        if not title:
            flash('Drama nomini kiriting.')
        elif not upload or not upload.filename:
            # Drama details can be saved without uploading a subtitle file.
            with db() as conn:
                conn.execute('INSERT INTO dramas(title,drama_url,cover_url,subtitle_filename) VALUES(?,?,?,?)', (title,drama_url,cover_url,''))
            sync_msg = sync_drive_after_change()
            flash('Drama saqlandi. Hozircha subtitr yuklanmagani uchun so‘zlar ajratilmadi.')
            if sync_msg: flash(sync_msg)
            return redirect(url_for('dramas_admin'))
        elif not upload.filename.lower().endswith(('.srt','.vtt','.txt')):
            flash('Faqat .srt, .vtt yoki .txt subtitr fayli qabul qilinadi.')
        else:
            try:
                raw = upload.read().decode('utf-8-sig', errors='replace')
                words = _parse_subtitles(raw)
                if not words:
                    flash('Subtitr ichidan koreyscha so‘z topilmadi. Faylni tekshiring.')
                else:
                    translations = _translate_korean_to_uzbek(words)
                    with db() as conn:
                        conn.execute('INSERT INTO dramas(title,drama_url,cover_url,subtitle_filename) VALUES(?,?,?,?)', (title,drama_url,cover_url,upload.filename))
                        drama_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
                        # Put words into the existing topic-day/round learning system: 25 words per day (12 + 13).
                        folder_name = 'Drama: ' + title
                        folder = conn.execute('SELECT id FROM topic_folders WHERE name=? LIMIT 1',(folder_name,)).fetchone()
                        if folder: folder_id = folder['id']
                        else:
                            conn.execute('INSERT INTO topic_folders(name,description) VALUES(?,?)',(folder_name,'Drama subtitridan ajratilgan so‘zlar'))
                            folder_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
                        conn.execute('UPDATE dramas SET folder_id=? WHERE id=?',(folder_id,drama_id))
                        for i, word in enumerate(words):
                            uzbek = translations.get(word,'') or ''
                            pronunciation = auto_pronunciation(word)
                            day_number = i // 25 + 1
                            day = conn.execute('SELECT id FROM topic_days WHERE folder_id=? AND day_number=?',(folder_id,day_number)).fetchone()
                            if day: day_id = day['id']
                            else:
                                conn.execute('INSERT INTO topic_days(folder_id,day_number,name) VALUES(?,?,?)',(folder_id,day_number,f'Kun {day_number}'))
                                day_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
                            round_number = 1 if i % 25 < 12 else 2
                            position = (i % 25) + 1 if round_number == 1 else (i % 25) - 11
                            example = next((ln.strip() for ln in raw.splitlines() if word in ln and not '-->' in ln and not ln.strip().isdigit()), '')[:500]
                            conn.execute('INSERT OR IGNORE INTO topic_words(day_id,round_number,position,korean,uzbek,pronunciation,example) VALUES(?,?,?,?,?,?,?)',(day_id,round_number,position,word,uzbek,pronunciation,example))
                            conn.execute('INSERT OR IGNORE INTO drama_words(drama_id,korean,uzbek,pronunciation,example,day_id,round_number,position,translation_status) VALUES(?,?,?,?,?,?,?,?,?)',(drama_id,word,uzbek,pronunciation,example,day_id,round_number,position,'translated' if uzbek else 'needs_review'))
                    sync_msg = sync_drive_after_change()
                    flash(f'Drama qo‘shildi: {len(words)} ta noyob koreyscha so‘z ajratildi. {sum(1 for w in words if translations.get(w))} tasiga tarjima topildi.')
                    if sync_msg: flash(sync_msg)
                    return redirect(url_for('dramas_admin'))
            except Exception as exc:
                flash(f'Drama tahlilida xatolik: {exc}')
        return redirect(url_for('dramas_admin'))
    with db() as conn:
        dramas = [dict(r) for r in conn.execute('SELECT d.*, (SELECT COUNT(*) FROM drama_words w WHERE w.drama_id=d.id) word_count FROM dramas d ORDER BY d.id DESC').fetchall()]
    body = r'''<main class="drama-admin"><section class="hero"><div class="eyebrow">ADMIN • DRAMA VOCABULARY</div><h1>🎬 Drama qo‘shish</h1><p class="muted">Drama nomi, video havolasi va muqova rasmini kiriting. Drama ma’lumotlari Drive backup’iga sinxronlanadi.</p><a class="btn" href="{{ url_for('dramas_page') }}">← Dramalar bo‘limi</a></section><div class="drama-admin-grid"><section class="panel drama-form"><h2>＋ Yangi drama</h2><form method="post"><label>Drama nomi *</label><input name="title" required placeholder="Masalan: 사랑의 불시착"><label>Drama video havolasi (ixtiyoriy)</label><input type="url" name="drama_url" placeholder="https://..."><label>Muqova rasmi URL (ixtiyoriy)</label><input type="url" name="cover_url" placeholder="https://...jpg"><button class="btn primary" type="submit">💾 Saqlash</button></form></section><section class="panel drama-list"><h2>Mavjud dramalar ({{ dramas|length }})</h2>{% for d in dramas %}<article class="word drama-row"><div><strong>{{ d.title }}</strong><p class="muted small">{{ d.word_count }} ta so‘z{% if d.subtitle_filename %} · {{ d.subtitle_filename }}{% endif %}</p></div><a class="btn primary" href="{{ url_for('drama_study',drama_id=d.id) }}">So‘zlarni ko‘rish</a></article>{% else %}<p class="muted">Hali drama yo‘q.</p>{% endfor %}</section></div></main><style>.drama-admin{max-width:1100px;margin:auto}.drama-admin-grid{display:grid;grid-template-columns:minmax(280px,.8fr) minmax(0,1.2fr);gap:16px;align-items:start}.drama-form,.drama-list{padding:20px!important}.drama-form form{display:grid;gap:9px}.drama-form label{font-size:12px;font-weight:750;margin-top:5px}.drama-form input{width:100%;padding:11px;border:1px solid var(--border);border-radius:10px;background:var(--input);color:var(--text)}.drama-row{display:flex;justify-content:space-between;align-items:center;gap:12px;padding:12px!important;margin-bottom:8px}.drama-row p{margin:4px 0}@media(max-width:800px){.drama-admin-grid{grid-template-columns:1fr}}</style>'''
    return page('Drama boshqaruvi', body, dramas=dramas)

@app.route('/dramas/<int:drama_id>')
def drama_study(drama_id):
    with db() as conn:
        drama = conn.execute('SELECT * FROM dramas WHERE id=?',(drama_id,)).fetchone()
        if not drama: abort(404)
        words = [dict(r) for r in conn.execute('SELECT * FROM drama_words WHERE drama_id=? ORDER BY id',(drama_id,)).fetchall()]
        missing = sum(1 for w in words if not w['uzbek'])
    body = r'''<main class="drama-study"><section class="hero"><div class="eyebrow">DRAMA VOCABULARY</div><h1>📚 {{ drama.title }}</h1><p class="muted">{{ words|length }} ta noyob so‘z · {{ missing }} ta tarjima tekshirish/kiritishni kutmoqda.</p><a class="btn" href="{{ url_for('dramas_page') }}">← Dramalar</a>{% if drama.folder_id %}<a class="btn primary" href="{{ url_for('topic_folder_view',folder_id=drama.folder_id) }}">Mashqlar orqali yodlash</a>{% endif %}</section><section class="panel drama-words"><div class="drama-word-head"><h2>Ajratilgan so‘zlar</h2><input id="drama-word-search" placeholder="So‘z yoki tarjima qidirish..."></div><div class="drama-word-grid" id="drama-word-grid">{% for w in words %}<article class="drama-word" data-search="{{ (w.korean ~ ' ' ~ w.uzbek)|lower }}"><strong>{{ w.korean }}</strong><span>{{ w.uzbek or 'Tarjima kiritilmagan — tekshirish kerak' }}</span><small>{{ w.pronunciation }}</small>{% if w.example %}<p>{{ w.example }}</p>{% endif %}</article>{% endfor %}</div></section></main><style>.drama-study{max-width:1150px;margin:auto}.drama-words{padding:18px!important}.drama-word-head{display:flex;justify-content:space-between;align-items:center;gap:12px}.drama-word-head h2{margin:0}.drama-word-head input{padding:10px;border:1px solid var(--border);border-radius:10px;background:var(--input);color:var(--text)}.drama-word-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:10px;margin-top:15px}.drama-word{display:flex;flex-direction:column;gap:5px;padding:13px;border:1px solid var(--border);border-radius:12px;background:var(--panel2);overflow-wrap:anywhere}.drama-word strong{font-size:22px;font-family:'Noto Sans KR',sans-serif}.drama-word span{font-weight:650}.drama-word small{color:var(--muted)}.drama-word p{margin:0;font-size:12px;color:var(--muted)}@media(max-width:600px){.drama-word-head{align-items:stretch;flex-direction:column}}</style><script>document.getElementById('drama-word-search').addEventListener('input',e=>{const q=e.target.value.toLowerCase();document.querySelectorAll('.drama-word[data-search]').forEach(x=>x.hidden=!x.dataset.search.includes(q))});</script>'''
    return page('Drama so‘zlari', body, drama=dict(drama), words=words, missing=missing)

@app.route('/topics')
def topics():
    session.pop('active_topic_day_id', None)
    session.pop('active_topic_round', None)
    session.pop('active_topic_folder_id', None)
    with db() as conn:
        folders = conn.execute('''SELECT f.*, COUNT(DISTINCT d.id) AS day_count,
            COUNT(w.id) AS word_count
            FROM topic_folders f LEFT JOIN topic_days d ON d.folder_id=f.id
            LEFT JOIN topic_words w ON w.day_id=d.id
            GROUP BY f.id ORDER BY f.id''').fetchall()
        sections = conn.execute('''SELECT s.*, COUNT(w.id) AS word_count FROM sections s
            LEFT JOIN words w ON w.category=s.name GROUP BY s.id ORDER BY s.name COLLATE NOCASE''').fetchall()
    body = '''<section class="hero"><div class="eyebrow">HANGEUL • MAVZULAR</div><h1>📚 Mening papkalarim</h1><p class="muted">Har bir papkada istalgancha kun, har bir kunda 12 + 13 so‘zli ikki round.</p>{% if session.get('admin_logged_in') %}<a class="btn primary" href="{{ url_for('topic_manager') }}">＋ Papkalarni boshqarish</a>{% endif %}</section>
    {% if folders %}<div class="cards">{% for f in folders %}<a class="section-card" href="{{ url_for('topic_folder_view', folder_id=f.id) }}"><div class="section-icon">📁</div><h2>{{ f.name }}</h2><p class="muted small">{{ f.description or 'Shaxsiy so‘zlar to‘plami' }}</p><span class="tag">{{ f.day_count }} ta DAY</span> <span class="tag">{{ f.word_count }} ta so‘z</span></a>{% endfor %}</div>{% else %}<div class="empty">Hali papka yaratilmagan.{% if session.get('admin_logged_in') %} <a href="{{ url_for('topic_manager') }}">Birinchi papkani yarating.</a>{% else %} Papka yaratish uchun Admin orqali kiring.{% endif %}</div>{% endif %}
    {% if sections %}<section class="panel" style="margin-top:22px"><h2>Wongoji test</h2><p class="muted small">Admin panelda kiritilgan so‘zlar asosida koreyscha yozuvni wongoji kataklarida mashq qiling.</p><div class="cards">{% for s in sections %}<a class="section-card" href="{{ url_for('section_words', section_id=s.id) }}"><div class="section-icon">📖</div><h2>{{ s.name }}</h2><span class="tag">{{ s.word_count }} ta so‘z</span></a>{% endfor %}</div></section>{% endif %}'''
    return page('Mavzular', body, folders=folders, sections=sections)


@app.route('/topic-folder/<int:folder_id>')
def topic_folder_view(folder_id):
    session.pop('active_topic_day_id', None)
    session.pop('active_topic_round', None)
    session.pop('active_topic_folder_id', None)
    with db() as conn:
        folder = conn.execute('SELECT * FROM topic_folders WHERE id=?', (folder_id,)).fetchone()
        if not folder: abort(404)
        days = conn.execute('''SELECT d.*, COUNT(w.id) AS word_count,
            SUM(CASE WHEN w.round_number=1 THEN 1 ELSE 0 END) AS round1_count,
            SUM(CASE WHEN w.round_number=2 THEN 1 ELSE 0 END) AS round2_count,
            CASE WHEN c.id IS NULL THEN 0 ELSE 1 END AS is_completed
            FROM topic_days d LEFT JOIN topic_words w ON w.day_id=d.id
            LEFT JOIN day_completions c ON c.day_id=d.id
            WHERE d.folder_id=? GROUP BY d.id ORDER BY d.day_number''', (folder_id,)).fetchall()
        total_xp = conn.execute('SELECT COALESCE((SELECT SUM(c.xp_awarded) FROM day_completions c JOIN topic_days d ON d.id=c.day_id WHERE d.folder_id=?),0) + COALESCE((SELECT SUM(rc.xp_awarded) FROM round_completions rc JOIN topic_days d2 ON d2.id=rc.day_id WHERE d2.folder_id=?),0)', (folder_id, folder_id)).fetchone()[0]
    body = '''<section class="hero"><div class="eyebrow"><a href="{{ url_for('topics') }}">MAVZULAR</a> / PAPKA</div><h1>📁 {{ folder.name }}</h1><p class="muted">{{ folder.description or 'Kunlik so‘zlar to‘plami' }} · Jami {{ total_xp }} XP</p>{% if session.get('admin_logged_in') %}<a class="btn" href="{{ url_for('topic_manager', folder_id=folder.id) }}">✎ Papkani boshqarish</a>{% endif %}</section>
    {% if days %}<div class="cards">{% for d in days %}<article class="section-card"><div class="section-icon">📅</div><h2>{{ d.name }}</h2><p class="muted small">{{ d.word_count }} / 25 ta so‘z kiritilgan</p><div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:12px"><a class="btn primary" href="{{ url_for('topic_round_view', day_id=d.id, round_number=1) }}">Round 1 · 12 ta</a><a class="btn primary" href="{{ url_for('topic_round_view', day_id=d.id, round_number=2) }}">Round 2 · 13 ta</a></div><p class="muted small" style="margin-top:12px">Rounddagi 4 ta mashqni tugating — +10 XP (har bir round uchun faqat bir marta).</p></article>{% endfor %}</div>{% else %}<div class="empty">Bu papkada hali DAY yo‘q.{% if session.get('admin_logged_in') %} <a href="{{ url_for('topic_manager', folder_id=folder.id) }}">DAY qo‘shish</a>{% endif %}</div>{% endif %}'''
    return page(folder['name'], body, folder=folder, days=days, total_xp=total_xp)


@app.route('/topic-day/<int:day_id>/round/<int:round_number>')
def topic_round_view(day_id, round_number):
    if round_number not in (1, 2): abort(404)
    # Round tanlanganda bevosita shu roundning Flashcard mashqi ochiladi.
    with db() as conn:
        day = conn.execute('SELECT id, folder_id FROM topic_days WHERE id=?', (day_id,)).fetchone()
    if not day:
        abort(404)
    session['active_topic_day_id'] = day_id
    session['active_topic_round'] = round_number
    session['active_topic_folder_id'] = day['folder_id']
    for key in ('quiz_order', 'quiz_index', 'quiz_history'):
        session.pop(key, None)
    return redirect(url_for('marathon'))
    with db() as conn:
        day = conn.execute('''SELECT d.*, f.id AS folder_id, f.name AS folder_name FROM topic_days d
            JOIN topic_folders f ON f.id=d.folder_id WHERE d.id=?''', (day_id,)).fetchone()
        if not day: abort(404)
        words = conn.execute('SELECT * FROM topic_words WHERE day_id=? AND round_number=? ORDER BY position', (day_id, round_number)).fetchall()
    session['active_topic_day_id'] = day_id
    session['active_topic_round'] = round_number
    session['active_topic_folder_id'] = day['folder_id']
    for key in ('quiz_order', 'quiz_index', 'quiz_history'):
        session.pop(key, None)
    limit = 12 if round_number == 1 else 13
    body = '''<section class="hero"><div class="eyebrow"><a href="{{ url_for('topics') }}">MAVZULAR</a> / <a href="{{ url_for('topic_folder_view', folder_id=day.folder_id) }}">{{ day.folder_name }}</a> / {{ day.name }}</div><h1>{{ day.name }} · Round {{ round_number }}</h1><p class="muted">{{ words|length }} / {{ limit }} ta so‘z</p></section>
    {% if words %}<div class="cards">{% for w in words %}<article class="word"><div class="hangul">{{ w.korean }}</div><div class="pron">{{ w.pronunciation or '' }}</div><div class="translation">{{ w.uzbek }}</div>{% if w.example %}<div class="example">{{ w.example }}</div>{% endif %}<button class="section-audio" type="button" onclick="speakKorean(this.dataset.korean)" data-korean="{{ w.korean }}" aria-label="Koreyscha talaffuzni eshitish" title="Talaffuzni eshitish">🔊</button></article>{% endfor %}</div>{% else %}<div class="empty">Bu roundga hali so‘z kiritilmagan.</div>{% endif %}
    <div style="margin-top:18px"><a class="btn" href="{{ url_for('topic_folder_view', folder_id=day.folder_id) }}">← Kunlarga qaytish</a>{% if session.get('admin_logged_in') %} <a class="btn primary" href="{{ url_for('topic_manager', folder_id=day.folder_id, day_id=day.id, round_number=round_number) }}">＋ So‘z qo‘shish</a>{% endif %}</div>'''
    return page(f"{day['name']} · Round {round_number}", body, day=day, words=words, round_number=round_number, limit=limit)


ROUND_ACTIVITIES = ('flashcard', 'matching', 'viktorina', 'writing')


def mark_round_activity(day_id, round_number, activity):
    # Save the activity once, then award XP only after all four activities are complete.
    with db() as conn:
        exists = conn.execute('SELECT id FROM topic_days WHERE id=?', (day_id,)).fetchone()
        if not exists or round_number not in (1, 2) or activity not in ROUND_ACTIVITIES:
            return {'ok': False, 'xp_awarded': False, 'completed': 0, 'round_completed': False}
        conn.execute('INSERT OR IGNORE INTO round_activity_completions (day_id, round_number, activity) VALUES (?,?,?)',
                     (day_id, round_number, activity))
        completed = conn.execute('SELECT COUNT(*) FROM round_activity_completions WHERE day_id=? AND round_number=? AND activity IN (?,?,?,?)',
                                 (day_id, round_number, *ROUND_ACTIVITIES)).fetchone()[0]
        xp_awarded = False
        if completed == 4:
            cur = conn.execute('INSERT OR IGNORE INTO round_completions (day_id, round_number, xp_awarded) VALUES (?,?,10)',
                               (day_id, round_number))
            xp_awarded = cur.rowcount == 1
        return {'ok': True, 'xp_awarded': xp_awarded, 'completed': completed, 'round_completed': completed == 4}


@app.route('/round-activity/<activity>/complete', methods=['POST'])
def complete_round_activity(activity):
    day_id = session.get('active_topic_day_id')
    round_number = session.get('active_topic_round')
    if not day_id or not round_number or activity not in ROUND_ACTIVITIES:
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return Response(json.dumps({'ok': False, 'message': 'Faol round tanlanmagan.'}), status=400, mimetype='application/json')
        flash('Avval Mavzular ichidan roundni tanlang.')
        return redirect(url_for('topics'))
    result = mark_round_activity(day_id, round_number, activity)
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return Response(json.dumps(result), mimetype='application/json')
    if result.get('xp_awarded'):
        flash(f'Barakalla! Round {round_number}dagi 4 ta mashq tugadi: +10 XP 🎉')
    else:
        flash(f'Mashq yakunlandi. Round bo‘yicha bajarilgan mashqlar: {result.get("completed", 0)}/4.')
    return redirect(url_for('topic_round_view', day_id=day_id, round_number=round_number))


@app.route('/topic-day/<int:day_id>/complete', methods=['POST'])
def complete_day(day_id):
    with db() as conn:
        day = conn.execute('SELECT id, folder_id, name FROM topic_days WHERE id=?', (day_id,)).fetchone()
        if not day:
            abort(404)
        flash('XP olish uchun Round ichidagi Flashcard, Aqli moslash, Viktorina va Yozib o‘rganish mashqlarining barchasini tugating.')
    message = sync_drive_after_change()
    if message:
        flash(message)
    return redirect(url_for('topic_folder_view', folder_id=day['folder_id']))


@app.route('/admin/topics', methods=['GET', 'POST'])
@admin_required
def topic_manager():
    if request.method == 'POST':
        action = request.form.get('action', '')
        with db() as conn:
            if action == 'create_folder':
                name = request.form.get('name', '').strip()
                description = request.form.get('description', '').strip()
                if not name:
                    flash('Papka nomini kiriting.')
                else:
                    cur = conn.execute('INSERT INTO topic_folders (name, description) VALUES (?,?)', (name, description))
                    flash('Papka yaratildi.')
                    folder_id = cur.lastrowid
                    conn.execute('INSERT INTO topic_days (folder_id, day_number, name) VALUES (?,?,?)', (folder_id, 1, 'DAY 1'))
            elif action == 'rename_folder':
                folder_id = request.form.get('folder_id', type=int)
                name = request.form.get('name', '').strip()
                description = request.form.get('description', '').strip()
                if folder_id and name:
                    conn.execute('UPDATE topic_folders SET name=?, description=? WHERE id=?', (name, description, folder_id))
                    flash('Papka yangilandi.')
            elif action == 'delete_folder':
                folder_id = request.form.get('folder_id', type=int)
                conn.execute('DELETE FROM day_completions WHERE day_id IN (SELECT id FROM topic_days WHERE folder_id=?)', (folder_id,))
                conn.execute('DELETE FROM topic_words WHERE day_id IN (SELECT id FROM topic_days WHERE folder_id=?)', (folder_id,))
                conn.execute('DELETE FROM topic_days WHERE folder_id=?', (folder_id,))
                conn.execute('DELETE FROM topic_folders WHERE id=?', (folder_id,))
                flash('Papka va uning ichidagi barcha kunlar hamda so‘zlar o‘chirildi.')
            elif action == 'add_day':
                folder_id = request.form.get('folder_id', type=int)
                folder = conn.execute('SELECT id FROM topic_folders WHERE id=?', (folder_id,)).fetchone()
                if folder:
                    number = conn.execute('SELECT COALESCE(MAX(day_number),0)+1 FROM topic_days WHERE folder_id=?', (folder_id,)).fetchone()[0]
                    conn.execute('INSERT INTO topic_days (folder_id, day_number, name) VALUES (?,?,?)', (folder_id, number, f'DAY {number}'))
                    flash(f'DAY {number} yaratildi.')
            elif action == 'rename_day':
                day_id = request.form.get('day_id', type=int)
                name = request.form.get('name', '').strip()
                if day_id and name:
                    conn.execute('UPDATE topic_days SET name=? WHERE id=?', (name, day_id))
                    flash('DAY nomi yangilandi.')
            elif action == 'delete_day':
                day_id = request.form.get('day_id', type=int)
                conn.execute('DELETE FROM day_completions WHERE day_id=?', (day_id,))
                conn.execute('DELETE FROM topic_words WHERE day_id=?', (day_id,))
                conn.execute('DELETE FROM topic_days WHERE id=?', (day_id,))
                flash('DAY va uning so‘zlari o‘chirildi.')
            elif action == 'add_word':
                day_id = request.form.get('day_id', type=int)
                round_number = request.form.get('round_number', type=int)
                korean = request.form.get('korean', '').strip()
                uzbek = request.form.get('uzbek', '').strip()
                pronunciation = request.form.get('pronunciation', '').strip()
                example = request.form.get('example', '').strip()
                part_of_speech = request.form.get('part_of_speech', 'Ko‘rsatilmagan').strip() or 'Ko‘rsatilmagan'
                image_url = request.form.get('image_url', '').strip()
                if round_number not in (1, 2) or not korean or not uzbek:
                    flash('Round, koreyscha so‘z va o‘zbekcha tarjimani kiriting.')
                else:
                    limit = 12 if round_number == 1 else 13
                    count = conn.execute('SELECT COUNT(*) FROM topic_words WHERE day_id=? AND round_number=?', (day_id, round_number)).fetchone()[0]
                    if count >= limit:
                        flash(f'Round {round_number} to‘ldi: ko‘pi bilan {limit} ta so‘z kiritish mumkin.')
                    elif not conn.execute('SELECT id FROM topic_days WHERE id=?', (day_id,)).fetchone():
                        flash('DAY topilmadi.')
                    else:
                        if not pronunciation:
                            pronunciation = auto_pronunciation(korean)
                        conn.execute('INSERT INTO topic_words (day_id,round_number,position,korean,uzbek,pronunciation,example,part_of_speech,image_url) VALUES (?,?,?,?,?,?,?,?,?)',
                            (day_id, round_number, count+1, korean, uzbek, pronunciation, example, part_of_speech, image_url))
                        flash(f'So‘z Round {round_number} ga saqlandi ({count+1}/{limit}).')
            elif action == 'edit_word':
                word_id = request.form.get('word_id', type=int)
                day_id = request.form.get('day_id', type=int)
                round_number = request.form.get('round_number', type=int)
                korean = request.form.get('korean', '').strip()
                uzbek = request.form.get('uzbek', '').strip()
                pronunciation = request.form.get('pronunciation', '').strip()
                example = request.form.get('example', '').strip()
                part_of_speech = request.form.get('part_of_speech', 'Ko‘rsatilmagan').strip() or 'Ko‘rsatilmagan'
                image_url = request.form.get('image_url', '').strip()
                existing = conn.execute('SELECT id, day_id, round_number FROM topic_words WHERE id=?', (word_id,)).fetchone()
                if not existing or existing['day_id'] != day_id:
                    flash('Tahrirlanadigan so‘z topilmadi.')
                elif round_number not in (1, 2) or not korean or not uzbek:
                    flash('Round, koreyscha so‘z va o‘zbekcha tarjimani kiriting.')
                else:
                    limit = 12 if round_number == 1 else 13
                    count = conn.execute('SELECT COUNT(*) FROM topic_words WHERE day_id=? AND round_number=? AND id<>?', (day_id, round_number, word_id)).fetchone()[0]
                    if count >= limit:
                        flash(f'Round {round_number} to‘la: ko‘pi bilan {limit} ta so‘z kiritish mumkin.')
                    else:
                        if not pronunciation:
                            pronunciation = auto_pronunciation(korean)
                        conn.execute('UPDATE topic_words SET round_number=?, korean=?, uzbek=?, pronunciation=?, example=?, part_of_speech=?, image_url=? WHERE id=? AND day_id=?',
                            (round_number, korean, uzbek, pronunciation, example, part_of_speech, image_url, word_id, day_id))
                        flash('So‘z muvaffaqiyatli tahrirlandi.')
            elif action == 'delete_word':
                word_id = request.form.get('word_id', type=int)
                conn.execute('DELETE FROM topic_words WHERE id=?', (word_id,))
                flash('So‘z o‘chirildi.')
        message = sync_drive_after_change()
        if message: flash(message)
        if action == 'add_day':
            return redirect(url_for('topic_manager', folder_id=request.form.get('folder_id') or request.args.get('folder_id')) + '#topic-day-list')
        return redirect(url_for('topic_manager', folder_id=request.form.get('folder_id') or request.args.get('folder_id'), day_id=request.form.get('day_id') or request.args.get('day_id'), round_number=request.form.get('round_number') or request.args.get('round_number')))

    selected_folder_id = request.args.get('folder_id', type=int)
    selected_day_id = request.args.get('day_id', type=int)
    selected_round = request.args.get('round_number', type=int) or 1
    if selected_round not in (1, 2):
        selected_round = 1
    with db() as conn:
        folders = conn.execute('SELECT * FROM topic_folders ORDER BY id').fetchall()
        selected_folder = conn.execute('SELECT * FROM topic_folders WHERE id=?', (selected_folder_id,)).fetchone() if selected_folder_id else None
        if not selected_folder and folders:
            selected_folder = folders[0]
            selected_folder_id = selected_folder['id']
        days = conn.execute('SELECT * FROM topic_days WHERE folder_id=? ORDER BY day_number', (selected_folder_id,)).fetchall() if selected_folder_id else []
        selected_day = conn.execute('SELECT * FROM topic_days WHERE id=?', (selected_day_id,)).fetchone() if selected_day_id else None
        if not selected_day and days:
            selected_day = days[0]
            selected_day_id = selected_day['id']
        words = conn.execute('SELECT * FROM topic_words WHERE day_id=? AND round_number=? ORDER BY position', (selected_day_id, selected_round)).fetchall() if selected_day_id else []
        edit_word_id = request.args.get('edit_word_id', type=int)
        editing_word = conn.execute('SELECT * FROM topic_words WHERE id=? AND day_id=?', (edit_word_id, selected_day_id)).fetchone() if edit_word_id and selected_day_id else None
        counts = {r: (conn.execute('SELECT COUNT(*) FROM topic_words WHERE day_id=? AND round_number=?', (selected_day_id, r)).fetchone()[0] if selected_day_id else 0) for r in (1,2)}
    body = '''<style>
/* Papka va kunlar boshqaruvi: ixcham, tartibli kartochkalar */
/* Ixcham panel va DAY kartochkalari — funksiyalar saqlangan */
.topic-manager-hero{padding:22px 24px!important;border-radius:22px!important;background:linear-gradient(115deg,rgba(112,64,240,.10),rgba(40,116,240,.07),rgba(34,211,238,.08));border:1px solid var(--border);box-shadow:0 12px 32px var(--shadow)}.topic-manager-hero h1{letter-spacing:-.8px}.topic-manager-hero .muted{max-width:850px;line-height:1.7}
.topic-manager-top{width:100%;max-width:100%;display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:16px;align-items:start;margin-top:14px}.topic-manager-top>*{min-width:0}.topic-manager-top form{max-width:100%}
.topic-manager-top>.panel{min-width:0;padding:17px!important;border:1px solid color-mix(in srgb,var(--border) 78%,transparent)!important;border-radius:20px!important;box-shadow:0 12px 30px var(--shadow);background:linear-gradient(145deg,var(--panel),var(--surface))}.topic-manager-top>.panel:first-child{position:relative;overflow:hidden}.topic-manager-top>.panel:first-child:before{content:'01';position:absolute;right:14px;top:4px;font-size:54px;line-height:1;font-weight:900;letter-spacing:-4px;color:color-mix(in srgb,var(--purple) 9%,transparent);pointer-events:none}.topic-manager-top>.panel:nth-child(2){position:relative}.topic-manager-top>section.panel:first-child{grid-column:1;grid-row:1}.topic-manager-top>section.panel:nth-child(3){grid-column:2;grid-row:1}.topic-manager-top>.topic-day-panel{grid-column:3;grid-row:1}.topic-manager-top>.topic-day-panel:before{content:none!important}
.topic-manager-top h2{position:relative;font-size:16px;letter-spacing:-.25px;margin:0 0 10px}.topic-manager-top hr{border:0;border-top:1px solid var(--border);margin:16px 0}.topic-manager-top .muted{font-size:12px;line-height:1.65}
.topic-manager-top .field{margin-bottom:7px}
.topic-manager-top .field label{font-size:12px;margin-bottom:3px}
.topic-manager-top .field input{padding:8px 10px;min-height:34px;font-size:13px}
.topic-manager-top .topic-folder-list{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,155px),1fr));gap:9px;margin:10px 0 14px}
.topic-folder-link{display:flex;align-items:center;gap:8px;min-width:0;min-height:42px;padding:9px 11px;border:1px solid var(--border);border-radius:12px;background:var(--panel2);font-weight:750;font-size:12px;overflow-wrap:anywhere;transition:transform .18s,box-shadow .18s,border-color .18s}.topic-folder-link:hover{transform:translateY(-2px);border-color:var(--accent);box-shadow:0 7px 17px var(--shadow)}
.topic-folder-link.active{background:linear-gradient(105deg,#7041f5,#3988f6);border-color:transparent;color:#fff}
.topic-day-list{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,220px),1fr));gap:10px;margin-top:12px;max-height:360px;overflow-y:auto;overflow-x:hidden;align-content:start;padding:2px 6px 2px 2px;scrollbar-gutter:stable}.topic-day-list>.topic-day-card{min-width:0}
.topic-day-card{min-width:0;margin:0!important;padding:9px!important;border-radius:11px!important;display:grid;gap:6px}
.topic-day-card strong{font-size:13px;overflow-wrap:anywhere}
.topic-day-actions,.topic-day-rename{display:flex;align-items:center;gap:5px;flex-wrap:wrap;margin:0!important}
.topic-day-actions form{margin:0!important}
.topic-day-rename input{flex:1 1 100px;min-width:0;width:100%;padding:7px 8px;font-size:12px;min-height:32px}
.topic-day-rename .btn,.topic-day-actions .btn{white-space:nowrap;padding:6px 8px;font-size:11px}
.topic-folder-edit{display:grid;grid-template-columns:1fr;gap:7px}
.topic-folder-buttons{display:flex;gap:6px;flex-wrap:wrap;align-items:center}.topic-manager-top .btn{padding:7px 10px;font-size:12px}.topic-manager-top .btn.danger{white-space:normal;max-width:100%;line-height:1.3}.topic-manager-top form[onsubmit*='Papka ichidagi']{display:flex;justify-content:flex-start}.topic-manager-top form[onsubmit*='Papka ichidagi'] .btn{width:auto;max-width:100%}
@media(max-width:980px){.topic-manager-top{grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}.topic-manager-top>section.panel:first-child{grid-column:1;grid-row:1}.topic-manager-top>section.panel:nth-child(3){grid-column:2;grid-row:1}.topic-manager-top>.topic-day-panel{grid-column:1/-1;grid-row:2}}@media(max-width:640px){.topic-manager-top{grid-template-columns:1fr;gap:10px}.topic-manager-top>.topic-day-panel,.topic-manager-top>section.panel:nth-child(3),.topic-manager-top>section.panel:first-child{grid-column:1;grid-row:auto}.topic-manager-top>.panel{padding:12px!important}}
</style><section class="hero topic-manager-hero"><div class="eyebrow">ADMIN • MAVZULAR</div><h1>📁 Papka va kunlar boshqaruvi</h1><p class="muted">Papkalar va DAY soni cheklanmagan. So‘z turkumi, avtomatik talaffuz, misol gap va ixtiyoriy rasm URL ham kiritiladi. Round 1 — 12 ta, Round 2 — 13 ta so‘z.</p></section>
    <div class="topic-manager-top"><section class="panel"><h2>＋ Yangi papka</h2><form method="post"><input type="hidden" name="action" value="create_folder"><div class="field"><label>Papka nomi</label><input name="name" placeholder="Masalan: Papka 1" required></div><div class="field"><label>Izoh (ixtiyoriy)</label><input name="description" placeholder="Boshlang‘ich koreys tili"></div><button class="btn primary" type="submit">Papka yaratish</button></form>
    {% if selected_folder %}<hr><h2>✎ {{ selected_folder.name }}</h2><form method="post"><input type="hidden" name="action" value="rename_folder"><input type="hidden" name="folder_id" value="{{ selected_folder.id }}"><div class="field"><label>Papka nomi</label><input name="name" value="{{ selected_folder.name }}" required></div><div class="field"><label>Izoh</label><input name="description" value="{{ selected_folder.description }}"></div><button class="btn" type="submit">Saqlash</button></form><form method="post" style="margin-top:10px" onsubmit="return confirm('Papka ichidagi barcha DAY va so‘zlar o‘chadi. Davom etasizmi?')"><input type="hidden" name="action" value="delete_folder"><input type="hidden" name="folder_id" value="{{ selected_folder.id }}"><button class="btn danger" type="submit">Papka va ichidagilarni o‘chirish</button></form>{% endif %}</section>
    <section class="panel topic-day-panel">{% if selected_folder %}<h2>📅 DAY ro‘yxati</h2><form method="post"><input type="hidden" name="action" value="add_day"><input type="hidden" name="folder_id" value="{{ selected_folder.id }}"><button class="btn primary" type="submit">＋ Keyingi DAY ni qo‘shish</button></form><div class="topic-day-list" id="topic-day-list">{% for d in days %}<article class="word topic-day-card"><strong>📅 {{ d.name }}</strong><div class="topic-day-actions"><a class="btn" href="{{ url_for('topic_manager', folder_id=selected_folder.id, day_id=d.id) }}">So‘zlarni boshqarish</a><form method="post" onsubmit="return confirm('DAY va uning so‘zlarini o‘chirasizmi?')"><input type="hidden" name="action" value="delete_day"><input type="hidden" name="folder_id" value="{{ selected_folder.id }}"><input type="hidden" name="day_id" value="{{ d.id }}"><button class="btn danger" type="submit">O‘chirish</button></form></div><form method="post" class="topic-day-rename"><input type="hidden" name="action" value="rename_day"><input type="hidden" name="folder_id" value="{{ selected_folder.id }}"><input type="hidden" name="day_id" value="{{ d.id }}"><input name="name" value="{{ d.name }}" required aria-label="DAY nomi"><button class="btn" type="submit">Nomini saqlash</button></form></article>{% endfor %}</div>{% else %}<h2>📅 DAY ro‘yxati</h2><p class="muted">DAYlarni ko‘rish va qo‘shish uchun avval papkani tanlang.</p>{% endif %}</section>
    <section class="panel"><h2>📚 Papkalar va kunlar</h2>{% if folders %}<div class="topic-folder-list">{% for f in folders %}<a class="topic-folder-link {{ 'active' if selected_folder and selected_folder.id==f.id else '' }}" href="{{ url_for('topic_manager', folder_id=f.id) }}">📁 <span>{{ f.name }}</span></a>{% endfor %}</div>{% else %}<p class="muted">Hali papka yo‘q.</p>{% endif %}
    </section></div>
    {% if selected_day %}<style>
/* So‘z kiritish va kiritilgan so‘zlar — ikkita alohida panel */
.day-word-layout{width:100%;max-width:100%;min-width:0;display:grid;grid-template-columns:minmax(310px,.618fr) minmax(0,1fr);gap:18px;align-items:start;margin-top:16px}.day-word-layout>*{min-width:0}
.day-word-panel,.day-words-panel{width:100%;min-width:0;margin:0!important;padding:16px!important;border-radius:20px!important;box-shadow:0 10px 28px var(--shadow);text-align:left}
.day-word-panel h2,.day-words-panel h2{font-size:18px;margin:0 0 7px}
.day-word-panel .muted,.day-words-panel .muted{font-size:12px;margin:0 0 10px}
.day-word-form{display:grid;grid-template-columns:minmax(0,1fr);gap:10px;align-items:stretch}
.day-word-form .field{margin:0;min-width:0}
.day-word-form .field:first-of-type select{border-radius:12px;background:var(--panel2);font-weight:750;color:var(--text)}
.day-word-form .field input,.day-word-form .field select,.day-word-form .field textarea{border-radius:12px}
.day-word-form .field label{font-size:12px;margin-bottom:3px}
.day-word-form input,.day-word-form select,.day-word-form textarea{width:100%;min-width:0;padding:8px 10px;font-size:13px;min-height:36px}
.day-word-form textarea{min-height:58px;resize:vertical}
.day-word-form .field small{display:block;font-size:10px;line-height:1.35;margin-top:3px}
.day-word-form button{grid-column:1/-1;padding:9px 12px}
.day-words-panel .cards{width:100%;min-width:0;display:grid;grid-template-columns:repeat(3,minmax(0,1fr));grid-auto-flow:row;grid-auto-rows:auto;gap:8px;margin:10px 0 0;overflow:visible;padding:0;align-items:stretch}
.day-words-panel .cards>.word{min-width:0;padding:13px!important;border-radius:16px!important;display:flex;flex-direction:column;align-items:flex-start;gap:5px;box-shadow:0 4px 14px var(--shadow)}
.day-words-panel .cards .hangul{font-size:25px;line-height:1.25;overflow-wrap:anywhere}
.day-words-panel .cards .translation{font-size:14px;font-weight:750;overflow-wrap:anywhere}
.day-words-panel .cards .pron,.day-words-panel .cards .example{font-size:12px;overflow-wrap:anywhere}
.day-words-panel .cards form{margin-top:auto!important;width:100%}
.day-words-panel .cards form .btn{width:100%;padding:7px 10px;font-size:12px}
.topic-word-actions{display:flex;align-items:stretch;gap:6px;flex-wrap:nowrap;width:100%;margin-top:auto}.topic-word-actions>a.btn,.topic-word-actions form{flex:1 1 0;min-width:0;margin:0!important;width:0!important}.topic-word-actions .btn{display:flex;align-items:center;justify-content:center;width:100%;min-width:0;padding:7px 6px;font-size:12px;white-space:nowrap}.day-word-form .cancel-edit{display:block;text-align:center}
.day-words-panel .tag{font-size:10px;padding:3px 8px}
@media(max-width:900px){.day-word-layout{grid-template-columns:minmax(220px,.6fr) minmax(0,1.4fr);gap:12px}.day-word-panel,.day-words-panel{padding:12px!important}}
@media(max-width:680px){.day-word-layout{grid-template-columns:1fr}.day-word-form{grid-template-columns:minmax(0,1fr);gap:10px}.day-word-panel .cards{grid-template-columns:repeat(2,minmax(0,1fr));gap:8px}.day-words-panel .cards{grid-template-columns:repeat(3,minmax(0,1fr));grid-auto-flow:row;gap:6px}.day-words-panel .cards>.word{padding:8px!important}.day-words-panel .cards .hangul{font-size:21px}.day-words-panel .cards .translation{font-size:12px}.day-words-panel .cards .pron,.day-words-panel .cards .example{font-size:10px}.day-words-panel .topic-word-actions{flex-wrap:wrap}.day-words-panel .topic-word-actions .btn{white-space:normal;font-size:10px;padding:6px 4px}}
@media(max-width:480px){.day-word-form{grid-template-columns:minmax(0,1fr)}.day-word-form button{grid-column:auto}}
@media(max-width:390px){.day-words-panel .cards{grid-template-columns:repeat(3,minmax(0,1fr));gap:5px}.day-words-panel .cards>.word{display:flex;flex-direction:column;align-items:flex-start;padding:6px!important}.day-words-panel .cards form{grid-column:auto}.day-words-panel .cards .hangul{font-size:19px}.day-words-panel .topic-word-actions{gap:4px}}
</style><div class="day-word-layout"><section class="panel day-word-panel" id="day-word-entry"><h2>✍ {{ selected_day.name }} — {{ "so‘zni tahrirlash" if editing_word else "so‘z kiritish" }}</h2><p class="muted">Round 1: {{ counts[1] }}/12 · Round 2: {{ counts[2] }}/13</p><form class="day-word-form" method="post"><input type="hidden" name="action" value="{{ 'edit_word' if editing_word else 'add_word' }}"><input type="hidden" name="folder_id" value="{{ selected_folder.id }}"><input type="hidden" name="day_id" value="{{ selected_day.id }}">{% if editing_word %}<input type="hidden" name="word_id" value="{{ editing_word.id }}">{% endif %}<div class="field"><label>Round</label><select name="round_number" onchange="window.location.href='{{ url_for('topic_manager', folder_id=selected_folder.id, day_id=selected_day.id) }}&round_number='+this.value+'#day-word-entry'"><option value="1" {% if (editing_word and editing_word.round_number==1) or (not editing_word and selected_round==1) %}selected{% endif %}>Round 1 — 12 ta</option><option value="2" {% if (editing_word and editing_word.round_number==2) or (not editing_word and selected_round==2) %}selected{% endif %}>Round 2 — 13 ta</option></select></div><div class="field"><label>Koreyscha so‘z *</label><input name="korean" value="{{ editing_word.korean if editing_word else '' }}" placeholder="가족" required></div><div class="field"><label>O‘zbekcha tarjima *</label><input name="uzbek" value="{{ editing_word.uzbek if editing_word else '' }}" placeholder="Oila" required></div><div class="field"><label>So‘z turkumi *</label><select name="part_of_speech" required>{% for pos in ["Ot", "Fe’l", "Sifat", "Ravish", "Olmosh", "Son", "Ko‘makchi", "Bog‘lovchi", "Undov", "Ibora", "Boshqa", "Ko‘rsatilmagan"] %}<option value="{{ pos }}" {% if (editing_word and pos == (editing_word.part_of_speech or 'Ko‘rsatilmagan')) or (not editing_word and pos == 'Ko‘rsatilmagan') %}selected{% endif %}>{{ pos }}</option>{% endfor %}</select><small class="muted">Masalan: 가족 — Ot, 예쁘다 — Sifat, 먹다 — Fe’l.</small></div><div class="field"><label>Talaffuz (avtomatik)</label><input name="pronunciation" value="{{ editing_word.pronunciation if editing_word else '' }}" placeholder="Bo‘sh qoldirilsa avtomatik"><small class="muted">Koreyscha so‘zni yozing; talaffuzni o‘zingiz ham tahrirlashingiz mumkin.</small></div><div class="field"><label>Misol gap</label><textarea name="example" placeholder="우리 가족">{{ editing_word.example if editing_word else '' }}</textarea></div><div class="field"><label>Rasm URL (ixtiyoriy)</label><input type="url" name="image_url" value="{{ editing_word.image_url if editing_word else '' }}" placeholder="https://.../image.jpg"></div><button class="btn primary" type="submit">{{ '💾 O‘zgarishlarni saqlash' if editing_word else '＋ So‘zni saqlash' }}</button>{% if editing_word %}<a class="btn" href="{{ url_for('topic_manager', folder_id=selected_folder.id, day_id=selected_day.id, round_number=selected_round) }}">Bekor qilish</a>{% endif %}</form></section><section class="panel day-words-panel"><h2>📚 Kiritilgan so‘zlar</h2><p class="muted">Ushbu DAY uchun saqlangan so‘zlar alohida kartochkalarda.</p>{% if words %}<div class="cards">{% for w in words %}<article class="word"><div class="hangul">{{ w.korean }}</div><div class="translation">{{ w.uzbek }}</div><div class="pron">Round {{ w.round_number }} · {{ w.pronunciation }}</div><span class="tag">{{ w.part_of_speech or 'Ko‘rsatilmagan' }}</span>{% if w.example %}<div class="example">Misol: {{ w.example }}</div>{% endif %}{% if w.image_url %}<a href="{{ w.image_url }}" target="_blank" rel="noopener">Rasmni ko‘rish</a>{% endif %}<div class="topic-word-actions"><a class="btn" href="{{ url_for('topic_manager', folder_id=selected_folder.id, day_id=selected_day.id, round_number=w.round_number, edit_word_id=w.id) }}#day-word-entry">✏️ Tahrirlash</a><form method="post" onsubmit="return confirm('So‘zni o‘chirasizmi?')"><input type="hidden" name="action" value="delete_word"><input type="hidden" name="folder_id" value="{{ selected_folder.id }}"><input type="hidden" name="day_id" value="{{ selected_day.id }}"><input type="hidden" name="round_number" value="{{ w.round_number }}"><input type="hidden" name="word_id" value="{{ w.id }}"><button class="btn danger" type="submit">O‘chirish</button></form></div></article>{% endfor %}</div>{% else %}<p class="muted">Bu DAY ga hali so‘z kiritilmagan.</p>{% endif %}</section></div>{% endif %}
    <div style="margin-top:18px"><a class="btn" href="{{ url_for('topics') }}">Mavzularni ko‘rish</a> <a class="btn" href="{{ url_for('admin') }}">Admin panel</a></div>'''
    return page('Mavzular boshqaruvi', body, folders=folders, selected_folder=selected_folder, days=days, selected_day=selected_day, words=words, counts=counts, selected_round=selected_round, editing_word=editing_word)


@app.route('/statistics')
def statistics():
    # Eski havolalar ishlashda davom etadi; statistika Sozlamalar ichiga ko‘chirildi.
    return redirect(url_for('settings') + '#statistics')


@app.route('/achievements')
def achievements():
    body = '''<section class="achievements-page">
      <header class="achievements-heading"><div class="achievements-heading-icon">🏆</div><div><p class="achievements-eyebrow">HAR BIR QADAM — YUTUQ</p><h1>Yutuqlar</h1><p class="muted">O‘rganishda davom eting va yangi marralarni zabt eting!</p></div></header>
      <div class="achievements-grid">
        <article class="word achievement-tile"><div class="achievement-icon badge-orange">✹</div><span class="achievement-type">BOSHLANG‘ICH</span><h2>Birinchi so‘z</h2><p class="muted">Koreys tilidagi ilk so‘zingizni o‘rganing.</p><div class="achievement-reward">⚡ <strong>+10 XP</strong></div></article>
        <article class="word achievement-tile"><div class="achievement-icon badge-fire">🔥</div><span class="achievement-type">MUNTazamLIK</span><h2>7 kun streak</h2><p class="muted">Bir hafta davomida har kuni shug‘ullaning.</p><div class="achievement-reward">🔥 <strong>Bir haftalik davomiylik</strong></div></article>
        <article class="word achievement-tile"><div class="achievement-icon badge-blue">♧</div><span class="achievement-type">SO‘Z BOYLIGI</span><h2>100 so‘z</h2><p class="muted">Lug‘atingizni 100 ta so‘zgacha kengaytiring.</p><div class="achievement-reward">📚 <strong>Yangi so‘zlar yutug‘i</strong></div></article>
      </div>
    </section>
    <style>
      .achievements-page{max-width:1120px;margin:8px auto;padding:8px 2px 28px}
      .achievements-heading{display:flex;align-items:center;gap:18px;margin:0 0 28px;padding:24px 26px;border:1px solid var(--border);border-radius:20px;background:linear-gradient(120deg,rgba(255,255,255,.96),rgba(233,243,255,.9));box-shadow:0 10px 28px var(--shadow)}
      :root[data-theme='dark'] .achievements-heading{background:linear-gradient(120deg,rgba(18,31,69,.96),rgba(27,46,91,.92))}
      .achievements-heading-icon{width:64px;height:64px;flex:0 0 64px;display:grid;place-items:center;border-radius:18px;background:linear-gradient(140deg,#ffcf70,#ff8a54);font-size:32px;box-shadow:0 8px 18px rgba(245,147,65,.24)}
      .achievements-eyebrow{margin:0 0 3px;color:var(--purple);font-size:10px;font-weight:850;letter-spacing:1.6px}
      .achievements-heading h1{margin:0;font-size:clamp(26px,3vw,34px);line-height:1.2}
      .achievements-heading .muted{margin:7px 0 0;font-size:13px}
      .achievements-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:18px;align-items:stretch}
      .achievements-grid .achievement-tile{position:relative;min-width:0;min-height:250px;display:flex;flex-direction:column;align-items:center;padding:25px 20px 20px;border:1px solid var(--border);border-radius:18px;background:var(--panel);box-shadow:0 8px 24px var(--shadow);transition:transform .18s,box-shadow .18s}
      .achievements-grid .achievement-tile:hover{transform:translateY(-3px);box-shadow:0 14px 28px var(--shadow)}
      .achievements-grid .achievement-icon{width:66px;height:66px;margin:0 0 14px;border-radius:20px;font-size:30px;box-shadow:0 7px 16px rgba(40,75,130,.12)}
      .achievement-type{margin-bottom:5px;color:var(--muted);font-size:9px;font-weight:850;letter-spacing:1.3px}
      .achievements-grid .achievement-tile h2{margin:0;text-align:center;font-size:18px;line-height:1.35}
      .achievements-grid .achievement-tile>p{min-height:42px;margin:9px 0 16px;text-align:center;font-size:12px;line-height:1.6}
      .achievement-reward{width:100%;margin-top:auto;padding:10px 9px;border:1px solid var(--border);border-radius:11px;background:var(--panel2);display:flex;justify-content:center;align-items:center;gap:7px;text-align:center;font-size:12px}
      .achievement-reward strong{font-size:12px}
      @media(max-width:850px){.achievements-grid{grid-template-columns:repeat(2,minmax(0,1fr))}}
      @media(max-width:560px){.achievements-page{padding:0}.achievements-heading{padding:18px 16px;gap:13px}.achievements-heading-icon{width:52px;height:52px;flex-basis:52px;font-size:26px}.achievements-heading .muted{font-size:12px}.achievements-grid{grid-template-columns:1fr;gap:12px}.achievements-grid .achievement-tile{min-height:0;padding:20px 18px}.achievements-grid .achievement-tile>p{min-height:0}}
    </style>'''
    return page('Yutuqlar',body)


@app.route('/settings')
def settings():
    with db() as conn:
        legacy_word_count = conn.execute('SELECT COUNT(*) FROM words').fetchone()[0]
        topic_word_count = conn.execute('SELECT COUNT(*) FROM topic_words').fetchone()[0]
        total_words = legacy_word_count + topic_word_count
        legacy_section_count = conn.execute('SELECT COUNT(*) FROM sections').fetchone()[0]
        folder_count = conn.execute('SELECT COUNT(*) FROM topic_folders').fetchone()[0]
        total_sections = legacy_section_count + folder_count
        total_xp = conn.execute('SELECT COALESCE((SELECT SUM(xp_awarded) FROM day_completions),0) + COALESCE((SELECT SUM(xp_awarded) FROM round_completions),0)').fetchone()[0]
        completed_rounds = conn.execute('SELECT COUNT(*) FROM round_completions').fetchone()[0]
    body = '''<section class="panel settings-panel"><h2>⚙️ Ko‘rinish rejimi</h2><div class="settings-actions"><button class="btn" type="button" onclick="setTheme('light')">☀️ Kunduzgi rejim</button><button class="btn" type="button" onclick="setTheme('dark')">🌙 Tungi rejim</button></div></section>
    <section class="panel" id="statistics" style="margin-top:16px"><h2>📊 Statistika</h2><p class="muted">O‘rganish jarayoningiz bo‘yicha umumiy ko‘rsatkichlar.</p><div class="dashboard-metrics stats-page-metrics"><article class="metric-card"><span class="metric-icon books">▤</span><div><small>So‘zlar</small><strong>{{ total_words }}</strong><span>Eski bo‘limlar va mavzu papkalaridagi so‘zlar</span></div></article><article class="metric-card"><span class="metric-icon star">📁</span><div><small>Mavzular</small><strong>{{ total_sections }}</strong><span>Bo‘limlar va papkalar</span></div></article><article class="metric-card"><span class="metric-icon xp">ϟ</span><div><small>XP</small><strong>{{ total_xp }}</strong><span>{{ completed_rounds }} ta Round yakunlandi</span></div></article><article class="metric-card"><span class="metric-icon fire">🔥</span><div><small>Streak</small><strong>7 kun</strong><span>Ketma-ket kunlar</span></div></article></div></section>'''
    return page('Sozlamalar',body,total_words=total_words,total_sections=total_sections,total_xp=total_xp,completed_rounds=completed_rounds)


@app.route('/search')
def search_words():
    q = request.args.get('q', '').strip()
    if not q:
        return redirect(url_for('vocabulary'))
    term = f'%{q}%'
    with db() as conn:
        words = conn.execute(
            "SELECT * FROM words WHERE korean LIKE ? OR uzbek LIKE ? OR pronunciation LIKE ? OR category LIKE ? OR example LIKE ? ORDER BY korean COLLATE NOCASE LIMIT 100",
            (term, term, term, term, term)
        ).fetchall()
    body = '''<section class="hero"><div class="eyebrow">HANGEUL • QIDIRUV</div><h1>Qidiruv natijalari</h1><p class="muted">“{{ q }}” bo‘yicha {{ words|length }} ta natija topildi.</p></section>{% if words %}<div class="cards">{% for w in words %}<article class="word">{% if w.image_url %}<img class="word-image" src="{{ w.image_url }}" alt="{{ w.korean }}" loading="lazy">{% endif %}<div style="display:flex;align-items:center;justify-content:space-between;gap:12px"><div class="hangul">{{ w.korean }}</div><button class="section-audio" type="button" onclick="speakKorean(this.dataset.korean)" data-korean="{{ w.korean }}" aria-label="Talaffuzni eshitish">🔊</button></div><div class="pron">{{ w.pronunciation or 'Talaffuz kiritilmagan' }}</div><div class="translation">{{ w.uzbek }}</div><span class="tag">{{ w.category }}</span>{% if w.example %}<div class="example">Misol: {{ w.example }}</div>{% endif %}</article>{% endfor %}</div>{% else %}<div class="empty">Bu so‘z bo‘yicha natija topilmadi.</div>{% endif %}'''
    return page('Qidiruv', body, q=q, words=words)


@app.route('/section/<int:section_id>')
def section_words(section_id):
    q=request.args.get('q','').strip()
    with db() as conn:
        section=conn.execute('SELECT * FROM sections WHERE id=?',(section_id,)).fetchone()
        if not section: abort(404)
        term=f'%{q}%'
        words=conn.execute('''SELECT * FROM words WHERE category=? AND (korean LIKE ? OR uzbek LIKE ? OR pronunciation LIKE ? OR example LIKE ?) ORDER BY korean COLLATE NOCASE''',(section['name'],term,term,term,term)).fetchall()
    body='''<section class="hero"><div class="eyebrow"><a href="{{ url_for('vocabulary') }}">MAVZULAR</a> / BO‘LIM</div><h1>📁 {{ section.name }}</h1><p class="muted">{{ section.description or 'Bu bo‘limdagi koreyscha so‘zlarni o‘rgan.' }}</p></section>
    <form class="searchrow" method="get"><input name="q" value="{{ q }}" placeholder="Faqat shu bo‘limdan qidirish…"><button class="btn primary" type="submit">Qidirish</button><a class="btn" href="{{ url_for('vocabulary') }}">Barcha bo‘limlar</a></form><p class="muted small">{{ words|length }} ta so‘z</p>
    {% if words %}<div class="cards">{% for w in words %}<article class="word">{% if w.image_url %}<img class="word-image" src="{{ w.image_url }}" alt="{{ w.korean }}" loading="lazy">{% endif %}<div style="display:flex;align-items:center;gap:50px"><div class="hangul">{{ w.korean }}</div><button class="section-audio" type="button" onclick="speakKorean(this.dataset.korean)" data-korean="{{ w.korean }}" aria-label="Koreyscha talaffuzni eshitish" title="Talaffuzni eshitish">🔊</button></div><div class="pron">{{ w.pronunciation or 'Talaffuz kiritilmagan' }}</div><div class="translation">{{ w.uzbek }}</div>{% if w.example %}<div class="example">Misol: {{ w.example }}</div>{% endif %}</article>{% endfor %}</div>{% else %}<div class="empty">Bu bo‘limda hozircha so‘z yo‘q.</div>{% endif %}
    <section class="wongoji-section" aria-label="Wongoji testi"><div class="wongoji-heading"><h2>원고지 · Wongoji test</h2><span class="muted small">O‘zbekcha ma’noga mos koreyscha so‘zni kataklarga yozing.</span></div>
    {% if words %}<div class="wongoji-test-card"><div class="wongoji-test-top"><div><span class="muted small">Tarjimasi</span><div id="wgPrompt" class="wg-prompt"></div></div><div class="wg-counter" id="wgCounter">1 / {{ words|length }}</div></div>
    <div class="wg-paper-wrap"><div id="wgCells" class="wg-cells" role="textbox" aria-label="Wongoji kataklariga koreyscha yozing" tabindex="0"></div></div>
    <div class="wg-actions"><button type="button" class="btn" id="wgClear">Tozalash</button><button type="button" class="btn primary" id="wgCheck">Tekshirish</button><button type="button" class="btn" id="wgNext" hidden>Keyingi so‘z →</button></div><p id="wgFeedback" class="wg-feedback" aria-live="polite">Katakni bosib, koreyscha javobni yozing.</p></div>
    <script>(()=>{const items={{ words|map(attribute='korean')|list|tojson }}.map((ko,i)=>({ko,uz:{{ words|map(attribute='uzbek')|list|tojson }}[i]}));const cells=document.getElementById('wgCells'),prompt=document.getElementById('wgPrompt'),counter=document.getElementById('wgCounter'),feedback=document.getElementById('wgFeedback'),check=document.getElementById('wgCheck'),next=document.getElementById('wgNext');let idx=0,value='',done=false;const maxCells=30;
    function render(){cells.innerHTML='';for(let i=0;i<maxCells;i++){const c=document.createElement('span');c.className='wg-cell';c.textContent=[...value][i]||'';cells.appendChild(c)}prompt.textContent=items[idx].uz;counter.textContent=(idx+1)+' / '+items.length;feedback.textContent='Katakni bosib, koreyscha javobni yozing.';feedback.className='wg-feedback';check.hidden=false;next.hidden=true;done=false}
    function add(ch){if(done)return;if(ch==='Backspace'){value=[...value].slice(0,-1).join('')}else if(ch==='Enter'){return verify()}else if(ch.length===1&&value.length<maxCells&&!ch.match(/[\t\n\r]/)){value+=ch} [...cells.children].forEach((c,i)=>c.textContent=[...value][i]||'')}
    cells.addEventListener('keydown',e=>{if(['Backspace','Enter'].includes(e.key)||e.key.length===1){e.preventDefault();add(e.key)}});cells.addEventListener('click',()=>cells.focus());document.getElementById('wgClear').addEventListener('click',()=>{value='';[...cells.children].forEach(c=>c.textContent='');cells.focus()});
    function verify(){if(done)return;const clean=x=>x.normalize('NFC').replace(/\\s+/g,'').trim();if(clean(value)===clean(items[idx].ko)){feedback.textContent='✓ To‘g‘ri! '+items[idx].ko;feedback.className='wg-feedback good';done=true;check.hidden=true;next.hidden=false}else{feedback.textContent='✕ Hali mos kelmadi. Yana urinib ko‘ring.';feedback.className='wg-feedback bad'}}check.addEventListener('click',verify);next.addEventListener('click',()=>{idx=(idx+1)%items.length;value='';render();cells.focus()});render()})();</script>
    {% else %}<p class="empty">Bu bo‘limga Admin panel orqali koreyscha so‘z va o‘zbekcha tarjima kiriting. Ular avtomatik ravishda Wongoji testiga qo‘shiladi.</p>{% endif %}</section>
    <style>.wongoji-section{margin-top:24px;width:100%;display:flex;flex-direction:column;gap:12px}.wongoji-heading{display:flex;align-items:baseline;gap:12px;flex-wrap:wrap}.wongoji-heading h2{margin:0;font-size:22px}.wongoji-test-card{padding:18px;border:1px solid var(--border);border-radius:16px;background:var(--panel)}.wongoji-test-top{display:flex;justify-content:space-between;align-items:center;gap:12px;margin-bottom:16px}.wg-prompt{font-size:clamp(22px,4vw,32px);font-weight:800;margin-top:4px}.wg-counter{white-space:nowrap;font-weight:800}.wg-paper-wrap{overflow:auto;background:#fff;border:1px solid #e4a2ad;border-radius:8px;padding:8px}.wg-cells{display:grid;grid-template-columns:repeat(10,minmax(30px,40px));grid-auto-rows:minmax(30px,40px);gap:0;outline:none;width:max-content;min-width:100%;border-top:1px solid #e5a3ad;border-left:1px solid #e5a3ad;cursor:text}.wg-cell{display:flex;align-items:center;justify-content:center;width:40px;height:40px;border-right:1px solid #e5a3ad;border-bottom:1px solid #e5a3ad;color:#18233b;font-size:25px;font-family:serif}.wg-cells:focus .wg-cell{background:#fffdfd}.wg-actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:14px}.wg-feedback{min-height:24px;margin:12px 0 0;font-weight:700}.wg-feedback.good{color:#16803c}.wg-feedback.bad{color:#c02640}@media(max-width:600px){.wongoji-test-card{padding:10px}.wg-cell{width:34px;height:34px;font-size:22px}.wg-cells{grid-template-columns:repeat(10,34px);grid-auto-rows:34px}}</style>'''
    return page(section['name'],body,section=section,words=words,q=q)

@app.route('/audio')
def korean_audio():
    text = request.args.get('text', '').strip()
    if not text:
        return 'Koreyscha matn berilmadi.', 400
    if len(text) > 200:
        return 'Matn juda uzun.', 400
    try:
        from gtts import gTTS
        audio = io.BytesIO()
        gTTS(text=text, lang='ko', slow=False).write_to_fp(audio)
        audio.seek(0)
        return Response(audio.read(), mimetype='audio/mpeg', headers={'Cache-Control': 'no-store'})
    except ImportError:
        return 'gTTS kutubxonasi o‘rnatilmagan. Terminalda: python3 -m pip install gTTS', 503
    except Exception:
        app.logger.exception('Koreyscha audio yaratishda xato')
        return 'Audio yaratish uchun internet kerak. Internetni tekshirib qayta urinib ko‘ring.', 502





@app.route('/writing')
def writing():
    # Both the legacy vocabulary table and the new Folder > DAY > ROUND tables
    # feed the same practice activity.
    with db() as conn:
        active_day = session.get('active_topic_day_id')
        active_round = session.get('active_topic_round')
        if active_day and active_round:
            rows = conn.execute("SELECT korean, uzbek FROM topic_words WHERE day_id=? AND round_number=? AND TRIM(COALESCE(korean,''))<>'' AND TRIM(COALESCE(uzbek,''))<>'' ORDER BY RANDOM()", (active_day, active_round)).fetchall()
        else:
            rows = conn.execute(
                "SELECT korean, uzbek FROM ("
                "SELECT korean, uzbek FROM words "
                "WHERE TRIM(COALESCE(korean,''))<>'' AND TRIM(COALESCE(uzbek,''))<>'' "
                "UNION ALL "
                "SELECT korean, uzbek FROM topic_words "
                "WHERE TRIM(COALESCE(korean,''))<>'' AND TRIM(COALESCE(uzbek,''))<>''"
                ") ORDER BY RANDOM()"
            ).fetchall()

    seen = set()
    questions = []
    for row in rows:
        ko, uz = row['korean'].strip(), row['uzbek'].strip()
        key = (ko, uz)
        if key not in seen:
            seen.add(key)
            questions.append({'ko': ko, 'uz': uz})
    if not questions:
        body = """<section class='hero practice-shell'><div class='eyebrow'>HANGEUL • MASHQ</div><h1>✍️ Yozib o‘rganish</h1><p class='muted'>Hozircha mashq uchun so‘zlar mavjud emas.</p><p class='muted'>Boshqa bo‘limlardan so‘zlarni o‘rganishingiz mumkin.</p><a class='btn primary' href='{{ url_for('vocabulary') }}'>Bo‘limlarga qaytish</a></section>"""
        return page('Yozib o‘rganish', body)

    body = r'''<style>
      .writing-page{max-width:700px;margin:0 auto;padding:12px;color:var(--text)}
      .writing-head{display:flex;align-items:center;justify-content:center;gap:8px}
      .writing-title{text-align:center;font-size:clamp(20px,4vw,25px);font-weight:800}
      .writing-progress{display:grid;grid-template-columns:repeat({{ questions|length }},minmax(0,1fr));gap:7px;margin:16px 0}
      .writing-progress span{height:7px;border-radius:20px;background:var(--border)}
      .writing-progress span.active{background:#2874f0}
      .writing-stats{display:flex;align-items:center;justify-content:space-between;gap:8px;margin:18px 0 34px}
      .writing-count{font-weight:800;white-space:nowrap}.writing-pills{display:flex;gap:7px}
      .writing-pill{padding:7px 12px;border-radius:999px;font-weight:800;white-space:nowrap}
      .writing-correct{background:#dcfce7;color:#15803d}.writing-wrong{background:#ffe4e6;color:#be123c}
      .writing-prompt{text-align:center;font-size:clamp(42px,9vw,56px);font-weight:850;margin:20px 0 25px;color:var(--text)}
      .writing-input{box-sizing:border-box;width:100%;padding:15px;border:1px solid var(--border);border-radius:14px;background:var(--input);color:var(--text);font:inherit;font-size:21px;text-align:center;outline:none}
      .writing-input:focus{border-color:#2874f0}
      .writing-check{width:100%;margin-top:12px;padding:14px;border:0;border-radius:13px;background:#d1d5db;color:#374151;font:inherit;font-weight:800;cursor:pointer}
      .writing-check:enabled{background:#2874f0;color:white}
      .writing-message{min-height:24px;margin:12px 0;text-align:center;font-weight:750}.writing-message:not(:empty){padding:12px 14px;border:1px solid #d1d5db;border-radius:12px;background:#fff;color:#111827;box-shadow:0 5px 16px rgba(15,23,42,.08)}:root[data-theme='dark'] .writing-message:not(:empty){background:#0b1430;color:#fff;border-color:#283b70}
      .hangul-keyboard{display:grid;gap:7px;margin-top:20px}.hangul-row{display:grid;grid-template-columns:repeat(var(--key-count),minmax(0,1fr));gap:6px}.hangul-row:nth-child(2){width:90%;margin:auto}.hangul-row:nth-child(3){width:72%;margin:auto}
      .hangul-key{min-width:0;padding:12px 2px;border:1px solid var(--border);border-radius:10px;background:var(--panel);color:var(--text);font-size:20px;font-weight:750;cursor:pointer;box-shadow:0 2px 0 var(--border)}
      .hangul-key:active{transform:translateY(2px)}.hangul-key.shifted{background:#dce9ff;color:#174ea6}
      .hangul-controls{display:grid;grid-template-columns:1fr 1fr 1fr;gap:7px;margin-top:12px}
      .hangul-controls button{padding:12px 4px;border:1px solid var(--border);border-radius:10px;background:var(--panel2);color:var(--text);font:inherit;font-weight:750;cursor:pointer}
      .hangul-controls button.active{background:#2874f0;color:white}.hangul-controls .delete-key{background:#ffe4e6;color:#be123c}
      @media(max-width:520px){.writing-page{padding:7px}.hangul-keyboard{gap:5px}.hangul-row{gap:4px}.hangul-row:nth-child(2){width:94%}.hangul-row:nth-child(3){width:82%}.hangul-key{font-size:17px;padding:11px 0}.writing-stats{margin-bottom:25px}}
    </style>
    <main class="writing-page"><div class="practice-shell">
      <header class="writing-head"><div class="writing-title">Yozib o‘rganish</div></header>
      <div class="writing-progress" id="writingProgress">{% for _ in questions %}<span class="{{ 'active' if loop.first else '' }}"></span>{% endfor %}</div>
      <div class="writing-stats"><span class="writing-count" id="writingCount">1 / {{ questions|length }}</span><div class="writing-pills"><span class="writing-pill writing-correct" id="correctCount">✓ 0</span><span class="writing-pill writing-wrong" id="wrongCount">✕ 0</span></div></div>
      <section><div class="writing-prompt" id="writingPrompt">{{ questions[0].uz }}</div><input class="writing-input" id="hangulAnswer" placeholder="Koreyscha yozing..." autocomplete="off" autocapitalize="off" aria-label="Koreyscha javob"><button class="writing-check" id="checkWriting" type="button" disabled>Tekshirish</button><div class="writing-message" id="writingMessage" aria-live="polite"></div></section>
      <section aria-label="Koreyscha virtual klaviatura"><div class="hangul-keyboard" id="hangulKeyboard"></div><div class="hangul-controls"><button type="button" id="shiftKey" aria-pressed="false">⇧ Shift</button><button type="button" id="clearKey">Tozalash</button><button type="button" class="delete-key" id="deleteKey">⌫</button></div></section>
    </div></main>
    <script>
    (() => {
      const questions = {{ questions|tojson }};
      const roundCompletionUrl = {{ url_for('complete_round_activity', activity='writing')|tojson }};
      const roundScoped = {{ (session.get('active_topic_day_id') and session.get('active_topic_round'))|tojson }};
      const input = document.getElementById('hangulAnswer');
      const keyboard = document.getElementById('hangulKeyboard');
      const checkButton = document.getElementById('checkWriting');
      const message = document.getElementById('writingMessage');
      const keyboardRows = [['ㅂ','ㅈ','ㄷ','ㄱ','ㅅ','ㅛ','ㅕ','ㅑ','ㅐ','ㅔ'],['ㅁ','ㄴ','ㅇ','ㄹ','ㅎ','ㅗ','ㅓ','ㅏ','ㅣ'],['ㅋ','ㅌ','ㅊ','ㅍ','ㅠ','ㅜ','ㅡ']];
      const shiftMap = {'ㄱ':'ㄲ','ㄷ':'ㄸ','ㅂ':'ㅃ','ㅅ':'ㅆ','ㅈ':'ㅉ','ㅐ':'ㅒ','ㅔ':'ㅖ'};
      const L = ['ㄱ','ㄲ','ㄴ','ㄷ','ㄸ','ㄹ','ㅁ','ㅂ','ㅃ','ㅅ','ㅆ','ㅇ','ㅈ','ㅉ','ㅊ','ㅋ','ㅌ','ㅍ','ㅎ'];
      const V = ['ㅏ','ㅐ','ㅑ','ㅒ','ㅓ','ㅔ','ㅕ','ㅖ','ㅗ','ㅘ','ㅙ','ㅚ','ㅛ','ㅜ','ㅝ','ㅞ','ㅟ','ㅠ','ㅡ','ㅢ','ㅣ'];
      const T = ['', 'ㄱ','ㄲ','ㄳ','ㄴ','ㄵ','ㄶ','ㄷ','ㄹ','ㄺ','ㄻ','ㄼ','ㄽ','ㄾ','ㄿ','ㅀ','ㅁ','ㅂ','ㅄ','ㅅ','ㅆ','ㅇ','ㅈ','ㅊ','ㅋ','ㅌ','ㅍ','ㅎ'];
      const compoundV = {'ㅗㅏ':'ㅘ','ㅗㅐ':'ㅙ','ㅗㅣ':'ㅚ','ㅜㅓ':'ㅝ','ㅜㅔ':'ㅞ','ㅜㅣ':'ㅟ','ㅡㅣ':'ㅢ','ㅏㅣ':'ㅐ','ㅑㅣ':'ㅒ','ㅓㅣ':'ㅔ','ㅕㅣ':'ㅖ'};
      const compoundT = {'ㄱㅅ':'ㄳ','ㄴㅈ':'ㄵ','ㄴㅎ':'ㄶ','ㄹㄱ':'ㄺ','ㄹㅁ':'ㄻ','ㄹㅂ':'ㄼ','ㄹㅅ':'ㄽ','ㄹㅌ':'ㄾ','ㄹㅍ':'ㄿ','ㄹㅎ':'ㅀ','ㅂㅅ':'ㅄ'};
      let current = 0, correct = 0, wrong = 0, shifted = false, answered = false, rawJamo = '';

      function isConsonant(ch){ return L.includes(ch); }
      function isVowel(ch){ return V.includes(ch); }
      function syllable(lead, vowel, tail='') {
        return String.fromCharCode(0xAC00 + (L.indexOf(lead)*21 + V.indexOf(vowel))*28 + T.indexOf(tail));
      }
      const splitVowel = {'ㅘ':'ㅗㅏ','ㅙ':'ㅗㅐ','ㅚ':'ㅗㅣ','ㅝ':'ㅜㅓ','ㅞ':'ㅜㅔ','ㅟ':'ㅜㅣ','ㅢ':'ㅡㅣ','ㅐ':'ㅏㅣ','ㅒ':'ㅑㅣ','ㅔ':'ㅓㅣ','ㅖ':'ㅕㅣ'};
      const splitTail = {'ㄳ':'ㄱㅅ','ㄵ':'ㄴㅈ','ㄶ':'ㄴㅎ','ㄺ':'ㄹㄱ','ㄻ':'ㄹㅁ','ㄼ':'ㄹㅂ','ㄽ':'ㄹㅅ','ㄾ':'ㄹㅌ','ㄿ':'ㄹㅍ','ㅀ':'ㄹㅎ','ㅄ':'ㅂㅅ'};
      // Keep an editable stream of compatibility jamo so Backspace works inside a syllable.
      function decomposeHangul(text) {
        let result='';
        for(const ch of text) {
          const code=ch.charCodeAt(0);
          if(code>=0xAC00 && code<=0xD7A3) {
            const offset=code-0xAC00;
            const lead=L[Math.floor(offset/588)];
            const vowel=V[Math.floor((offset%588)/28)];
            const tail=T[offset%28];
            result+=lead+(splitVowel[vowel]||vowel)+(tail?(splitTail[tail]||tail):'');
          } else result+=ch;
        }
        return result;
      }
      // Compose Korean jamo into syllables while moving a consonant to the next syllable when followed by a vowel.
      function composeHangul(raw) {
        const out=[]; let i=0;
        while(i<raw.length) {
          const lead=raw[i];
          if(!isConsonant(lead) || !isVowel(raw[i+1])) { out.push(lead); i++; continue; }
          i++;
          let vowel=raw[i++];
          if(compoundV[vowel+raw[i]]) { vowel=compoundV[vowel+raw[i]]; i++; }
          let tail='';
          if(isConsonant(raw[i])) {
            const first=raw[i], second=raw[i+1];
            if(isVowel(second)) {
              // The consonant begins the next syllable.
            } else if(compoundT[first+second] && !isVowel(raw[i+2])) {
              tail=compoundT[first+second]; i+=2;
            } else if(T.includes(first)) { tail=first; i++; }
          }
          out.push(syllable(lead,vowel,tail));
        }
        return out.join('');
      }
      function syncInput(){
        const composed=composeHangul(rawJamo);
        input.value=composed;
        checkButton.disabled=!composed.trim()||answered;
      }
      function renderKeyboard(){
        keyboard.replaceChildren();
        keyboardRows.forEach(row=>{
          const rowElement=document.createElement('div');
          rowElement.className='hangul-row';
          rowElement.style.setProperty('--key-count', row.length);
          row.forEach(letter=>{
            const shown=shifted?(shiftMap[letter]||letter):letter;
            const button=document.createElement('button');
            button.type='button'; button.className='hangul-key'+(shifted&&shiftMap[letter]?' shifted':'');
            button.textContent=shown; button.setAttribute('aria-label',shown);
            button.addEventListener('click',()=>{
              if(answered)return;
              rawJamo+=shown; syncInput();
              // Shift is one-shot: it resets after the next key is pressed.
              if(shifted){shifted=false;renderKeyboard();updateShiftButton();}
            });
            rowElement.appendChild(button);
          });
          keyboard.appendChild(rowElement);
        });
      }
      function updateShiftButton(){
        const button=document.getElementById('shiftKey');
        button.classList.toggle('active',shifted); button.setAttribute('aria-pressed',String(shifted));
      }
      function update(){
        document.getElementById('writingCount').textContent=`${current+1} / ${questions.length}`;
        document.getElementById('correctCount').textContent=`✓ ${correct}`;
        document.getElementById('wrongCount').textContent=`✕ ${wrong}`;
        const active=current+1;
        document.querySelectorAll('#writingProgress span').forEach((el,i)=>el.classList.toggle('active',i<active));
      }
      function checkAnswer(){
        if(answered||!input.value.trim())return;
        answered=true;
        if(input.value.trim().normalize('NFC')===questions[current].ko.trim().normalize('NFC')){
          correct++; message.textContent='✓ To‘g‘ri! Barakalla!'; 
        }else{
          wrong++; message.textContent=`✕ To‘g‘ri javob: ${questions[current].ko}`; 
        }
        checkButton.textContent=current===questions.length-1?'Natijani ko‘rish':'Keyingi →'; checkButton.disabled=false; update();
      }
      function nextQuestion(){
        if(current===questions.length-1){
          document.getElementById('writingPrompt').textContent='Mashq tugadi!';
          input.hidden=true;checkButton.hidden=true;keyboard.hidden=true;document.querySelector('.hangul-controls').hidden=true;
          message.textContent=`Natija: ${correct} ta to‘g‘ri, ${wrong} ta noto‘g‘ri.`; message.style.color='';
          if(roundScoped){fetch(roundCompletionUrl,{method:'POST',headers:{'X-Requested-With':'XMLHttpRequest'}}).then(r=>r.json()).then(d=>{if(d.xp_awarded)message.textContent+=' 🎉 Barcha 4 mashq tugadi: +10 XP!';else if(d.round_completed)message.textContent+=' Round yakunlandi.';}).catch(()=>{});}
          return;
        }
        current++;answered=false;rawJamo='';input.value='';input.hidden=false;checkButton.hidden=false;checkButton.disabled=true;checkButton.textContent='Tekshirish';
        keyboard.hidden=false;document.querySelector('.hangul-controls').hidden=false;
        shifted=false;renderKeyboard();updateShiftButton();message.textContent='';message.style.color='';
        document.getElementById('writingPrompt').textContent=questions[current].uz;update();
      }
      checkButton.addEventListener('click',()=>answered?nextQuestion():checkAnswer());
      // Allow users to type Korean with their physical keyboard too.
      input.addEventListener('input',()=>{rawJamo=decomposeHangul(input.value);checkButton.disabled=!input.value.trim()||answered;});
      document.getElementById('shiftKey').addEventListener('click',()=>{if(answered)return;shifted=!shifted;renderKeyboard();updateShiftButton();});
      document.getElementById('clearKey').addEventListener('click',()=>{if(answered)return;rawJamo='';input.value='';input.focus();checkButton.disabled=true;message.textContent='';});
      document.getElementById('deleteKey').addEventListener('click',()=>{if(answered)return;rawJamo=rawJamo.slice(0,-1);syncInput();input.focus();});
      renderKeyboard();updateShiftButton();update();
    })();
    </script>'''
    return page('Yozib o‘rganish', body, questions=questions)

MATCHING_DRIVE_FILE_ID = '1rz0wNM3ZAxHSWQ9LrN14xQONWjwJDuNp'
# Keep Google Drive vocabulary in server memory for 5 minutes. This avoids a
# network request every time someone opens the matching game.
MATCHING_CACHE_TTL = 300
_matching_cache = {'pairs': None, 'expires_at': 0.0}
_matching_cache_lock = threading.Lock()
_matching_refresh_thread = None
_matching_refresh_error = None
MATCHING_CACHE_PATH = os.path.join(APP_DIR, 'matching_words_cache.json')

def _matching_cached_pairs():
    """Return memory/disk cache without waiting for a network request."""
    now = time.monotonic()
    cached = _matching_cache['pairs']
    if cached is not None:
        if now >= _matching_cache['expires_at']:
            _start_matching_refresh()
        return cached
    try:
        with open(MATCHING_CACHE_PATH, 'r', encoding='utf-8') as cache_file:
            disk_pairs = json.load(cache_file)
        if isinstance(disk_pairs, list) and all(
            isinstance(pair, dict) and isinstance(pair.get('ko'), str) and isinstance(pair.get('uz'), str)
            for pair in disk_pairs
        ) and disk_pairs:
            _matching_cache['pairs'] = disk_pairs
            _matching_cache['expires_at'] = 0.0
            _start_matching_refresh()
            return disk_pairs
    except (OSError, ValueError, TypeError):
        pass
    return None

def _start_matching_refresh():
    """Refresh Drive data in the background so page requests stay responsive."""
    global _matching_refresh_thread, _matching_refresh_error
    if _matching_refresh_thread is not None and _matching_refresh_thread.is_alive():
        return
    _matching_refresh_error = None
    _matching_refresh_thread = threading.Thread(
        target=_refresh_matching_cache, name='hangeul-matching-refresh', daemon=True
    )
    _matching_refresh_thread.start()

def _refresh_matching_cache():
    global _matching_refresh_error
    try:
        _matching_pairs_from_drive(force_refresh=True)
        _matching_refresh_error = None
    except Exception as exc:
        # Keep the last good cache if Drive is temporarily unavailable.
        _matching_refresh_error = str(exc)
        print(f'Matching cache refresh failed: {exc}')


def _matching_drive_bytes():
    """Fetch the matching activity's source file from the exact Google Drive link."""
    errors = []
    # First try the user's existing Google Drive OAuth connection.
    try:
        service = _drive_service()
        if service is not None:
            content = service.files().get_media(fileId=MATCHING_DRIVE_FILE_ID).execute()
            if isinstance(content, bytes) and content:
                return content
            if isinstance(content, str) and content:
                return content.encode('utf-8')
    except Exception as exc:
        errors.append(str(exc))

    # Public-link fallback. The Drive file must be shared as "Anyone with the link".
    url = f'https://drive.google.com/uc?export=download&id={MATCHING_DRIVE_FILE_ID}'
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 HANGEUL'})
        with urllib.request.urlopen(req, timeout=15) as response:
            content = response.read()
        if content and not content.lstrip().startswith(b'<!DOCTYPE html') and b'<html' not in content[:300].lower():
            return content
        errors.append('Google Drive JSON fayl o‘rniga HTML sahifa qaytardi; fayl ochiqligini tekshiring.')
    except Exception as exc:
        errors.append(str(exc))
    raise RuntimeError('Google Drive faylini o‘qib bo‘lmadi. Havola ruxsatini “Anyone with the link / Havolaga ega har kim” qiling yoki Google Drive OAuth ulanishini tekshiring. ' + ' | '.join(errors))


def _matching_pairs_from_drive(force_refresh=False):
    """Serve cached Drive vocabulary immediately; refresh expired cache in background. SQLite is never used here."""
    now = time.monotonic()
    cached = _matching_cache['pairs']
    if not force_refresh and cached is not None:
        if now < _matching_cache['expires_at']:
            return cached
        _start_matching_refresh()
        return cached

    # On a server restart, restore the last successful snapshot before contacting Drive.
    if not force_refresh and cached is None:
        try:
            with open(MATCHING_CACHE_PATH, 'r', encoding='utf-8') as cache_file:
                disk_pairs = json.load(cache_file)
            if isinstance(disk_pairs, list) and all(
                isinstance(pair, dict) and isinstance(pair.get('ko'), str) and isinstance(pair.get('uz'), str)
                for pair in disk_pairs
            ) and disk_pairs:
                _matching_cache['pairs'] = disk_pairs
                _matching_cache['expires_at'] = 0.0  # Serve immediately and revalidate in background.
                _start_matching_refresh()
                return disk_pairs
        except (OSError, ValueError, TypeError):
            pass

    # Only one request refreshes the cache at a time if several visitors arrive together.
    with _matching_cache_lock:
        now = time.monotonic()
        if not force_refresh and _matching_cache['pairs'] is not None and now < _matching_cache['expires_at']:
            return _matching_cache['pairs']

        raw = _matching_drive_bytes().decode('utf-8-sig')
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            # Also accept a CSV file saved in Drive, if it has Korean/Uzbek columns.
            try:
                reader = csv.DictReader(io.StringIO(raw))
                rows = list(reader)
                payload = rows if rows and reader.fieldnames else None
            except Exception:
                payload = None
            if payload is None:
                raise RuntimeError('Google Drive fayli JSON yoki koreyscha/o‘zbekcha ustunlari bor CSV bo‘lishi kerak.')

        if isinstance(payload, dict):
            rows = None
            for key in ('words', 'data', 'vocabulary', 'items', 'results'):
                if isinstance(payload.get(key), list):
                    rows = payload[key]
                    break
            if rows is None:
                rows = [{'korean': key, 'uzbek': value} for key, value in payload.items() if isinstance(value, str)]
        elif isinstance(payload, list):
            rows = payload
        else:
            rows = []

        ko_keys = ('korean', 'ko', 'kor', 'word', 'korean_word', '한국어')
        uz_keys = ('uzbek', 'uz', 'translation', 'meaning', 'translated', 'o‘zbekcha', 'uzbek_translation')
        pairs, seen_ko, seen_uz = [], set(), set()
        for row in rows:
            if not isinstance(row, dict):
                continue
            normalized = {str(k).strip().casefold(): v for k, v in row.items()}
            ko = next((normalized[k] for k in ko_keys if k.casefold() in normalized), None)
            uz = next((normalized[k] for k in uz_keys if k.casefold() in normalized), None)
            if not isinstance(ko, str) or not isinstance(uz, str):
                continue
            ko, uz = ko.strip(), uz.strip()
            if not ko or not uz or ko.casefold() in seen_ko or uz.casefold() in seen_uz:
                continue
            pairs.append({'ko': ko, 'uz': uz})
            seen_ko.add(ko.casefold()); seen_uz.add(uz.casefold())

        # An empty parse is not a usable cache: report it as a load error so
        # /matching/status cannot claim the game is ready without any words.
        if not pairs:
            raise RuntimeError('Google Drive faylidan koreyscha–o‘zbekcha juftlik topilmadi. JSON/CSV formatini va ustun nomlarini tekshiring.')

        # Cache only a successful, usable parse. Keep Drive as the source of truth.
        _matching_cache['pairs'] = pairs
        _matching_cache['expires_at'] = time.monotonic() + MATCHING_CACHE_TTL
        try:
            temporary_path = MATCHING_CACHE_PATH + '.tmp'
            with open(temporary_path, 'w', encoding='utf-8') as cache_file:
                json.dump(pairs, cache_file, ensure_ascii=False)
            os.replace(temporary_path, MATCHING_CACHE_PATH)
        except OSError as exc:
            print(f'Matching cache snapshot could not be saved: {exc}')
        return pairs


@app.route('/matching/status')
def matching_status():
    """Lightweight status endpoint used while Drive vocabulary loads in the background."""
    pairs = _matching_cached_pairs()
    if pairs is not None:
        return Response(json.dumps({'status': 'ready'}), mimetype='application/json')
    thread = _matching_refresh_thread
    if thread is not None and thread.is_alive():
        return Response(json.dumps({'status': 'loading'}), status=202, mimetype='application/json')
    if _matching_refresh_error:
        return Response(json.dumps({'status': 'error', 'message': _matching_refresh_error}), status=502, mimetype='application/json')
    _start_matching_refresh()
    return Response(json.dumps({'status': 'loading'}), status=202, mimetype='application/json')


@app.route('/matching')
def matching():
    """Use saved app vocabulary immediately, and merge in the Drive vocabulary when available."""
    active_day = session.get('active_topic_day_id')
    active_round = session.get('active_topic_round')
    with db() as conn:
        if active_day and active_round:
            saved_rows = conn.execute("SELECT korean AS ko, uzbek AS uz FROM topic_words WHERE day_id=? AND round_number=? AND TRIM(COALESCE(korean,''))<>'' AND TRIM(COALESCE(uzbek,''))<>''", (active_day, active_round)).fetchall()
        else:
            saved_rows = conn.execute(
                "SELECT korean AS ko, uzbek AS uz FROM words "
                "WHERE TRIM(COALESCE(korean,''))<>'' AND TRIM(COALESCE(uzbek,''))<>'' "
                "UNION ALL "
                "SELECT korean AS ko, uzbek AS uz FROM topic_words "
                "WHERE TRIM(COALESCE(korean,''))<>'' AND TRIM(COALESCE(uzbek,''))<>''"
            ).fetchall()
    saved_pairs = [{'ko': r['ko'].strip(), 'uz': r['uz'].strip()} for r in saved_rows]
    drive_pairs = [] if (active_day and active_round) else _matching_cached_pairs()
    # A database-backed exercise must not be blocked by a slow/unavailable Drive connection.
    pairs = []
    seen_pairs = set()
    for pair in saved_pairs + (drive_pairs or []):
        ko = str(pair.get('ko', '')).strip()
        uz = str(pair.get('uz', '')).strip()
        if ko and uz and (ko, uz) not in seen_pairs:
            seen_pairs.add((ko, uz))
            pairs.append({'ko': ko, 'uz': uz})
    if not pairs and drive_pairs is None:
        _start_matching_refresh()
        body = r"""<section class='hero practice-shell matching-loading' style='max-width:680px;margin:28px auto;padding:36px 20px;text-align:center;border-radius:20px;background:var(--panel);border:1px solid var(--border)'><div style='font-size:36px;margin-bottom:12px'>🧩</div><h1>Aqli moslash</h1><p id='matching-loading-text' class='muted'>So‘zlar yuklanmoqda…</p><div style='height:5px;max-width:260px;margin:20px auto;background:var(--border);border-radius:9px;overflow:hidden'><div style='height:100%;width:40%;background:var(--accent);border-radius:9px;animation:matchingLoad 1.2s ease-in-out infinite alternate'></div></div><a class='btn primary' href='{{ url_for('vocabulary') }}'>Bo‘limlarga qaytish</a></section><style>@keyframes matchingLoad{from{transform:translateX(-70%)}to{transform:translateX(220%)}}</style><script>(()=>{const statusUrl={{ url_for('matching_status')|tojson }};const target={{ url_for('matching')|tojson }};const text=document.getElementById('matching-loading-text');let attempts=0;async function poll(){attempts++;try{const r=await fetch(statusUrl,{cache:'no-store'});const d=await r.json();if(d.status==='ready'){location.replace(target+'?ready=1');return}if(d.status==='error'){text.textContent='Google Drive’dan so‘zlarni yuklab bo‘lmadi. Ulanish yoki fayl ruxsatlarini tekshiring.';return}}catch(e){}if(attempts>45){text.textContent='Yuklanish cho‘zildi. Internet yoki Google Drive ulanishini tekshirib, sahifani qayta oching.';return}setTimeout(poll,700)}poll()})();</script>"""
        return page('Aqli moslash', body)
    if not pairs:
        body = """<section class='hero practice-shell'><h1>Aqli moslash</h1><p class='muted'>Hozircha mashq uchun so‘z topilmadi. Admin bo‘limiga yoki Mavzular ichidagi DAY/ROUNDga kamida 3 ta koreyscha–o‘zbekcha juftlik kiriting.</p><a class='btn primary' href='{{ url_for('vocabulary') }}'>Bo‘limlarga qaytish</a></section>"""
        return page('Aqli moslash', body)
    # Har bir matching bosqichida 3–8 ta so‘z bo‘ladi.
    # 3 tadan kam juftlik bo‘lsa, mashqni boshlash uchun ma’lumot yetarli emas.
    if len(pairs) < 3:
        body = """<section class='hero practice-shell'><h1>Aqli moslash</h1><p class='muted'>Taqqoslash mashqini boshlash uchun kamida 3 ta koreyscha–o‘zbekcha so‘z juftligi kerak. Admin bo‘limiga yoki Mavzular ichidagi DAY/ROUNDga yana so‘z qo‘shing.</p><a class='btn primary' href='{{ url_for('vocabulary') }}'>Bo‘limlarga qaytish</a></section>"""
        return page('Aqli moslash', body)

    # 8 tadan oshirmaymiz; guruhlarni teng taqsimlab, har birida kamida 3 ta bo‘ladi.
    wave_count = (len(pairs) + 7) // 8
    waves = [[] for _ in range(wave_count)]
    for i, pair in enumerate(pairs):
        waves[min(i * wave_count // len(pairs), wave_count - 1)].append(pair)
    body = r'''<style>
.matching-page{max-width:680px;margin:auto;padding:12px;color:var(--text)}.matching-head{display:flex;justify-content:center;align-items:center;margin-bottom:22px}.matching-back{width:42px;height:42px;display:grid;place-items:center;border-radius:13px;background:var(--panel);border:1px solid var(--border);color:var(--text);font-size:27px;text-decoration:none}.matching-title{text-align:center;font-size:clamp(21px,4vw,27px);font-weight:800}.matching-progress{display:flex;gap:7px;margin:0 0 22px}.matching-segment{height:6px;flex:1;border-radius:10px;background:#d5dae4}.matching-segment.active{background:#3478f6}.matching-status{display:flex;justify-content:space-between;align-items:center;gap:12px;margin-bottom:20px;font-weight:700;font-size:14px}.matching-results{display:flex;gap:14px;white-space:nowrap}.matching-correct{color:#16a34a}.matching-wrong{color:#e05275}.matching-columns{display:grid;grid-template-columns:1fr 1fr;gap:12px}.matching-label{text-align:center;color:var(--muted);font-size:13px;margin-bottom:11px}.matching-list{display:flex;flex-direction:column;gap:11px}.matching-card{width:100%;min-height:62px;padding:15px 7px;border:2px solid var(--border);border-radius:15px;background:var(--panel);color:var(--text);font-size:17px;font-weight:700;cursor:pointer;overflow-wrap:anywhere}.matching-card.selected{border-color:#3478f6;background:#eaf1ff;color:#245fce}.matching-card.matched{border-color:#a9e5bd;background:#effcf3;color:#16803d}.matching-card.error{border-color:#e05275;background:#fff0f3;color:#bd3157}.matching-message{text-align:center;color:var(--muted);font-size:14px;min-height:22px;margin-top:20px}.matching-next{display:block;width:100%;margin-top:18px;padding:14px;border:0;border-radius:13px;background:#3478f6;color:white;font-size:16px;font-weight:700;cursor:pointer}.matching-next[hidden]{display:none}.matching-finish{grid-column:1/-1;text-align:center;padding:clamp(28px,7vw,54px) 20px;border-radius:25px;background:radial-gradient(ellipse at 50% 0%,#1b376b 0%,#101d38 48%,#0b1428 100%);border:1px solid rgba(100,151,255,.25);box-shadow:0 22px 60px rgba(3,10,28,.35);color:#f8fafc;animation:finishIn .45s ease-out}.finish-check{width:76px;height:76px;margin:0 auto 22px;border-radius:50%;display:grid;place-items:center;background:rgba(34,197,94,.13);border:1px solid rgba(74,222,128,.45);color:#4ade80;font-size:39px;box-shadow:0 0 35px rgba(34,197,94,.12)}.matching-finish h2{font-size:clamp(30px,6vw,42px);letter-spacing:-1px;margin:0 0 12px;color:#fff}.finish-subtitle{color:#b9c8e5;margin:0 0 26px}.finish-stats{display:grid;grid-template-columns:1fr 1fr;gap:12px;max-width:390px;margin:0 auto 25px}.finish-stat{padding:17px 12px;border-radius:16px;background:rgba(255,255,255,.055);border:1px solid rgba(255,255,255,.1)}.finish-stat small{display:block;color:#aab9d5;font-size:13px;margin-bottom:7px}.finish-stat strong{font-size:27px}.finish-stat.good strong{color:#4ade80}.finish-stat.bad strong{color:#fb7185}.matching-finish .matching-next{max-width:340px;margin:0 auto;background:linear-gradient(100deg,#3478f6,#5b8dff);box-shadow:0 0 24px rgba(52,120,246,.4);transition:transform .2s,box-shadow .2s}.matching-finish .matching-next:hover{transform:translateY(-2px);box-shadow:0 0 34px rgba(52,120,246,.58)}.finish-congrats{font-size:20px;font-weight:800;color:#dbeafe;margin:26px 0 0}.finish-caption{font-size:13px;color:#91a3c4;margin-top:8px}@keyframes finishIn{from{opacity:0;transform:translateY(10px) scale(.985)}to{opacity:1;transform:translateY(0) scale(1)}}@media(max-width:420px){.matching-card{font-size:15px;min-height:58px;padding:13px 5px}.matching-status{font-size:13px}.matching-finish{padding:30px 14px}.finish-stat{padding:14px 8px}}
</style><main class="matching-page"><div class="practice-shell"><header class="matching-head"><div class="matching-title">Aqli moslash</div><div></div></header><div class="matching-progress">{% for n in range(waves|length) %}<div class="matching-segment {{ 'active' if n < 1 else '' }}"></div>{% endfor %}</div><div class="matching-status"><span id="stage">To‘lqin 1 / {{ waves|length }}</span><div class="matching-results"><span class="matching-correct">✓ <span id="correct">0</span></span><span class="matching-wrong">✕ <span id="wrong">0</span></span></div></div><div class="matching-columns" id="columns"><section><div class="matching-label">Koreyscha</div><div class="matching-list" id="ko"></div></section><section><div class="matching-label">O‘zbekcha</div><div class="matching-list" id="uz"></div></section></div><div class="matching-message" id="message">Koreyscha so‘zni, keyin tarjimasini tanlang.</div><button class="matching-next" id="next" hidden>Keyingi to‘lqin</button></div></main><script>
(()=>{const waves={{ waves|tojson }};const roundCompletionUrl={{ round_completion_url|tojson }};const roundScoped={{ round_scoped|tojson }};let wi=0,correct=0,wrong=0,sk=null,su=null,matched=new Set(),locked=false;const $=x=>document.getElementById(x);const shuffle=a=>{a=[...a];for(let i=a.length-1;i>0;i--){let j=Math.floor(Math.random()*(i+1));[a[i],a[j]]=[a[j],a[i]]}return a};function card(text,i,type){let b=document.createElement('button');b.type='button';b.className='matching-card';b.textContent=text;b.dataset.i=i;b.onclick=()=>choose(b);return b}function render(){sk=su=null;matched=new Set();locked=false;document.querySelectorAll('.matching-segment').forEach((segment,i)=>segment.classList.toggle('active',i<=wi));$('stage').textContent=`To‘lqin ${wi+1} / ${waves.length}`;$('correct').textContent=correct;$('wrong').textContent=wrong;$('message').textContent='Koreyscha so‘zni, keyin tarjimasini tanlang.';$('ko').replaceChildren();$('uz').replaceChildren();waves[wi].forEach((p,i)=>$('ko').append(card(p.ko,i,'ko')));shuffle(waves[wi].map((p,i)=>({...p,i}))).forEach(p=>$('uz').append(card(p.uz,p.i,'uz')));$('next').hidden=true;$('next').textContent=wi===waves.length-1?'Natijani ko‘rish':'Keyingi to‘lqin'}function choose(b){if(locked||b.classList.contains('matched'))return;let type=b.parentElement.id;b.parentElement.querySelectorAll('button').forEach(x=>x.classList.remove('selected'));b.classList.add('selected');if(type==='ko')sk=b;else su=b;if(sk&&su)check()}function check(){if(sk.dataset.i===su.dataset.i){sk.classList.remove('selected');su.classList.remove('selected');sk.classList.add('matched');su.classList.add('matched');matched.add(+sk.dataset.i);correct++;$('message').textContent='To‘g‘ri! 🎉';sk=su=null;if(matched.size===waves[wi].length){$('message').textContent='Barcha juftliklar topildi!';$('next').hidden=false}}else{wrong++;locked=true;sk.classList.add('error');su.classList.add('error');$('message').textContent='Mos kelmadi, yana urinib ko‘ring.';setTimeout(()=>{sk?.classList.remove('selected','error');su?.classList.remove('selected','error');sk=su=null;locked=false},500)}$('correct').textContent=correct;$('wrong').textContent=wrong}$('next').onclick=()=>{if(matched.size!==waves[wi].length)return;if(wi<waves.length-1){wi++;render()}else{$('.matching-progress').innerHTML=Array.from({length:waves.length},()=>'<div class="matching-segment active"></div>').join('');$('stage').textContent=`To‘lqin ${waves.length} / ${waves.length}`;$('correct').textContent=correct;$('wrong').textContent=wrong;$('columns').innerHTML=`<section class="matching-finish"><div class="finish-check">✓</div><h2>Mashq tugadi!</h2><p class="finish-subtitle">Barcha juftliklarni yakunladingiz.</p><div class="finish-stats"><div class="finish-stat good"><small>To‘g‘ri</small><strong>${correct}</strong></div><div class="finish-stat bad"><small>Xato urinish</small><strong>${wrong}</strong></div></div><button class="matching-next" onclick="location.reload()">↻ &nbsp; Qayta boshlash</button><p class="finish-congrats">Barakalla!</p><div id="matching-xp-message" class="finish-caption">Hangeul · Koreys tilini bosqichma-bosqich o‘rganamiz</div></section>`;$('message').textContent='';$('next').hidden=true;if(roundScoped){fetch(roundCompletionUrl,{method:'POST',headers:{'X-Requested-With':'XMLHttpRequest'}}).then(r=>r.json()).then(d=>{if(d.xp_awarded)$('matching-xp-message').textContent='Tabriklaymiz! Barcha 4 mashq tugadi: +10 XP 🎉';else if(d.round_completed)$('matching-xp-message').textContent='Round yakunlandi.';else $('matching-xp-message').textContent=`Mashq yakunlandi: ${d.completed}/4 mashq bajarildi.`}).catch(()=>{});}}};render()})();</script>'''
    return page('Aqli moslash', body, waves=waves,
                round_completion_url=url_for('complete_round_activity', activity='matching'),
                round_scoped=bool(active_day and active_round))

@app.route('/marathon')
def marathon():
    # Flashcards use both legacy vocabulary and words saved under Folder -> DAY -> ROUND.
    # Keep the same fields expected by the existing template for both sources.
    with db() as conn:
        active_day = session.get('active_topic_day_id')
        active_round = session.get('active_topic_round')
        if active_day and active_round:
            legacy_words = []
            topic_words = conn.execute(
                "SELECT 'topic:' || tw.id AS id,tw.korean,tw.uzbek,tw.pronunciation,"
                "'Mavzular' AS category,tw.example,'' AS image_url,'Ko‘rsatilmagan' AS part_of_speech "
                'FROM topic_words AS tw WHERE tw.day_id=? AND tw.round_number=? ORDER BY tw.position',
                (active_day, active_round)
            ).fetchall()
        else:
            legacy_words = conn.execute(
                "SELECT 'word:' || id AS id,korean,uzbek,pronunciation,category,example,image_url,part_of_speech "
                'FROM words ORDER BY id'
            ).fetchall()
            topic_words = conn.execute(
                "SELECT 'topic:' || tw.id AS id,tw.korean,tw.uzbek,tw.pronunciation,"
                "'Mavzular' AS category,tw.example,'' AS image_url,'Ko‘rsatilmagan' AS part_of_speech "
                'FROM topic_words AS tw '
                'JOIN topic_days AS td ON td.id=tw.day_id '
                'JOIN topic_folders AS tf ON tf.id=td.folder_id '
                'ORDER BY tf.id,td.day_number,tw.round_number,tw.position'
            ).fetchall()

    # Avoid showing the same Korean/Uzbek pair twice if it exists in both sources.
    words = []
    seen_flashcard_pairs = set()
    for item in list(legacy_words) + list(topic_words):
        korean = (item['korean'] or '').strip()
        uzbek = (item['uzbek'] or '').strip()
        if not korean or not uzbek:
            continue
        pair = (korean, uzbek)
        if pair in seen_flashcard_pairs:
            continue
        seen_flashcard_pairs.add(pair)
        words.append(item)

    if not words:
        body = '''<section class="hero"><div class="eyebrow">FLASH CARD</div><h1>Hozircha so‘z yo‘q</h1><p class="muted">Marafonni boshlash uchun admin paneldan avval so‘z qo‘shing.</p><a class="btn primary" href="{{ url_for('admin') if session.get('admin_logged_in') else url_for('login') }}">So‘z qo‘shish</a></section>'''
        return page('Flash card', body)

    try:
        index = int(request.args.get('i', '0'))
    except (TypeError, ValueError):
        index = 0
    index = max(0, min(index, len(words) - 1))
    word = words[index]
    previous_index = max(0, index - 1)
    next_index = min(len(words) - 1, index + 1)
    # Twenty-five visual segments, with progress reflecting the current position.
    progress_segments = min(25, max(1, (index * 25 // len(words)) + 1))
    body = '''
    <style>
      .marathon-page{max-width:920px;margin:0 auto;color:var(--text)}
      .marathon-shell{background:var(--panel);border:1px solid var(--border);border-radius:28px;padding:20px;box-shadow:0 18px 50px var(--shadow)}
      .marathon-head{display:grid;grid-template-columns:1fr;align-items:center;gap:8px;border-bottom:1px solid #e8edf6;padding:0 0 16px}
      .marathon-head h1{font-size:clamp(20px,4vw,27px);text-align:center;margin:0;color:var(--text)}
            .marathon-meta{padding:20px 4px 18px}.marathon-meta h2{margin:0 0 14px;font-size:23px;color:var(--text)}
      .marathon-progress-row{display:flex;align-items:center;gap:14px}.marathon-progress{display:grid;grid-template-columns:repeat(25,minmax(0,1fr));gap:3px;flex:1}
      .marathon-segment{height:9px;border-radius:99px;background:#dfe5ef}.marathon-segment.active{background:#2874f0}
      .marathon-count{font-size:16px;white-space:nowrap;color:var(--muted);font-weight:650}
      .flashcard{background:var(--surface);border:1px solid var(--border);border-radius:24px;padding:clamp(20px,5vw,38px);box-shadow:0 12px 32px var(--shadow)}
      .word-badge{display:inline-block;background:#e3efff;color:#1763d8;border-radius:999px;padding:6px 14px;font-weight:750;font-size:14px}
      .flash-word{text-align:center;padding:25px 0 28px}.flash-word .korean{font-size:clamp(48px,10vw,76px);font-weight:850;letter-spacing:1px;line-height:1.2;color:var(--text);overflow-wrap:anywhere}
      .flash-word .uzbek{font-size:clamp(23px,5vw,32px);color:var(--muted);margin-top:10px}
      .flash-example{background:var(--panel2);border-left:5px solid var(--accent);border-radius:4px 14px 14px 4px;padding:17px 18px;margin:6px 0 20px;color:var(--text);overflow-wrap:anywhere}
      .example-ko{font-size:clamp(18px,4vw,23px);font-weight:650}.example-uz{font-size:16px;color:var(--muted);margin-top:8px}
      .highlight-word{color:#2874f0;font-weight:800}.flash-actions{display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:10px}
      .flash-link{border:0;background:transparent;color:#1769e8;font:inherit;font-weight:750;cursor:pointer;padding:10px 12px}
      .flash-audio{display:inline-flex;align-items:center;justify-content:center;width:44px;height:44px;flex:0 0 44px;border:1px solid #d7e5fb;background:#f5f9ff;color:#1769e8;border-radius:50%;padding:0;font-size:22px;cursor:pointer}.flash-audio:hover{background:#e8f1ff}
      .marathon-footer{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-top:20px}
      .marathon-btn{border:1px solid #cbd5e5;border-radius:999px;background:var(--panel2);color:var(--text);font:inherit;font-weight:750;padding:13px 16px;cursor:pointer;text-align:center}
      .marathon-btn.next{background:linear-gradient(100deg,var(--purple),var(--accent));border-color:transparent;color:white}.marathon-btn:disabled{opacity:.45;cursor:not-allowed}
      .translation-form{display:none;margin-top:12px}.translation-form.open{display:block}.translation-form textarea{background:var(--input);color:var(--text);border-color:var(--border)}
      @media(max-width:520px){.marathon-shell{padding:12px;border-radius:20px}.marathon-head h1{font-size:19px}.marathon-progress-row{gap:9px}.marathon-progress{gap:2px}.marathon-segment{height:7px}.flashcard{border-radius:18px;padding:20px 16px}.marathon-footer{gap:9px}.marathon-btn{padding:12px 8px}}
    
/* Existing sections: exactly two columns, with vertical scrolling inside the list. */
.fibonacci-admin .admin-sections-panel{min-width:0;min-height:0}
.fibonacci-admin .admin-sections-panel .admin-section-list{
  display:grid!important;
  grid-template-columns:repeat(2,minmax(0,1fr))!important;
  grid-auto-flow:row!important;
  grid-auto-rows:max-content;
  gap:10px!important;
  height:auto!important;
  max-height:390px!important;
  min-height:0!important;
  overflow-y:auto!important;
  overflow-x:hidden!important;
  overscroll-behavior-y:contain;
  align-content:start!important;
  padding:2px 8px 8px 2px!important;
  scrollbar-gutter:stable;
  -webkit-overflow-scrolling:touch;
}
.fibonacci-admin .admin-sections-panel .admin-section-card{min-width:0!important;max-width:100%;align-self:start}
.fibonacci-admin .admin-sections-panel .admin-section-title{min-width:0}
.fibonacci-admin .admin-sections-panel .admin-section-title h3{overflow-wrap:anywhere;word-break:break-word}
.fibonacci-admin .admin-sections-panel .admin-section-list::-webkit-scrollbar{width:9px}
.fibonacci-admin .admin-sections-panel .admin-section-list::-webkit-scrollbar-thumb{background:rgba(85,115,180,.48);border-radius:10px}
.fibonacci-admin .admin-sections-panel .admin-section-list{scrollbar-color:rgba(85,115,180,.48) transparent;scrollbar-width:thin}


/* Panel o‘z enida qoladi; ichidagi har bir bo‘lim kartochkasi 60% toraytiriladi. */
.fibonacci-admin .admin-sections-panel {
    width: auto !important;
    max-width: none !important;
    min-width: 0 !important;
    box-sizing: border-box;
}
.fibonacci-admin .admin-sections-panel .admin-section-list {
    grid-template-columns: repeat(2, minmax(0, 1fr)) !important;
}
.fibonacci-admin .admin-sections-panel .admin-section-card {
    width: 40% !important;
    max-width: 40% !important;
    min-width: 0 !important;
    box-sizing: border-box;
}
</style>
    <main class="marathon-page"><section class="flash-hero"><div class="flash-hero-copy"><span class="flash-hero-icon" aria-hidden="true"><img src="/rasmlar/fleshcard-transparent.png" alt=""></span><div><h1>Flash card</h1><p>Yangi so‘zlarni kartochkalar orqali o‘rganing va eslab qoling!</p></div></div><div class="flash-hero-art"><span class="flash-sunset"></span><i class="flash-peak p1"></i><i class="flash-peak p2"></i><div class="flash-temple"><i></i><b></b><em></em></div><span class="flash-flower f1">✦</span><span class="flash-flower f2">✦</span><span class="flash-flower f3">✦</span></div></section><div class="marathon-shell">
      <header class="marathon-head">
        <h1>Flash card</h1>
      </header>
      <section class="marathon-meta">
        <div class="marathon-progress-row"><div class="marathon-progress" aria-label="Marafon progressi">{% for n in range(25) %}<span class="marathon-segment {{ 'active' if n < progress_segments else '' }}"></span>{% endfor %}</div><span class="marathon-count">{{ index + 1 }} / {{ words|length }}</span></div>
      </section>
      <article class="flashcard">
        {% set pos_icons = {'Ot': '🏷️', 'Fe’l': '🏃', 'Sifat': '🎨', 'Ravish': '⚡', 'Olmosh': '👤', 'Son': '🔢', 'Ko‘makchi': '🔗', 'Bog‘lovchi': '➕', 'Undov': '❗', 'Ibora': '💬', 'Boshqa': '📚', 'Ko‘rsatilmagan': '📌'} %}
        {% set pos = word.part_of_speech or 'Ko‘rsatilmagan' %}
        <span class="word-badge"><span aria-hidden="true">{{ pos_icons.get(pos, '📌') }}</span> {{ pos }}</span>
        <div class="flash-word"><div class="korean">{{ word.korean }}</div><div class="uzbek">{{ word.uzbek }}</div>
          {% if word.pronunciation %}<div style="margin-top:8px;color:#2874f0;font-size:14px">{{ word.pronunciation }}</div>{% endif %}
        </div>
        {% if word.example %}<section class="flash-example"><div class="example-ko">{{ word.example }}</div><div class="example-uz">So‘z tarjimasi: {{ word.uzbek }}</div></section>{% else %}<section class="flash-example"><div class="example-ko">Misol gap hozircha kiritilmagan.</div><div class="example-uz">Admin panel orqali misol gap qo‘shishingiz mumkin.</div></section>{% endif %}
        <div class="flash-actions"><button class="flash-link" type="button" onclick="document.getElementById('translationForm').classList.toggle('open')">✎ Boshqacha tarjima taklif qilish</button><button class="flash-audio" type="button" onclick="speakKorean(this.dataset.korean)" data-korean="{{ word.korean }}" aria-label="Koreyscha so‘zni eshitish" title="Ovozini eshitish">🔊</button></div>
        <form class="translation-form" id="translationForm" onsubmit="event.preventDefault();alert('Taklif matni tayyorlandi. Hozircha u bazaga yuborilmaydi.');this.reset();this.classList.remove('open')"><label for="suggestion" style="display:block;margin-bottom:6px;font-weight:700">Tarjima taklifingiz</label><textarea id="suggestion" rows="2" placeholder="Yaxshiroq tarjimani yozing…" required></textarea><button class="btn primary" type="submit" style="margin-top:8px">Taklifni ko‘rsatish</button></form>
      </article>
      <footer class="marathon-footer"><a class="marathon-btn" href="{{ url_for('marathon', i=previous_index) }}" {% if index == 0 %}aria-disabled="true" style="opacity:.45;pointer-events:none"{% endif %}>Oldingi</a>{% if session.get('active_topic_day_id') and session.get('active_topic_round') and index == words|length - 1 %}<form method="post" action="{{ url_for('complete_round_activity', activity='flashcard') }}"><button class="marathon-btn next" type="submit">Flashcardni yakunlash ✓</button></form>{% else %}<a class="marathon-btn next" href="{{ url_for('marathon', i=next_index) }}" {% if index == words|length - 1 %}aria-disabled="true" style="opacity:.45;pointer-events:none"{% endif %}>Keyingi →</a>{% endif %}</footer>
    </div></main>'''
    return page('Flash card', body, word=word, words=words, index=index,
                previous_index=previous_index, next_index=next_index,
                progress_segments=progress_segments)



@app.route('/marathon/quiz', methods=['GET', 'POST'])
def viktorina():
    import random
    direction = request.args.get('direction') or request.form.get('direction') or session.get('quiz_direction', 'uz-kr')
    if direction not in ('uz-kr', 'kr-uz'):
        direction = 'uz-kr'
    if direction != session.get('quiz_direction') or request.args.get('restart') == '1':
        session['quiz_direction'] = direction
        session.pop('quiz_order', None)
        session.pop('quiz_index', None)
        session.pop('quiz_history', None)
    with db() as conn:
        active_day = session.get('active_topic_day_id')
        active_round = session.get('active_topic_round')
        if active_day and active_round:
            rows = conn.execute("SELECT 'topic:' || id AS id,korean,uzbek,pronunciation FROM topic_words WHERE day_id=? AND round_number=? AND TRIM(COALESCE(korean,''))<>'' AND TRIM(COALESCE(uzbek,''))<>'' ORDER BY korean", (active_day, active_round)).fetchall()
        else:
            rows = conn.execute(
                "SELECT 'word:' || id AS id,korean,uzbek,pronunciation FROM words "
                "WHERE TRIM(COALESCE(korean,''))<>'' AND TRIM(COALESCE(uzbek,''))<>'' "
                "UNION ALL "
                "SELECT 'topic:' || id AS id,korean,uzbek,pronunciation FROM topic_words "
                "WHERE TRIM(COALESCE(korean,''))<>'' AND TRIM(COALESCE(uzbek,''))<>'' "
                "ORDER BY korean"
            ).fetchall()
    words, seen = [], set()
    for row in rows:
        key = (row['korean'].strip(), row['uzbek'].strip())
        if key not in seen:
            seen.add(key)
            words.append(dict(row))
    if not words:
        return page('Viktorina', '<section class="quiz-empty"><div class="quiz-empty-icon">🏆</div><h1>Viktorina</h1><p>Viktorinani boshlash uchun avval Admin orqali so‘z qo‘shing.</p><a class="btn primary" href="{{ url_for(\'topics\') }}">Mavzularni ko‘rish</a></section>')
    if 'quiz_order' not in session:
        random.shuffle(words)
        session['quiz_order'] = [w['id'] for w in words]
        session['quiz_index'] = 0
        session['quiz_history'] = []
    by_id = {w['id']: w for w in words}
    order = [wid for wid in session.get('quiz_order', []) if wid in by_id]
    if not order:
        return redirect(url_for('viktorina', restart=1, direction=direction))
    idx = int(session.get('quiz_index', 0))
    history = session.get('quiz_history', [])
    if request.method == 'POST' and idx < len(order):
        word = by_id[order[idx]]
        selected = request.form.get('answer', '').strip()
        correct_answer = word['korean'] if direction == 'uz-kr' else word['uzbek']
        history.append({'korean': word['korean'], 'meaning': word['uzbek'], 'prompt': word['uzbek'] if direction == 'uz-kr' else word['korean'], 'selected': selected, 'answer': correct_answer, 'correct': selected == correct_answer, 'direction': direction})
        session['quiz_history'] = history
        session['quiz_index'] = idx + 1
        session.modified = True
        return redirect(url_for('viktorina', direction=direction) + '#page-top')
    idx = int(session.get('quiz_index', 0))
    history = session.get('quiz_history', [])
    correct = sum(1 for item in history if item['correct'])
    total = len(order)
    direction_label = 'UZ → KR' if direction == 'uz-kr' else 'KR → UZ'
    if idx >= total:
        if session.get('active_topic_day_id') and session.get('active_topic_round'):
            quiz_completion = mark_round_activity(session['active_topic_day_id'], session['active_topic_round'], 'viktorina')
            if quiz_completion.get('xp_awarded'):
                flash(f'Barakalla! Round {session['active_topic_round']}dagi 4 ta mashq tugadi: +10 XP 🎉')
        body = '<main class="quiz-page"><section class="quiz-result"><div class="quiz-result-icon">🏆</div><span class="quiz-kicker">HANGEUL • NATIJA</span><h1>Viktorina yakunlandi!</h1><p class="quiz-result-subtitle">Ajoyib urinish! Natijalaringiz quyida.</p><div class="quiz-result-stats"><article><small>Jami savollar</small><strong>{{ total }}</strong></article><article class="correct"><small>To‘g‘ri</small><strong>{{ correct }}</strong></article><article class="incorrect"><small>Xato</small><strong>{{ total-correct }}</strong></article></div><div class="quiz-review"><h2>Javoblar</h2>{% for item in history %}<article class="quiz-review-item"><div><strong>{{ loop.index }}. {{ item.prompt }}</strong><small>To‘g‘ri javob: {{ item.answer }}</small>{% if not item.correct %}<small>Siz tanlagan: {{ item.selected or \'Javob berilmadi\' }}</small>{% endif %}</div><span class="{{ \'review-correct\' if item.correct else \'review-wrong\' }}">{{ \'✓ To‘g‘ri\' if item.correct else \'× Xato\' }}</span></article>{% endfor %}</div><div class="quiz-result-actions"><a class="btn primary" href="{{ url_for(\'viktorina\', restart=1, direction=direction) }}">↻ Qayta boshlash</a><a class="btn" href="{{ url_for(\'topics\') }}">Mavzular</a></div></section></main>'
        return page('Viktorina natijasi', body, total=total, correct=correct, history=history, direction=direction, direction_label=direction_label)
    word = by_id[order[idx]]
    prompt = word['uzbek'] if direction == 'uz-kr' else word['korean']
    answer_key = 'korean' if direction == 'uz-kr' else 'uzbek'
    options = [word[answer_key]]
    other = list({w[answer_key] for w in words if w[answer_key] != word[answer_key]})
    random.shuffle(other)
    options.extend(other[:3])
    random.shuffle(options)
    prompt_label = 'O‘zbekcha so‘zni koreys tiliga tarjima qiling' if direction == 'uz-kr' else 'Koreyscha so‘zni o‘zbek tiliga tarjima qiling'
    body = '<main class="quiz-page"><section class="quiz-shell"><div class="quiz-topline"><div class="quiz-count"><strong>{{ idx+1 }}</strong><span>/ {{ total }} savol</span></div><div class="quiz-score"><span class="score-good">✓ {{ correct }}</span><span class="score-bad">× {{ idx-correct }}</span></div><span class="quiz-direction">{{ direction_label }}</span></div><div class="quiz-progress" aria-label="Viktorina jarayoni">{% for n in range(total) %}<span class="{{ \'done\' if n < idx else (\'current\' if n == idx else \'\') }}"></span>{% endfor %}</div><div class="quiz-directions"><a class="{{ \'selected\' if direction == \'uz-kr\' else \'\' }}" href="{{ url_for(\'viktorina\', restart=1, direction=\'uz-kr\') }}">🇺🇿 O‘zbekcha → Koreyscha</a><a class="{{ \'selected\' if direction == \'kr-uz\' else \'\' }}" href="{{ url_for(\'viktorina\', restart=1, direction=\'kr-uz\') }}">🇰🇷 Koreyscha → O‘zbekcha</a></div><section class="quiz-question-card"><span class="quiz-question-label">{{ prompt_label }}</span><div class="quiz-prompt">{{ prompt }}</div><span class="quiz-hint">To‘g‘ri javobni tanlang</span>{% if direction == \'kr-uz\' %}<button class="quiz-speak" type="button" onclick="speakKorean(this.dataset.korean)" data-korean="{{ prompt }}" aria-label="Talaffuzni eshitish">🔊 <small>Talaffuz</small></button>{% endif %}</section><form class="quiz-options-form" method="post"><input type="hidden" name="direction" value="{{ direction }}"><div class="quiz-options">{% for option in options %}<label class="quiz-option"><input type="radio" name="answer" value="{{ option }}" required><span class="quiz-option-number">{{ loop.index }}</span><span class="quiz-option-text">{{ option }}</span><span class="quiz-option-check" aria-hidden="true"></span></label>{% endfor %}</div><button class="btn primary quiz-submit" type="submit">{{ \'Yakunlash\' if idx+1 >= total else \'Keyingi savol\' }} <span>→</span></button></form></section></main>'
    return page('Viktorina', body, word=word, prompt=prompt, prompt_label=prompt_label, options=options, idx=idx, total=total, correct=correct, direction=direction, direction_label=direction_label)

@app.route('/practice', methods=['GET','POST'])
def practice():
    answer = request.form.get('answer', '').strip() if request.method == 'POST' else ''
    word_id = request.form.get('word_id', type=int) if request.method == 'POST' else None
    with db() as conn:
        words = conn.execute('SELECT id,korean,uzbek,pronunciation,category,example,image_url FROM words ORDER BY id').fetchall()
    if not words:
        return page('Mashq', '<section class="hero"><h1>🧠 So‘z mashqi</h1><p class="muted">Avval admin paneldan so‘z qo‘shing.</p></section>')
    import random
    current = next((w for w in words if w['id'] == word_id), None) if word_id else None
    if current is None: current = random.choice(words)
    options = list({current['uzbek']})
    distractors = [w['uzbek'] for w in words if w['uzbek'] != current['uzbek']]
    random.shuffle(distractors)
    options += distractors[:3]
    random.shuffle(options)
    feedback = ''
    if request.method == 'POST':
        feedback = '<div class="notice">' + ('✅ To‘g‘ri! Barakalla!' if answer == current['uzbek'] else '❌ Bu safar xato. To‘g‘ri javob: ' + current['uzbek']) + '</div>'
    body = '''<section class="hero"><div class="eyebrow">ESLAB QOLISH MASHQI</div><h1>🧠 So‘zni top</h1><p class="muted">Koreyscha so‘zning o‘zbekcha ma’nosini tanla.</p></section>''' + feedback + '''<section class="panel" style="max-width:600px;margin:auto;text-align:center">{% if word.image_url %}<img class="word-image" src="{{ word.image_url }}" alt="{{ word.korean }}">{% endif %}<div class="hangul" style="font-size:48px">{{ word.korean }}</div><div class="pron">{{ word.pronunciation }}</div><button class="btn audio-btn" type="button" onclick="speakKorean(this.dataset.korean)" data-korean="{{ word.korean }}">🔊 Eshitish</button><form method="post" style="text-align:left;margin-top:20px"><input type="hidden" name="word_id" value="{{ word.id }}">{% for option in options %}<label class="word" style="display:block;margin:8px 0;cursor:pointer"><input type="radio" name="answer" value="{{ option }}" required style="width:auto;margin-right:8px">{{ option }}</label>{% endfor %}<button class="btn primary" style="width:100%;margin-top:12px">Javobni tekshirish</button></form><a class="btn" style="margin-top:12px" href="{{ url_for('practice') }}">Keyingi so‘z →</a></section>'''
    return page('Mashq', body, word=current, options=options)

@app.route('/admin/login',methods=['GET','POST'])
def login():
    if request.method=='POST':
        if request.form.get('username','')==ADMIN_USERNAME and request.form.get('password','')==ADMIN_PASSWORD:
            session['admin_logged_in']=True; flash('Admin panelga muvaffaqiyatli kirdingiz.'); return redirect(url_for('admin'))
        flash('Login yoki parol noto‘g‘ri.')
    body='''<div class="center"><div class="eyebrow">XAVFSIZ KIRISH</div><h1>Admin panel</h1><p class="muted">Bo‘limlar va so‘zlarni boshqarish.</p><form class="panel" method="post"><div class="field"><label>Login</label><input name="username" autocomplete="username" required></div><div class="field"><label>Parol</label><input type="password" name="password" autocomplete="current-password" required></div><button class="btn primary" type="submit" style="width:100%">Kirish</button></form></div>'''
    return page('Admin kirishi',body)

@app.route('/admin/logout')
def logout():
    session.clear(); return redirect(url_for('vocabulary'))

@app.route('/admin',methods=['GET','POST'])
@admin_required
def admin():
    if request.method=='POST':
        action=request.form.get('action','')
        with db() as conn:
            if action=='add_section':
                name=request.form.get('section_name','').strip(); description=request.form.get('description','').strip()
                if not name: flash('Bo‘lim nomini kiriting.')
                else:
                    try:
                        conn.execute('INSERT INTO sections (name,description) VALUES (?,?)',(name,description))
                        flash('Yangi bo‘lim yaratildi.')
                    except sqlite3.IntegrityError:
                        flash('Bunday bo‘lim allaqachon mavjud.')
            elif action=='delete_section':
                sid=request.form.get('section_id',type=int)
                s=conn.execute('SELECT * FROM sections WHERE id=?',(sid,)).fetchone()
                if s:
                    conn.execute('DELETE FROM words WHERE category=?',(s['name'],)); conn.execute('DELETE FROM sections WHERE id=?',(sid,)); flash('Bo‘lim va uning so‘zlari o‘chirildi.')
            elif action=='rename_section':
                sid=request.form.get('section_id',type=int); newname=request.form.get('new_name','').strip(); desc=request.form.get('description','').strip()
                s=conn.execute('SELECT * FROM sections WHERE id=?',(sid,)).fetchone()
                if s and newname:
                    try:
                        conn.execute('UPDATE sections SET name=?,description=? WHERE id=?',(newname,desc,sid)); conn.execute('UPDATE words SET category=? WHERE category=?',(newname,s['name'])); flash('Bo‘lim yangilandi.')
                    except sqlite3.IntegrityError: flash('Bu nomdagi bo‘lim mavjud.')
            elif action in ('add_word','update_word','delete_word'):
                if action=='delete_word': conn.execute('DELETE FROM words WHERE id=?',(request.form.get('id'),)); flash('So‘z o‘chirildi.')
                else:
                    korean=request.form.get('korean','').strip(); uzbek=request.form.get('uzbek','').strip(); pron=request.form.get('pronunciation','').strip(); category=request.form.get('category','').strip(); example=request.form.get('example','').strip(); image_url=request.form.get('image_url','').strip(); part_of_speech=request.form.get('part_of_speech','Ko‘rsatilmagan').strip(); wid=request.form.get('id','').strip()
                    if not pron and korean:
                        pron = auto_pronunciation(korean)
                    section=conn.execute('SELECT name FROM sections WHERE name=?',(category,)).fetchone()
                    if not korean or not uzbek or not section: flash('So‘z, tarjima va to‘g‘ri bo‘limni kiriting.')
                    elif action=='update_word': conn.execute('UPDATE words SET korean=?,uzbek=?,pronunciation=?,category=?,example=?,image_url=?,part_of_speech=? WHERE id=?',(korean,uzbek,pron,category,example,image_url,part_of_speech,wid)); flash('So‘z yangilandi.')
                    else: conn.execute('INSERT INTO words (korean,uzbek,pronunciation,category,example,image_url,part_of_speech) VALUES (?,?,?,?,?,?,?)',(korean,uzbek,pron,category,example,image_url,part_of_speech)); flash('So‘z bo‘limga saqlandi.')
        drive_message = sync_drive_after_change()
        if drive_message:
            flash(drive_message)
        return redirect(url_for('admin'))
    edit_id=request.args.get('edit',type=int)
    with db() as conn:
        sections=conn.execute('SELECT s.*,COUNT(w.id) AS word_count FROM sections s LEFT JOIN words w ON w.category=s.name GROUP BY s.id ORDER BY s.name').fetchall()
        words=conn.execute('SELECT * FROM words ORDER BY category,korean').fetchall()
        editing=conn.execute('SELECT * FROM words WHERE id=?',(edit_id,)).fetchone() if edit_id else None
    body='''<div class="admin-layout fibonacci-admin"><section class="panel admin-vocabulary-panel" id="admin-vocabulary"><div class="admin-vocab-heading"><div><h2 class="admin-words-heading">📚 So‘zlar boshqaruvi <span class="tag" id="admin-word-total">{{ words|length }}</span></h2><p class="muted">Koreyscha so‘z, o‘zbekcha tarjima, talaffuz, misol gap va so‘z turkumi.</p></div><strong class="tag">Barcha so‘zlar</strong></div><div class="admin-vocab-tools"><label class="admin-vocab-search"><span>⌕</span><input id="admin-word-search" type="search" placeholder="Koreyscha, tarjima, talaffuz yoki misol..." aria-label="So‘zlarni qidirish"></label><select id="admin-word-category" aria-label="Bo‘lim bo‘yicha filtr"><option value="">📁 Barcha bo‘limlar</option>{% for s in sections %}<option value="{{ s.name|lower }}">{{ s.name }}</option>{% endfor %}</select><select id="admin-word-pos" aria-label="So‘z turkumi bo‘yicha filtr"><option value="">🔤 Barcha so‘z turkumlari</option>{% for pos in ["Ot", "Fe’l", "Sifat", "Ravish", "Olmosh", "Son", "Ko‘makchi", "Bog‘lovchi", "Undov", "Ibora", "Boshqa", "Ko‘rsatilmagan"] %}<option value="{{ pos|lower }}">{{ pos }}</option>{% endfor %}</select></div>{% if words %}<p class="admin-vocab-result" id="admin-word-result">Ko‘rsatilmoqda: {{ words|length }} / {{ words|length }} ta so‘z</p><div class="admin-word-list" id="admin-word-list">{% for w in words %}<article class="word admin-word-entry" data-category="{{ w.category|lower }}" data-pos="{{ (w.part_of_speech or 'Ko‘rsatilmagan')|lower }}" data-search="{{ (w.korean ~ ' ' ~ w.uzbek ~ ' ' ~ (w.pronunciation or '') ~ ' ' ~ w.category ~ ' ' ~ (w.part_of_speech or '') ~ ' ' ~ (w.example or ''))|lower }}"><div class="admin-word-info"><div class="hangul">{{ w.korean }}</div><div class="pron">🔊 {{ w.pronunciation or 'Talaffuz kiritilmagan' }}</div><div class="admin-translation">{{ w.uzbek }}</div></div><div class="admin-word-meta"><span class="tag">📁 {{ w.category }}</span><span class="tag">{{ w.part_of_speech or 'Ko‘rsatilmagan' }}</span></div>{% if w.example %}<p class="admin-word-example">💬 {{ w.example }}</p>{% else %}<p class="admin-word-example muted">Misol gap kiritilmagan</p>{% endif %}{% if w.image_url %}<a class="muted" style="font-size:11px" href="{{ w.image_url }}" target="_blank" rel="noopener">🖼 Rasmni ko‘rish</a>{% endif %}<div class="word-actions"><a class="btn" href="{{ url_for('admin',edit=w.id) }}">✏️ Tahrirlash</a><form method="post" onsubmit="return confirm('So‘zni o‘chirasizmi?')"><input type="hidden" name="action" value="delete_word"><input type="hidden" name="id" value="{{ w.id }}"><button class="btn danger" type="submit">🗑 O‘chirish</button></form></div></article>{% endfor %}</div><div class="admin-no-results" id="admin-word-empty" hidden>Bu qidiruv yoki filtrga mos so‘z topilmadi.</div><script>(()=>{const panel=document.getElementById('admin-vocabulary');if(!panel)return;const search=panel.querySelector('#admin-word-search'),category=panel.querySelector('#admin-word-category'),pos=panel.querySelector('#admin-word-pos'),cards=[...panel.querySelectorAll('.admin-word-entry')],result=panel.querySelector('#admin-word-result'),empty=panel.querySelector('#admin-word-empty');function filterWords(){const q=search.value.trim().toLocaleLowerCase();let visible=0;cards.forEach(card=>{const matchText=card.dataset.search.includes(q),matchCategory=!category.value||card.dataset.category===category.value,matchPos=!pos.value||card.dataset.pos===pos.value,show=matchText&&matchCategory&&matchPos;card.hidden=!show;if(show)visible++});result.textContent=`Ko‘rsatilmoqda: ${visible} / ${cards.length} ta so‘z`;empty.hidden=visible!==0}search.addEventListener('input',filterWords);category.addEventListener('change',filterWords);pos.addEventListener('change',filterWords)})();</script>{% else %}<div class="empty">Hali so‘z yo‘q.</div>{% endif %}</section><aside class="admin-rail">    <div class="admin-sections-topic-row"><section class="panel admin-sections-panel"><h2>📁 Mavjud bo‘limlar <span class="tag">{{ sections|length }}</span></h2>{% if sections %}<div class="admin-section-list">{% for s in sections %}<article class="word admin-section-card"><div class="admin-section-heading"><div class="admin-section-title"><span class="admin-section-icon">📁</span><h3>{{ s.name }}</h3></div><span class="tag">{{ s.word_count }} ta so‘z</span></div>{% if s.description %}<p class="admin-section-description">{{ s.description }}</p>{% endif %}<div class="admin-section-actions"><details class="admin-section-edit"><summary class="btn">✎ Edit</summary><form method="post" class="admin-section-form"><input type="hidden" name="action" value="rename_section"><input type="hidden" name="section_id" value="{{ s.id }}"><div class="admin-edit-fields"><label class="sr-only" for="section-name-{{ s.id }}">Bo‘lim nomi</label><input id="section-name-{{ s.id }}" name="new_name" value="{{ s.name }}" aria-label="Bo‘lim nomi" required><label class="sr-only" for="section-description-{{ s.id }}">Bo‘lim izohi</label><input id="section-description-{{ s.id }}" name="description" value="{{ s.description }}" placeholder="Qisqacha izoh" aria-label="Bo‘lim izohi"></div><button class="btn" type="submit">Saqlash</button></form></details><form method="post" class="admin-delete-section" onsubmit="return confirm('Bo‘lim va ichidagi barcha so‘zlarni o‘chirasizmi?')"><input type="hidden" name="action" value="delete_section"><input type="hidden" name="section_id" value="{{ s.id }}"><button class="btn danger" type="submit">Delete</button></form></div></article>{% endfor %}</div>{% else %}<div class="empty">Hali bo‘lim yo‘q. Avval bo‘lim yarating.</div>{% endif %}</section></div><div class="admin-forms"><section class="panel"><div class="admin-topic-system-panel" style="margin-top:18px;padding-top:16px;border-top:1px solid var(--border)"><h2>📝 Grammatika darslari</h2><p class="muted">Koreys tili qoidalari, grammatik qoliplar va misollarni yozing.</p><a class="btn primary" href="{{ url_for('grammar_admin') }}">Grammatika yozish →</a></div><div class="admin-topic-system-panel" style="margin-top:18px;padding-top:16px;border-top:1px solid var(--border)"><h2>📚 Koreys tili kitoblari</h2><p class="muted">Google Drive’dagi PDF kitoblarni qo‘shing, saytda o‘qiting va yuklab olishga ruxsat bering.</p><a class="btn primary" href="{{ url_for('books_admin') }}">Kitoblarni boshqarish →</a></div><div class="admin-topic-system-panel" style="margin-top:18px;padding-top:16px;border-top:1px solid var(--border)"><h2>📁 Yangi Mavzular tizimi</h2><p class="muted">Cheksiz papkalar va DAYlar; har bir DAY ichida Round 1 — 12 ta, Round 2 — 13 ta so‘z.</p><a class="btn primary" href="{{ url_for('topic_manager') }}">Papkalar va kunlarni boshqarish →</a></div><h2>＋ Yangi bo‘lim yaratish</h2><p class="muted small">Bo‘limni yarating va shu yerning o‘zida Wongoji mashqi uchun so‘zlarni kiriting.</p><form method="post"><input type="hidden" name="action" value="add_section"><div class="field"><label>Bo‘lim nomi *</label><input name="section_name" placeholder="Masalan: Oila" required></div><div class="field"><label>Qisqacha izoh</label><input name="description" placeholder="Oila a'zolari va qarindoshlar"></div><button class="btn primary" style="width:100%">Bo‘lim yaratish</button></form></section>
    <section class="panel" style="margin-top:16px"><h2>{% if editing %}So‘zni tahrirlash{% else %}Tanlangan bo‘limga so‘z qo‘shish{% endif %}</h2><form method="post"><input type="hidden" name="action" value="{{ 'update_word' if editing else 'add_word' }}">{% if editing %}<input type="hidden" name="id" value="{{ editing.id }}">{% endif %}<div class="field"><label>Qaysi bo‘lim? *</label><select name="category" required>{% for s in sections %}<option value="{{ s.name }}" {% if (editing and editing.category==s.name) or (not editing and loop.first) %}selected{% endif %}>{{ s.name }}</option>{% endfor %}</select></div><div class="field"><label>Koreyscha so‘z *</label><input name="korean" value="{{ editing.korean if editing else '' }}" placeholder="가족" required></div><div class="field"><label>O‘zbekcha tarjima *</label><input name="uzbek" value="{{ editing.uzbek if editing else '' }}" placeholder="Oila" required></div><div class="field"><label>So‘z turkumi *</label><select name="part_of_speech" required>{% for pos in ["Ot", "Fe’l", "Sifat", "Ravish", "Olmosh", "Son", "Ko‘makchi", "Bog‘lovchi", "Undov", "Ibora", "Boshqa", "Ko‘rsatilmagan"] %}<option value="{{ pos }}" {% if editing and editing.part_of_speech == pos %}selected{% elif not editing and pos == "Ko‘rsatilmagan" %}selected{% endif %}>{{ pos }}</option>{% endfor %}</select><small class="muted">Masalan: 가족 — Ot, 예쁘다 — Sifat, 먹다 — Fe’l.</small></div><div class="field"><label>Talaffuz (avtomatik)</label><input name="pronunciation" value="{{ editing.pronunciation if editing else '' }}" placeholder="Bo‘sh qoldirsangiz avtomatik hosil bo‘ladi"><small class="muted">Koreyscha so‘zni yozing; talaffuzni o‘zingiz ham tahrirlashingiz mumkin.</small></div><div class="field"><label>Misol gap</label><textarea name="example" placeholder="우리 가족">{{ editing.example if editing else '' }}</textarea></div><div class="field"><label>Rasm URL (ixtiyoriy)</label><input type="url" name="image_url" value="{{ editing.image_url if editing and editing.image_url else '' }}" placeholder="https://.../image.jpg"></div><button class="btn primary" style="width:100%">{{ 'O‘zgarishni saqlash' if editing else '＋ So‘zni saqlash' }}</button>{% if editing %}<a class="btn" style="margin-top:8px;width:100%" href="{{ url_for('admin') }}">Bekor qilish</a>{% endif %}</form></section></div>
</aside></div>'''
    return page('Admin',body,sections=sections,words=words,editing=editing)

if __name__=='__main__':
    if '--drive-auth' in sys.argv:
        try:
            drive_auth()
        except Exception as exc:
            print(f'Google OAuth xatosi: {exc}')
            raise SystemExit(1)
        raise SystemExit(0)

    init_db()
    initialize_drive_data()
    # 127.0.0.1 means this is local-only. Hosting will be configured separatcely.
    app.run(host='127.0.0.1', port=5000, debug=False)
