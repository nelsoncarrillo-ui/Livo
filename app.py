import os
import re
import sqlite3
import json
import requests
from datetime import datetime, timedelta
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, session
import pandas as pd
from werkzeug.utils import secure_filename

app = Flask(__name__)

# ── Seguridad: secret_key persistido + cookie hardening ──
_CONFIG_DIR_BOOT = os.path.join(os.path.dirname(__file__), 'config')
os.makedirs(_CONFIG_DIR_BOOT, exist_ok=True)
_SECRET_PATH = os.path.join(_CONFIG_DIR_BOOT, 'flask_secret.key')
if os.path.exists(_SECRET_PATH):
    with open(_SECRET_PATH, 'rb') as _f:
        app.secret_key = _f.read()
else:
    import secrets as _secrets_boot
    app.secret_key = _secrets_boot.token_bytes(48)
    with open(_SECRET_PATH, 'wb') as _f:
        _f.write(app.secret_key)

app.config.update(
    SESSION_COOKIE_SAMESITE='Strict',   # bloquea cross-site → mitiga CSRF
    SESSION_COOKIE_HTTPONLY=True,        # JS no puede leer la cookie
    # SESSION_COOKIE_SECURE=True solo cuando uses HTTPS
)

UPLOAD_FOLDER = os.path.join(os.path.dirname(__file__), 'uploads')
DB_PATH       = os.path.join(os.path.dirname(__file__), 'rankings.db')
CONFIG_DIR    = os.path.join(os.path.dirname(__file__), 'config')
SETTINGS_PATH = os.path.join(CONFIG_DIR, 'settings.json')
GSC_CREDS_PATH  = os.path.join(CONFIG_DIR, 'gsc_credentials.json')
GSC_TOKEN_PATH  = os.path.join(CONFIG_DIR, 'gsc_token.json')
GA4_TOKEN_PATH  = os.path.join(CONFIG_DIR, 'ga4_token.json')
IG_TOKEN_PATH   = os.path.join(CONFIG_DIR, 'instagram_token.json')
ALLOWED_EXTENSIONS = {'xlsx', 'xls'}

app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024
os.makedirs(CONFIG_DIR, exist_ok=True)


# ── Config helpers ──────────────────────────────────────────────────────────
def load_settings():
    if os.path.exists(SETTINGS_PATH):
        with open(SETTINGS_PATH, encoding='utf-8') as f:
            return json.load(f)
    return {}

def save_settings(data):
    with open(SETTINGS_PATH, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2)

# ── CTR model for traffic estimation ────────────────────────────────────────
_CTR = {1:.2748,2:.1509,3:.1095,4:.0765,5:.0574,6:.0432,7:.0328,8:.0240,9:.0181,10:.0151}

def ctr(pos):
    if not pos: return 0.0
    if pos <= 10: return _CTR.get(pos, 0.005)
    return 0.005 if pos <= 20 else 0.001

def compute_import_kpis(import_id, conn):
    rows = conn.execute(
        'SELECT position, search_volume FROM rankings WHERE import_id=? AND position IS NOT NULL',
        (import_id,)
    ).fetchall()
    if not rows:
        return None
    positions   = [r['position'] for r in rows]
    traffic     = sum(ctr(r['position']) * (r['search_volume'] or 0) for r in rows)
    potential   = sum((r['search_volume'] or 0) for r in rows)
    avg_pos     = sum(positions) / len(positions)
    visibility  = (traffic / potential * 100) if potential else 0
    return {
        'avg_position':   round(avg_pos, 2),
        'traffic':        round(traffic, 2),
        'visibility_pct': round(visibility, 2),
        'total':  len(rows),
        'top3':   sum(1 for p in positions if p <= 3),
        'top10':  sum(1 for p in positions if p <= 10),
        'top20':  sum(1 for p in positions if p <= 20),
        'top100': sum(1 for p in positions if p <= 100),
    }

def fmt_date_range(d1: str, d2: str) -> str:
    """'2026-05-07', '2026-05-13'  →  '7-13 may 2026'"""
    import calendar
    try:
        a = datetime.strptime(d1, '%Y-%m-%d')
        b = datetime.strptime(d2, '%Y-%m-%d')
        month = calendar.month_abbr[b.month].lower()
        if a.month == b.month and a.year == b.year:
            return f'{a.day}-{b.day} {month} {b.year}'
        ma = calendar.month_abbr[a.month].lower()
        return f'{a.day} {ma} - {b.day} {month} {b.year}'
    except Exception:
        return f'{d1} – {d2}'

# ── Seguridad: helpers ──────────────────────────────────────────────────────

def _is_localhost_request():
    """True si la request viene de localhost (para permitir OAuth sobre HTTP en dev)."""
    host = (request.host or '').split(':')[0]
    return host in ('localhost', '127.0.0.1', '::1', '[::1]')


def _enable_insecure_oauth_if_local():
    """Activa OAUTHLIB_INSECURE_TRANSPORT solo si la request es localhost."""
    if _is_localhost_request():
        os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1'
    else:
        os.environ.pop('OAUTHLIB_INSECURE_TRANSPORT', None)


# Endpoints donde NO aplicar CSRF Origin/Referer check (callbacks GET externos no aplican,
# pero por si acaso, y endpoints públicos GET). Solo necesario para POST.
_CSRF_EXEMPT = set()  # vacío por ahora


@app.before_request
def _csrf_origin_guard():
    """Protección CSRF para POST/PUT/DELETE/PATCH: verifica Origin o Referer."""
    if request.method not in ('POST', 'PUT', 'DELETE', 'PATCH'):
        return
    if request.endpoint in _CSRF_EXEMPT:
        return
    host = request.host
    expected = (f'http://{host}', f'https://{host}')
    origin = request.headers.get('Origin', '')
    referer = request.headers.get('Referer', '')
    if origin:
        if not origin.startswith(expected):
            return ('CSRF: Origin no coincide con el host', 403)
    elif referer:
        if not referer.startswith(expected):
            return ('CSRF: Referer no coincide con el host', 403)
    else:
        # Sin Origin ni Referer: rechazar (SameSite=Strict ya bloqueó la cookie igualmente)
        return ('CSRF: falta Origin/Referer', 403)


def gsc_is_authenticated():
    if not os.path.exists(GSC_TOKEN_PATH):
        return False
    try:
        from google.oauth2.credentials import Credentials
        creds = Credentials.from_authorized_user_file(GSC_TOKEN_PATH)
        return creds and (creds.valid or creds.refresh_token)
    except Exception:
        return False


def ga4_is_authenticated():
    if not os.path.exists(GA4_TOKEN_PATH):
        return False
    try:
        from google.oauth2.credentials import Credentials
        creds = Credentials.from_authorized_user_file(GA4_TOKEN_PATH)
        return creds and (creds.valid or creds.refresh_token)
    except Exception:
        return False


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS imports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            filename TEXT NOT NULL,
            import_date TEXT NOT NULL,
            report_date TEXT NOT NULL,
            domain TEXT NOT NULL,
            keyword_count INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS rankings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            import_id INTEGER NOT NULL,
            report_date TEXT NOT NULL,
            domain TEXT NOT NULL,
            keyword TEXT NOT NULL,
            position INTEGER,
            visibility INTEGER,
            result_type TEXT,
            landing_url TEXT,
            position_diff INTEGER,
            visibility_diff INTEGER,
            tags TEXT,
            intents TEXT,
            cpc REAL,
            search_volume INTEGER,
            keyword_difficulty INTEGER,
            is_manual INTEGER DEFAULT 0,
            FOREIGN KEY (import_id) REFERENCES imports(id)
        );

        -- User-defined labels per keyword (persists across all date imports)
        CREATE TABLE IF NOT EXISTS keyword_labels (
            domain TEXT NOT NULL,
            keyword TEXT NOT NULL,
            labels TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (domain, keyword)
        );

        CREATE INDEX IF NOT EXISTS idx_rankings_keyword ON rankings(keyword);
        CREATE INDEX IF NOT EXISTS idx_rankings_date ON rankings(report_date);
        CREATE INDEX IF NOT EXISTS idx_rankings_domain ON rankings(domain);

        CREATE TABLE IF NOT EXISTS local_rank_searches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            search_date TEXT NOT NULL,
            keyword TEXT NOT NULL,
            business_name TEXT NOT NULL,
            location_name TEXT NOT NULL,
            center_lat REAL NOT NULL,
            center_lng REAL NOT NULL,
            grid_size INTEGER NOT NULL,
            spacing_km REAL NOT NULL,
            avg_rank REAL,
            top3_count INTEGER DEFAULT 0,
            top10_count INTEGER DEFAULT 0,
            not_found_count INTEGER DEFAULT 0,
            total_points INTEGER DEFAULT 0,
            is_demo INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS local_rank_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            search_id INTEGER NOT NULL,
            lat REAL NOT NULL,
            lng REAL NOT NULL,
            row_idx INTEGER,
            col_idx INTEGER,
            rank INTEGER,
            FOREIGN KEY (search_id) REFERENCES local_rank_searches(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_lrs_keyword  ON local_rank_searches(keyword);
        CREATE INDEX IF NOT EXISTS idx_lrs_business ON local_rank_searches(business_name);
        CREATE INDEX IF NOT EXISTS idx_lrs_date     ON local_rank_searches(search_date);

        CREATE TABLE IF NOT EXISTS bot_view_analyses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            analysis_date TEXT NOT NULL,
            url TEXT NOT NULL,
            final_url TEXT,
            status_code INTEGER,
            raw_html TEXT,
            rendered_html TEXT,
            raw_fetch_ms INTEGER,
            render_ms INTEGER,
            raw_size_bytes INTEGER,
            rendered_size_bytes INTEGER,
            title TEXT,
            meta_description TEXT,
            h1_count INTEGER,
            findings_json TEXT,
            score INTEGER,
            critical_count INTEGER DEFAULT 0,
            warning_count INTEGER DEFAULT 0,
            improvement_count INTEGER DEFAULT 0,
            extra_json TEXT,
            error TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_bva_url  ON bot_view_analyses(url);
        CREATE INDEX IF NOT EXISTS idx_bva_date ON bot_view_analyses(analysis_date);

        CREATE TABLE IF NOT EXISTS crawls (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            start_url TEXT NOT NULL,
            domain TEXT NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            status TEXT NOT NULL DEFAULT 'running',
            max_pages INTEGER DEFAULT 30,
            max_depth INTEGER DEFAULT 3,
            render_js INTEGER DEFAULT 0,
            pages_done INTEGER DEFAULT 0,
            pages_failed INTEGER DEFAULT 0,
            avg_score REAL,
            error TEXT
        );

        CREATE TABLE IF NOT EXISTS crawl_pages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            crawl_id INTEGER NOT NULL,
            url TEXT NOT NULL,
            depth INTEGER DEFAULT 0,
            parent_url TEXT,
            status_code INTEGER,
            fetch_ms INTEGER,
            size_bytes INTEGER,
            title TEXT,
            meta_description TEXT,
            h1_count INTEGER,
            internal_links INTEGER,
            external_links INTEGER,
            score INTEGER,
            critical_count INTEGER DEFAULT 0,
            warning_count INTEGER DEFAULT 0,
            improvement_count INTEGER DEFAULT 0,
            findings_json TEXT,
            error TEXT,
            FOREIGN KEY (crawl_id) REFERENCES crawls(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_cp_crawl ON crawl_pages(crawl_id);
        CREATE INDEX IF NOT EXISTS idx_cp_score ON crawl_pages(score);

        -- Authority Score histórico (SEMrush/Ahrefs etc)
        CREATE TABLE IF NOT EXISTS authority_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            domain TEXT NOT NULL,
            date TEXT NOT NULL,
            score REAL NOT NULL,
            source TEXT DEFAULT 'manual',
            UNIQUE(domain, date)
        );

        -- Backlinks histórico
        CREATE TABLE IF NOT EXISTS backlinks_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            domain TEXT NOT NULL,
            date TEXT NOT NULL,
            total_backlinks INTEGER,
            referring_domains INTEGER,
            source TEXT DEFAULT 'manual',
            UNIQUE(domain, date)
        );

        -- GA4 tráfico (snapshot por periodo)
        CREATE TABLE IF NOT EXISTS ga4_traffic (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            domain TEXT NOT NULL,
            period_start TEXT NOT NULL,
            period_end TEXT NOT NULL,
            channel TEXT DEFAULT 'Organic Search',
            total_users INTEGER,
            new_users INTEGER,
            returning_users INTEGER,
            avg_engagement_seconds REAL,
            engaged_sessions INTEGER,
            events INTEGER,
            source TEXT DEFAULT 'manual',
            UNIQUE(domain, period_start, period_end, channel)
        );

        CREATE INDEX IF NOT EXISTS idx_auth_domain  ON authority_history(domain, date);
        CREATE INDEX IF NOT EXISTS idx_bl_domain    ON backlinks_history(domain, date);
        CREATE INDEX IF NOT EXISTS idx_ga4_domain   ON ga4_traffic(domain, period_end);

        -- Configuración por dominio (mapping a property GA4, site_url GSC, etc.)
        CREATE TABLE IF NOT EXISTS domain_settings (
            domain TEXT PRIMARY KEY,
            gsc_site_url TEXT,
            ga4_property_id TEXT,
            ga4_property_name TEXT,
            updated_at TEXT
        );

        -- ── SOCIAL (Instagram/FB/TikTok) ──
        CREATE TABLE IF NOT EXISTS social_accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            platform TEXT NOT NULL DEFAULT 'instagram',
            username TEXT NOT NULL,
            platform_id TEXT,
            name TEXT,
            is_own INTEGER DEFAULT 0,
            profile_picture_url TEXT,
            biography TEXT,
            added_at TEXT,
            UNIQUE(platform, username)
        );

        CREATE TABLE IF NOT EXISTS social_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id INTEGER NOT NULL,
            date TEXT NOT NULL,
            followers INTEGER,
            follows INTEGER,
            media_count INTEGER,
            engagement_rate REAL,
            FOREIGN KEY (account_id) REFERENCES social_accounts(id) ON DELETE CASCADE,
            UNIQUE(account_id, date)
        );

        CREATE TABLE IF NOT EXISTS social_posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id INTEGER NOT NULL,
            post_id TEXT,
            media_type TEXT,
            caption TEXT,
            like_count INTEGER,
            comments_count INTEGER,
            timestamp TEXT,
            permalink TEXT,
            media_url TEXT,
            thumbnail_url TEXT,
            fetched_at TEXT,
            FOREIGN KEY (account_id) REFERENCES social_accounts(id) ON DELETE CASCADE,
            UNIQUE(account_id, post_id)
        );

        CREATE INDEX IF NOT EXISTS idx_ss_account ON social_snapshots(account_id, date);
        CREATE INDEX IF NOT EXISTS idx_sp_account ON social_posts(account_id);
    ''')

    # ── Migraciones ligeras (añadir columnas que falten en tablas existentes) ──
    def _ensure_column(table, column, ddl):
        cols = [r[1] for r in conn.execute(f'PRAGMA table_info({table})').fetchall()]
        if column not in cols:
            conn.execute(f'ALTER TABLE {table} ADD COLUMN {ddl}')

    _ensure_column('bot_view_analyses', 'extra_json', 'extra_json TEXT')
    _ensure_column('crawl_pages', 'extra_json', 'extra_json TEXT')
    _ensure_column('crawls', 'duplicates_json', 'duplicates_json TEXT')
    _ensure_column('social_posts', 'share_count', 'share_count INTEGER')
    _ensure_column('social_posts', 'view_count', 'view_count INTEGER')
    _ensure_column('social_accounts', 'extra_json', 'extra_json TEXT')

    conn.commit()
    conn.close()


def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


@app.route('/')
def index():
    """Dashboard tipo comando central — sin upload de Excel."""
    conn = get_db()
    snapshots = conn.execute(
        'SELECT id, domain, report_date, filename, keyword_count FROM imports ORDER BY report_date DESC LIMIT 6'
    ).fetchall()

    stats = {}
    if snapshots:
        latest = snapshots[0]
        stats['domain'] = latest['domain']
        stats['date'] = latest['report_date']
        stats['total'] = latest['keyword_count']
        top3 = conn.execute('SELECT COUNT(*) as c FROM rankings WHERE import_id=? AND position<=3', (latest['id'],)).fetchone()['c']
        top10 = conn.execute('SELECT COUNT(*) as c FROM rankings WHERE import_id=? AND position<=10', (latest['id'],)).fetchone()['c']
        stats['top3'] = top3
        stats['top10'] = top10

    # Contadores de cada módulo
    counts = {
        'bot_view': conn.execute('SELECT COUNT(*) FROM bot_view_analyses').fetchone()[0],
        'crawls':   conn.execute('SELECT COUNT(*) FROM crawls').fetchone()[0],
        'local':    conn.execute('SELECT COUNT(*) FROM local_rank_searches').fetchone()[0],
        'snapshots': len(snapshots),
    }
    conn.close()
    return render_template('index.html', stats=stats, snapshots=snapshots, counts=counts,
                           has_gsc=gsc_is_authenticated())


@app.route('/rankings')
def rankings():
    conn = get_db()
    dates = [r['report_date'] for r in conn.execute(
        'SELECT DISTINCT report_date FROM rankings ORDER BY report_date DESC'
    ).fetchall()]
    domains = [r['domain'] for r in conn.execute(
        'SELECT DISTINCT domain FROM rankings ORDER BY domain'
    ).fetchall()]

    selected_date = request.args.get('date', dates[0] if dates else '')
    selected_domain = request.args.get('domain', domains[0] if domains else '')
    search = request.args.get('q', '').strip()
    pos_filter = request.args.get('pos', 'all')
    tag_filter = request.args.get('tag', '').strip()

    # Get all distinct user labels for the filter dropdown
    all_labels_raw = conn.execute(
        'SELECT labels FROM keyword_labels WHERE domain=? AND labels != ""',
        (selected_domain,)
    ).fetchall()
    all_labels = sorted({lbl.strip() for row in all_labels_raw for lbl in row['labels'].split(',') if lbl.strip()})

    query = '''
        SELECT r.*, COALESCE(kl.labels, '') as user_labels
        FROM rankings r
        LEFT JOIN keyword_labels kl ON kl.domain = r.domain AND kl.keyword = r.keyword
        WHERE r.report_date=? AND r.domain=?
    '''
    params = [selected_date, selected_domain]

    if search:
        query += ' AND r.keyword LIKE ?'
        params.append(f'%{search}%')

    if pos_filter == 'top3':
        query += ' AND r.position <= 3'
    elif pos_filter == 'top10':
        query += ' AND r.position <= 10'
    elif pos_filter == 'top30':
        query += ' AND r.position <= 30'

    if tag_filter:
        query += ' AND (kl.labels LIKE ? OR kl.labels LIKE ? OR kl.labels LIKE ? OR kl.labels = ?)'
        params += [f'{tag_filter},%', f'%,{tag_filter},%', f'%,{tag_filter}', tag_filter]

    query += ' ORDER BY r.position ASC NULLS LAST, r.keyword ASC'
    rows = conn.execute(query, params).fetchall()
    conn.close()

    return render_template('rankings.html',
                           rows=rows,
                           dates=dates,
                           domains=domains,
                           selected_date=selected_date,
                           selected_domain=selected_domain,
                           search=search,
                           pos_filter=pos_filter,
                           tag_filter=tag_filter,
                           all_labels=all_labels)


@app.route('/serp')
def serp():
    keyword = request.args.get('keyword', '').strip()
    conn = get_db()
    dates = [r['report_date'] for r in conn.execute(
        'SELECT DISTINCT report_date FROM rankings ORDER BY report_date DESC'
    ).fetchall()]
    selected_date = request.args.get('date', dates[0] if dates else '')

    results = []
    if keyword and selected_date:
        results = conn.execute(
            '''SELECT r.*, COALESCE(kl.labels,'') as user_labels
               FROM rankings r
               LEFT JOIN keyword_labels kl ON kl.domain=r.domain AND kl.keyword=r.keyword
               WHERE r.report_date=? AND r.keyword=?''',
            (selected_date, keyword)
        ).fetchall()

    top_keywords = conn.execute(
        'SELECT DISTINCT keyword FROM rankings ORDER BY keyword LIMIT 500'
    ).fetchall()

    domains = [r['domain'] for r in conn.execute(
        'SELECT DISTINCT domain FROM rankings ORDER BY domain'
    ).fetchall()]

    conn.close()
    settings = load_settings()
    has_serpapi = bool(settings.get('serpapi_key'))

    return render_template('serp.html',
                           keyword=keyword,
                           results=results,
                           dates=dates,
                           selected_date=selected_date,
                           top_keywords=top_keywords,
                           domains=domains,
                           has_serpapi=has_serpapi)


@app.route('/history')
def history():
    conn = get_db()
    dates = [r['report_date'] for r in conn.execute(
        'SELECT DISTINCT report_date FROM rankings ORDER BY report_date ASC'
    ).fetchall()]
    domains = [r['domain'] for r in conn.execute(
        'SELECT DISTINCT domain FROM rankings ORDER BY domain'
    ).fetchall()]

    date1 = request.args.get('date1', dates[0] if len(dates) > 0 else '')
    date2 = request.args.get('date2', dates[-1] if len(dates) > 1 else '')
    selected_domain = request.args.get('domain', domains[0] if domains else '')
    search = request.args.get('q', '').strip()

    comparison = []
    if date1 and date2 and selected_domain:
        # Get keywords from both dates
        kw_query = 'SELECT DISTINCT keyword FROM rankings WHERE domain=? AND report_date IN (?,?)'
        kw_params = [selected_domain, date1, date2]
        if search:
            kw_query += ' AND keyword LIKE ?'
            kw_params.append(f'%{search}%')

        keywords = [r['keyword'] for r in conn.execute(kw_query, kw_params).fetchall()]

        for kw in keywords:
            r1 = conn.execute(
                'SELECT * FROM rankings WHERE report_date=? AND domain=? AND keyword=?',
                (date1, selected_domain, kw)
            ).fetchone()
            r2 = conn.execute(
                'SELECT * FROM rankings WHERE report_date=? AND domain=? AND keyword=?',
                (date2, selected_domain, kw)
            ).fetchone()

            pos1 = r1['position'] if r1 else None
            pos2 = r2['position'] if r2 else None

            if pos1 is not None and pos2 is not None:
                diff = pos1 - pos2  # positive = improved (lower position number)
            else:
                diff = None

            comparison.append({
                'keyword': kw,
                'pos1': pos1,
                'pos2': pos2,
                'diff': diff,
                'search_volume': (r2 or r1)['search_volume'] if (r2 or r1) else None,
                'landing_url': (r2 or r1)['landing_url'] if (r2 or r1) else None,
                'intents': (r2 or r1)['intents'] if (r2 or r1) else None,
            })

        # Sort: most improved first
        comparison.sort(key=lambda x: (x['pos2'] is None, x['pos2'] or 999))

    conn.close()
    return render_template('history.html',
                           dates=dates,
                           domains=domains,
                           date1=date1,
                           date2=date2,
                           selected_domain=selected_domain,
                           comparison=comparison,
                           search=search)


@app.route('/api/keyword-trend')
def keyword_trend():
    keyword = request.args.get('keyword', '')
    domain = request.args.get('domain', '')
    conn = get_db()
    rows = conn.execute(
        'SELECT report_date, position FROM rankings WHERE keyword=? AND domain=? ORDER BY report_date ASC',
        (keyword, domain)
    ).fetchall()
    conn.close()
    return jsonify([{'date': r['report_date'], 'position': r['position']} for r in rows])


@app.route('/api/delete-import/<int:import_id>', methods=['POST'])
def delete_import(import_id):
    conn = get_db()
    conn.execute('DELETE FROM rankings WHERE import_id=?', (import_id,))
    conn.execute('DELETE FROM imports WHERE id=?', (import_id,))
    conn.commit()
    conn.close()
    flash('Importación eliminada.', 'info')
    return redirect(url_for('index'))


@app.route('/api/keyword/labels', methods=['POST'])
def update_keyword_labels():
    """Save user-defined labels for a keyword (persists across all dates)."""
    data = request.get_json()
    domain = data.get('domain', '').strip()
    keyword = data.get('keyword', '').strip()
    labels_raw = data.get('labels', '')
    # Normalize: split by comma, strip whitespace, deduplicate, rejoin
    labels = ','.join(sorted({l.strip() for l in labels_raw.split(',') if l.strip()}))

    if not domain or not keyword:
        return jsonify({'ok': False, 'error': 'domain/keyword required'}), 400

    conn = get_db()
    conn.execute('''
        INSERT INTO keyword_labels (domain, keyword, labels) VALUES (?,?,?)
        ON CONFLICT(domain, keyword) DO UPDATE SET labels=excluded.labels
    ''', (domain, keyword, labels))
    conn.commit()
    conn.close()
    return jsonify({'ok': True, 'labels': labels})


@app.route('/keyword/add', methods=['POST'])
def keyword_add():
    """Manually add a keyword to a specific date."""
    domain = request.form.get('domain', '').strip()
    keyword = request.form.get('keyword', '').strip()
    report_date = request.form.get('report_date', '').strip()
    position = request.form.get('position', '').strip()
    landing_url = request.form.get('landing_url', '').strip() or None
    search_volume = request.form.get('search_volume', '').strip() or None
    labels = request.form.get('labels', '').strip()

    if not domain or not keyword or not report_date:
        flash('Domínio, palavra-chave e data são obrigatórios.', 'danger')
        return redirect(url_for('rankings', date=report_date, domain=domain))

    try:
        pos_int = int(position) if position else None
    except ValueError:
        pos_int = None

    conn = get_db()

    # Get or create an import for this date+domain
    imp = conn.execute(
        'SELECT id FROM imports WHERE report_date=? AND domain=?',
        (report_date, domain)
    ).fetchone()

    if imp:
        import_id = imp['id']
        # Update keyword count
        conn.execute(
            'UPDATE imports SET keyword_count = keyword_count + 1 WHERE id=?',
            (import_id,)
        )
    else:
        import_id = conn.execute(
            'INSERT INTO imports (filename, import_date, report_date, domain, keyword_count) VALUES (?,?,?,?,?)',
            ('manual', datetime.now().strftime('%Y-%m-%d %H:%M:%S'), report_date, domain, 1)
        ).lastrowid

    # Check duplicate
    exists = conn.execute(
        'SELECT id FROM rankings WHERE report_date=? AND domain=? AND keyword=?',
        (report_date, domain, keyword)
    ).fetchone()
    if exists:
        flash(f'A palavra-chave "{keyword}" já existe para esta data.', 'warning')
    else:
        conn.execute('''
            INSERT INTO rankings (import_id, report_date, domain, keyword, position,
                landing_url, search_volume, is_manual)
            VALUES (?,?,?,?,?,?,?,1)
        ''', (import_id, report_date, domain, keyword, pos_int, landing_url,
              int(search_volume) if search_volume and search_volume.isdigit() else None))

        if labels:
            normalized = ','.join(sorted({l.strip() for l in labels.split(',') if l.strip()}))
            conn.execute('''
                INSERT INTO keyword_labels (domain, keyword, labels) VALUES (?,?,?)
                ON CONFLICT(domain, keyword) DO UPDATE SET labels=excluded.labels
            ''', (domain, keyword, normalized))

        flash(f'✓ Palavra-chave "{keyword}" adicionada.', 'success')

    conn.commit()
    conn.close()
    return redirect(url_for('rankings', date=report_date, domain=domain))


@app.route('/keyword/delete/<int:ranking_id>', methods=['POST'])
def keyword_delete(ranking_id):
    """Delete a single keyword from a specific date."""
    return_date = request.form.get('return_date', '')
    return_domain = request.form.get('return_domain', '')

    conn = get_db()
    row = conn.execute('SELECT import_id FROM rankings WHERE id=?', (ranking_id,)).fetchone()
    if row:
        conn.execute('DELETE FROM rankings WHERE id=?', (ranking_id,))
        conn.execute(
            'UPDATE imports SET keyword_count = MAX(0, keyword_count - 1) WHERE id=?',
            (row['import_id'],)
        )
        conn.commit()
        flash('Palavra-chave removida.', 'info')
    conn.close()
    return redirect(url_for('rankings', date=return_date, domain=return_domain))


@app.route('/overview')
def overview():
    conn = get_db()

    domains = [r['domain'] for r in conn.execute(
        'SELECT DISTINCT domain FROM rankings ORDER BY domain'
    ).fetchall()]
    selected_domain = request.args.get('domain', domains[0] if domains else '')

    # All imports for this domain, ordered ascending
    all_imports = conn.execute(
        'SELECT * FROM imports WHERE domain=? ORDER BY report_date ASC', (selected_domain,)
    ).fetchall()

    if not all_imports:
        conn.close()
        return render_template('overview.html', no_data=True, domains=domains,
                               selected_domain=selected_domain)

    # Default: compare second-to-last vs latest; or just latest if only one
    imp_latest = all_imports[-1]
    imp_prev   = all_imports[-2] if len(all_imports) >= 2 else None

    id1 = request.args.get('imp1', str(imp_prev['id']) if imp_prev else '')
    id2 = request.args.get('imp2', str(imp_latest['id']))

    # Resolve chosen imports
    def find_imp(iid):
        for i in all_imports:
            if str(i['id']) == str(iid):
                return i
        return None

    imp2 = find_imp(id2) or imp_latest
    imp1 = find_imp(id1) if id1 else imp_prev

    kpis2 = compute_import_kpis(imp2['id'], conn)
    kpis1 = compute_import_kpis(imp1['id'], conn) if imp1 else None

    def diff(a, b, key):
        if a is None or b is None: return None
        return round(a[key] - b[key], 2)

    kpis = {
        'visibility':   {'val': kpis2['visibility_pct'] if kpis2 else 0,
                         'diff': diff(kpis2, kpis1, 'visibility_pct')},
        'traffic':      {'val': kpis2['traffic'] if kpis2 else 0,
                         'diff': diff(kpis2, kpis1, 'traffic')},
        'avg_position': {'val': kpis2['avg_position'] if kpis2 else 0,
                         'diff': diff(kpis2, kpis1, 'avg_position')},
    }

    # ── Stacked bar chart data (one bar per import) ──────────────────────────
    chart_labels, c_top3, c_top10, c_top20, c_top100 = [], [], [], [], []
    for imp in all_imports:
        rows = conn.execute(
            'SELECT position FROM rankings WHERE import_id=? AND position IS NOT NULL',
            (imp['id'],)
        ).fetchall()
        positions = [r['position'] for r in rows]
        chart_labels.append(imp['report_date'])
        c_top3.append(sum(1 for p in positions if p <= 3))
        c_top10.append(sum(1 for p in positions if 4 <= p <= 10))
        c_top20.append(sum(1 for p in positions if 11 <= p <= 20))
        c_top100.append(sum(1 for p in positions if 21 <= p <= 100))

    # ── Keywords panel: counts + sparklines + new/lost ───────────────────────
    def kw_counts_over_time(bucket_fn):
        return [sum(1 for p in [r['position'] for r in conn.execute(
            'SELECT position FROM rankings WHERE import_id=? AND position IS NOT NULL',
            (imp['id'],)).fetchall()] if p and bucket_fn(p)) for imp in all_imports]

    def new_lost(imp_before, imp_after, bucket_fn):
        if not imp_before:
            return 0, 0
        kw_before = {r['keyword'] for r in conn.execute(
            'SELECT keyword,position FROM rankings WHERE import_id=? AND position IS NOT NULL',
            (imp_before['id'],)).fetchall() if bucket_fn(r['position'])}
        kw_after = {r['keyword'] for r in conn.execute(
            'SELECT keyword,position FROM rankings WHERE import_id=? AND position IS NOT NULL',
            (imp_after['id'],)).fetchall() if bucket_fn(r['position'])}
        return len(kw_after - kw_before), len(kw_before - kw_after)

    kw_panel = {}
    for label, fn in [('top3', lambda p: p<=3), ('top10', lambda p: p<=10),
                       ('top20', lambda p: p<=20), ('top100', lambda p: p<=100)]:
        sparkline = kw_counts_over_time(fn)
        new, lost = new_lost(imp1, imp2, fn)
        count = sparkline[-1] if sparkline else 0
        kw_panel[label] = {'count': count, 'sparkline': sparkline,
                           'new': new, 'lost': lost}

    # ── Improved / declined ──────────────────────────────────────────────────
    improved = declined = 0
    if imp1:
        prev_map = {r['keyword']: r['position'] for r in conn.execute(
            'SELECT keyword, position FROM rankings WHERE import_id=? AND position IS NOT NULL',
            (imp1['id'],)).fetchall()}
        for r in conn.execute(
            'SELECT keyword, position FROM rankings WHERE import_id=? AND position IS NOT NULL',
            (imp2['id'],)).fetchall():
            prev = prev_map.get(r['keyword'])
            if prev:
                if r['position'] < prev: improved += 1
                elif r['position'] > prev: declined += 1

    # ── Comparison table ─────────────────────────────────────────────────────
    rows2 = {r['keyword']: r for r in conn.execute(
        '''SELECT r.*, COALESCE(kl.labels,'') as user_labels
           FROM rankings r
           LEFT JOIN keyword_labels kl ON kl.domain=r.domain AND kl.keyword=r.keyword
           WHERE r.import_id=?''', (imp2['id'],)).fetchall()}
    rows1 = {}
    if imp1:
        rows1 = {r['keyword']: r['position'] for r in conn.execute(
            'SELECT keyword, position FROM rankings WHERE import_id=?',
            (imp1['id'],)).fetchall()}

    # Union of all keywords from both imports
    all_kws = set(rows2.keys())
    if imp1:
        all_kws |= {r['keyword'] for r in conn.execute(
            'SELECT keyword FROM rankings WHERE import_id=?', (imp1['id'],)).fetchall()}

    table_rows = []
    for kw in all_kws:
        r2 = rows2.get(kw)
        pos1 = rows1.get(kw)
        pos2 = r2['position'] if r2 else None
        vol  = r2['search_volume'] if r2 else None
        diff_pos = None
        if pos1 is not None and pos2 is not None:
            diff_pos = pos1 - pos2  # positive = improved (lower number = better)
        elif pos2 is None:
            diff_pos = None  # lost

        traffic_est = round(ctr(pos2) * (vol or 0), 2) if pos2 else 0
        traffic_prev = round(ctr(pos1) * (vol or 0), 2) if pos1 else 0
        table_rows.append({
            'keyword':      kw,
            'intents':      r2['intents'] if r2 else None,
            'result_type':  r2['result_type'] if r2 else None,
            'pos1':         pos1,
            'pos2':         pos2,
            'diff':         diff_pos,
            'volume':       vol,
            'traffic_est':  traffic_est,
            'traffic_diff': round(traffic_est - traffic_prev, 2),
            'url':          r2['landing_url'] if r2 else None,
            'user_labels':  r2['user_labels'] if r2 else '',
        })

    # Sort by volume desc
    table_rows.sort(key=lambda x: (x['volume'] or 0), reverse=True)

    date_range = fmt_date_range(
        (imp1 or imp2)['report_date'], imp2['report_date']
    )

    conn.close()
    return render_template('overview.html',
        domains=domains, selected_domain=selected_domain,
        all_imports=all_imports, imp1=imp1, imp2=imp2,
        kpis=kpis, chart_labels=chart_labels,
        c_top3=c_top3, c_top10=c_top10, c_top20=c_top20, c_top100=c_top100,
        kw_panel=kw_panel, improved=improved, declined=declined,
        table_rows=table_rows, date_range=date_range,
        no_data=False)


@app.route('/api/labels/all')
def all_labels_api():
    domain = request.args.get('domain', '')
    conn = get_db()
    rows = conn.execute(
        'SELECT labels FROM keyword_labels WHERE domain=? AND labels != ""', (domain,)
    ).fetchall()
    conn.close()
    labels = sorted({l.strip() for row in rows for l in row['labels'].split(',') if l.strip()})
    return jsonify(labels)


# ════════════════════════════════════════════════════════════════════════════
#  INTEGRATIONS PAGE
# ════════════════════════════════════════════════════════════════════════════

@app.route('/integrations')
def integrations():
    settings = load_settings()
    gsc_has_creds = os.path.exists(GSC_CREDS_PATH)
    gsc_connected = gsc_is_authenticated()

    # Get list of GSC sites if connected
    gsc_sites = []
    if gsc_connected:
        try:
            from google.oauth2.credentials import Credentials
            from googleapiclient.discovery import build
            creds = Credentials.from_authorized_user_file(GSC_TOKEN_PATH)
            service = build('searchconsole', 'v1', credentials=creds)
            resp = service.sites().list().execute()
            gsc_sites = [s['siteUrl'] for s in resp.get('siteEntry', [])]
        except Exception as e:
            gsc_sites = []

    ga4_connected = ga4_is_authenticated()
    ig_tok = ig_load_token()

    return render_template('integrations.html',
                           settings=settings,
                           gsc_has_creds=gsc_has_creds,
                           gsc_connected=gsc_connected,
                           gsc_sites=gsc_sites,
                           ga4_connected=ga4_connected,
                           ig_connected=ig_is_authenticated(),
                           ig_username=(ig_tok or {}).get('username'),
                           tt_connected=tt_is_authenticated())


# ── SerpAPI ─────────────────────────────────────────────────────────────────

@app.route('/serpapi/save-key', methods=['POST'])
def serpapi_save_key():
    key = request.form.get('serpapi_key', '').strip()
    settings = load_settings()
    settings['serpapi_key'] = key
    save_settings(settings)
    flash('✓ Chave SerpAPI salva com sucesso.', 'success')
    return redirect(url_for('integrations'))


@app.route('/pagespeed/save-key', methods=['POST'])
def pagespeed_save_key():
    key = request.form.get('pagespeed_key', '').strip()
    settings = load_settings()
    settings['pagespeed_key'] = key
    save_settings(settings)
    flash('✓ Chave PageSpeed Insights salva.', 'success')
    return redirect(url_for('integrations'))


@app.route('/api/serpapi/search')
def serpapi_search():
    """Live Google search via SerpAPI. Returns top results for a keyword."""
    keyword = request.args.get('keyword', '').strip()
    domain  = request.args.get('domain', '').strip()
    settings = load_settings()
    api_key = settings.get('serpapi_key', '')

    if not api_key:
        return jsonify({'error': 'Chave SerpAPI não configurada. Vá em Integrações.'}), 400
    if not keyword:
        return jsonify({'error': 'Keyword obrigatória.'}), 400

    params = {
        'engine': 'google',
        'q': keyword,
        'location': 'Brazil',
        'hl': 'pt-BR',
        'gl': 'br',
        'num': 20,
        'api_key': api_key,
    }
    try:
        r = requests.get('https://serpapi.com/search.json', params=params, timeout=15)
        r.raise_for_status()
        data = r.json()
    except requests.RequestException as e:
        return jsonify({'error': f'Erro na API: {str(e)}'}), 502

    organic = data.get('organic_results', [])
    results = []
    our_position = None

    for item in organic:
        pos = item.get('position')
        link = item.get('link', '')
        results.append({
            'position': pos,
            'title': item.get('title', ''),
            'link': link,
            'snippet': item.get('snippet', ''),
            'displayed_link': item.get('displayed_link', ''),
            'is_ours': domain and domain.lower() in link.lower(),
        })
        if domain and domain.lower() in link.lower() and our_position is None:
            our_position = pos

    return jsonify({
        'keyword': keyword,
        'our_position': our_position,
        'total_results': data.get('search_information', {}).get('total_results'),
        'results': results,
        'credits_used': data.get('search_metadata', {}).get('credits_used'),
    })


@app.route('/api/serpapi/save-result', methods=['POST'])
def serpapi_save_result():
    """Save a SerpAPI search result to the rankings DB."""
    data = request.get_json()
    keyword     = data.get('keyword', '').strip()
    domain      = data.get('domain', '').strip()
    position    = data.get('position')
    landing_url = data.get('landing_url', '')
    report_date = data.get('report_date', datetime.now().strftime('%Y-%m-%d'))

    if not keyword or not domain:
        return jsonify({'ok': False, 'error': 'keyword/domain required'}), 400

    conn = get_db()
    imp = conn.execute(
        'SELECT id FROM imports WHERE report_date=? AND domain=?', (report_date, domain)
    ).fetchone()
    if imp:
        import_id = imp['id']
        conn.execute('UPDATE imports SET keyword_count = keyword_count + 1 WHERE id=?', (import_id,))
    else:
        import_id = conn.execute(
            'INSERT INTO imports (filename, import_date, report_date, domain, keyword_count) VALUES (?,?,?,?,?)',
            ('serpapi_live', datetime.now().strftime('%Y-%m-%d %H:%M:%S'), report_date, domain, 1)
        ).lastrowid

    existing = conn.execute(
        'SELECT id FROM rankings WHERE report_date=? AND domain=? AND keyword=?',
        (report_date, domain, keyword)
    ).fetchone()
    if existing:
        conn.execute(
            'UPDATE rankings SET position=?, landing_url=? WHERE id=?',
            (position, landing_url, existing['id'])
        )
    else:
        conn.execute('''
            INSERT INTO rankings (import_id, report_date, domain, keyword, position, landing_url, is_manual)
            VALUES (?,?,?,?,?,?,1)
        ''', (import_id, report_date, domain, keyword, position, landing_url))

    conn.commit()
    conn.close()
    return jsonify({'ok': True})


# ── Google Search Console ────────────────────────────────────────────────────

GSC_SCOPES = ['https://www.googleapis.com/auth/webmasters.readonly']

@app.route('/gsc/upload-credentials', methods=['POST'])
def gsc_upload_credentials():
    if 'credentials_file' not in request.files:
        flash('Nenhum arquivo selecionado.', 'danger')
        return redirect(url_for('integrations'))

    f = request.files['credentials_file']
    if not f.filename.endswith('.json'):
        flash('O arquivo deve ser um .json do Google Cloud Console.', 'danger')
        return redirect(url_for('integrations'))

    try:
        content = json.load(f)
        # Validate it's OAuth2 credentials
        if 'web' not in content and 'installed' not in content:
            flash('Arquivo inválido. Precisa ser credenciais OAuth2 do Google Cloud Console.', 'danger')
            return redirect(url_for('integrations'))

        # Force redirect URI for web type
        key = 'web' if 'web' in content else 'installed'
        redirect_uri = url_for('gsc_callback', _external=True)
        if redirect_uri not in content[key].get('redirect_uris', []):
            content[key].setdefault('redirect_uris', []).append(redirect_uri)

        with open(GSC_CREDS_PATH, 'w', encoding='utf-8') as out:
            json.dump(content, out, indent=2)

        flash('✓ Credenciais salvas. Agora clique em "Conectar com Google".', 'success')
    except Exception as e:
        flash(f'Erro ao processar credenciais: {e}', 'danger')

    return redirect(url_for('integrations'))


@app.route('/gsc/auth')
def gsc_auth():
    if not os.path.exists(GSC_CREDS_PATH):
        flash('Faça upload das credenciais primeiro.', 'warning')
        return redirect(url_for('integrations'))

    try:
        from google_auth_oauthlib.flow import Flow
        flow = Flow.from_client_secrets_file(
            GSC_CREDS_PATH,
            scopes=GSC_SCOPES,
            redirect_uri=url_for('gsc_callback', _external=True)
        )
        auth_url, state = flow.authorization_url(
            access_type='offline',
            include_granted_scopes='true',
            prompt='consent'
        )
        session['gsc_oauth_state'] = state
        return redirect(auth_url)
    except Exception as e:
        flash(f'Erro ao iniciar autenticação: {e}', 'danger')
        return redirect(url_for('integrations'))


@app.route('/gsc/callback')
def gsc_callback():
    state = session.get('gsc_oauth_state')
    if not state:
        flash('Sessão expirada. Tente novamente.', 'warning')
        return redirect(url_for('integrations'))

    try:
        from google_auth_oauthlib.flow import Flow
        _enable_insecure_oauth_if_local()
        flow = Flow.from_client_secrets_file(
            GSC_CREDS_PATH,
            scopes=GSC_SCOPES,
            state=state,
            redirect_uri=url_for('gsc_callback', _external=True)
        )
        flow.fetch_token(authorization_response=request.url)
        creds = flow.credentials
        token_data = {
            'token': creds.token,
            'refresh_token': creds.refresh_token,
            'token_uri': creds.token_uri,
            'client_id': creds.client_id,
            'client_secret': creds.client_secret,
            'scopes': list(creds.scopes) if creds.scopes else GSC_SCOPES,
        }
        with open(GSC_TOKEN_PATH, 'w', encoding='utf-8') as f:
            json.dump(token_data, f, indent=2)

        flash('✓ Google Search Console conectado com sucesso!', 'success')
    except Exception as e:
        flash(f'Erro na autenticação: {e}', 'danger')

    return redirect(url_for('integrations'))


@app.route('/gsc/disconnect', methods=['POST'])
def gsc_disconnect():
    if os.path.exists(GSC_TOKEN_PATH):
        os.remove(GSC_TOKEN_PATH)
    flash('Search Console desconectado.', 'info')
    return redirect(url_for('integrations'))


@app.route('/api/gsc/sites')
def gsc_sites_api():
    if not gsc_is_authenticated():
        return jsonify({'error': 'Não autenticado'}), 401
    try:
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build
        creds = Credentials.from_authorized_user_file(GSC_TOKEN_PATH)
        service = build('searchconsole', 'v1', credentials=creds)
        resp = service.sites().list().execute()
        sites = [s['siteUrl'] for s in resp.get('siteEntry', [])]
        return jsonify(sites)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ── Google Analytics 4 OAuth + API ──────────────────────────────────────────

GA4_SCOPES = ['https://www.googleapis.com/auth/analytics.readonly']


@app.route('/ga4/auth')
def ga4_auth():
    if not os.path.exists(GSC_CREDS_PATH):
        flash('Sube primero el credentials.json en la sección Search Console (se reusa la misma OAuth client).', 'warning')
        return redirect(url_for('integrations'))
    try:
        from google_auth_oauthlib.flow import Flow
        _enable_insecure_oauth_if_local()
        flow = Flow.from_client_secrets_file(
            GSC_CREDS_PATH,
            scopes=GA4_SCOPES,
            redirect_uri=url_for('ga4_callback', _external=True)
        )
        auth_url, state = flow.authorization_url(
            access_type='offline', include_granted_scopes='true', prompt='consent'
        )
        session['ga4_oauth_state'] = state
        return redirect(auth_url)
    except Exception as e:
        flash(f'Erro ao iniciar GA4: {e}', 'danger')
        return redirect(url_for('integrations'))


@app.route('/ga4/callback')
def ga4_callback():
    state = session.get('ga4_oauth_state')
    if not state:
        flash('Sessão expirada. Tente novamente.', 'warning')
        return redirect(url_for('integrations'))
    try:
        from google_auth_oauthlib.flow import Flow
        _enable_insecure_oauth_if_local()
        flow = Flow.from_client_secrets_file(
            GSC_CREDS_PATH,
            scopes=GA4_SCOPES,
            state=state,
            redirect_uri=url_for('ga4_callback', _external=True)
        )
        flow.fetch_token(authorization_response=request.url)
        creds = flow.credentials
        token_data = {
            'token': creds.token,
            'refresh_token': creds.refresh_token,
            'token_uri': creds.token_uri,
            'client_id': creds.client_id,
            'client_secret': creds.client_secret,
            'scopes': list(creds.scopes) if creds.scopes else GA4_SCOPES,
        }
        with open(GA4_TOKEN_PATH, 'w', encoding='utf-8') as f:
            json.dump(token_data, f, indent=2)
        flash('✓ Google Analytics 4 conectado!', 'success')
    except Exception as e:
        flash(f'Erro na autenticação GA4: {e}', 'danger')
    return redirect(url_for('integrations'))


@app.route('/ga4/disconnect', methods=['POST'])
def ga4_disconnect():
    if os.path.exists(GA4_TOKEN_PATH):
        os.remove(GA4_TOKEN_PATH)
    flash('GA4 desconectado.', 'info')
    return redirect(url_for('integrations'))


@app.route('/api/ga4/properties')
def ga4_properties_api():
    """Lista las cuentas + properties GA4 a las que tiene acceso el usuario."""
    if not ga4_is_authenticated():
        return jsonify({'error': 'GA4 no conectado'}), 401
    try:
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build
        creds = Credentials.from_authorized_user_file(GA4_TOKEN_PATH)
        service = build('analyticsadmin', 'v1beta', credentials=creds)

        accounts_resp = service.accounts().list().execute()
        out = []
        for acc in accounts_resp.get('accounts', []):
            acc_name = acc.get('displayName', acc.get('name', ''))
            acc_id = acc.get('name', '').replace('accounts/', '')
            # Listar properties de esta cuenta
            props_resp = service.properties().list(filter=f"parent:{acc.get('name')}").execute()
            for p in props_resp.get('properties', []):
                out.append({
                    'account': acc_name,
                    'account_id': acc_id,
                    'property_id': p.get('name', '').replace('properties/', ''),
                    'property_name': p.get('displayName', ''),
                    'currency': p.get('currencyCode', ''),
                    'time_zone': p.get('timeZone', ''),
                })
        return jsonify(out)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


def _ga4_run_report(property_id: str, start_date: str, end_date: str,
                    metrics: list, dimensions: list = None) -> dict:
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    creds = Credentials.from_authorized_user_file(GA4_TOKEN_PATH)
    service = build('analyticsdata', 'v1beta', credentials=creds)
    body = {
        'dateRanges': [{'startDate': start_date, 'endDate': end_date}],
        'metrics': [{'name': m} for m in metrics],
    }
    if dimensions:
        body['dimensions'] = [{'name': d} for d in dimensions]
    return service.properties().runReport(property=f'properties/{property_id}', body=body).execute()


@app.route('/api/ga4/sync', methods=['POST'])
def ga4_sync():
    """Snapshot GA4 metrics → ga4_traffic table."""
    domain = (request.form.get('domain') or '').strip()
    property_id = (request.form.get('property_id') or '').strip()
    try:
        days = max(1, min(int(request.form.get('days', 30)), 365))
    except ValueError:
        days = 30

    if not ga4_is_authenticated():
        return jsonify({'error': 'GA4 no conectado'}), 401
    if not (domain and property_id):
        return jsonify({'error': 'Faltan domain o property_id'}), 400

    today = datetime.utcnow().date()
    end = today - timedelta(days=1)
    start = end - timedelta(days=days - 1)
    prev_end = start - timedelta(days=1)
    prev_start = prev_end - timedelta(days=days - 1)

    try:
        # Período actual + anterior — por canal (organic / direct / referral / etc.)
        results = {}
        for label, s, e in [('current', start, end), ('previous', prev_start, prev_end)]:
            r = _ga4_run_report(
                property_id, s.isoformat(), e.isoformat(),
                metrics=['totalUsers', 'newUsers', 'engagedSessions', 'eventCount',
                         'userEngagementDuration'],
                dimensions=['sessionDefaultChannelGroup'],
            )
            results[label] = r

        # Guardar por canal (período actual)
        conn = get_db()
        saved = 0
        for row in results['current'].get('rows', []):
            channel = row['dimensionValues'][0]['value']
            mv = row['metricValues']
            total_users   = int(mv[0]['value'] or 0)
            new_users     = int(mv[1]['value'] or 0)
            eng_sessions  = int(mv[2]['value'] or 0)
            events        = int(mv[3]['value'] or 0)
            engagement    = float(mv[4]['value'] or 0)
            returning     = max(0, total_users - new_users)
            avg_eng_secs  = (engagement / total_users) if total_users else 0
            conn.execute(
                '''INSERT OR REPLACE INTO ga4_traffic
                (domain, period_start, period_end, channel, total_users, new_users,
                 returning_users, avg_engagement_seconds, engaged_sessions, events, source)
                VALUES (?,?,?,?,?,?,?,?,?,?,'ga4_api')''',
                (domain, start.isoformat(), end.isoformat(), channel,
                 total_users, new_users, returning, avg_eng_secs, eng_sessions, events),
            )
            saved += 1
        # Guardar también período anterior
        for row in results['previous'].get('rows', []):
            channel = row['dimensionValues'][0]['value']
            mv = row['metricValues']
            total_users = int(mv[0]['value'] or 0)
            new_users   = int(mv[1]['value'] or 0)
            eng_sessions = int(mv[2]['value'] or 0)
            events = int(mv[3]['value'] or 0)
            engagement = float(mv[4]['value'] or 0)
            returning = max(0, total_users - new_users)
            avg_eng_secs = (engagement / total_users) if total_users else 0
            conn.execute(
                '''INSERT OR REPLACE INTO ga4_traffic
                (domain, period_start, period_end, channel, total_users, new_users,
                 returning_users, avg_engagement_seconds, engaged_sessions, events, source)
                VALUES (?,?,?,?,?,?,?,?,?,?,'ga4_api')''',
                (domain, prev_start.isoformat(), prev_end.isoformat(), channel,
                 total_users, new_users, returning, avg_eng_secs, eng_sessions, events),
            )
        conn.commit()
        conn.close()
        return jsonify({'ok': True, 'channels_saved': saved,
                        'period': f'{start.isoformat()} → {end.isoformat()}'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/domain/settings', methods=['GET', 'POST'])
def domain_settings_api():
    if request.method == 'POST':
        domain = (request.form.get('domain') or '').strip()
        if not domain:
            return jsonify({'error': 'domain requerido'}), 400
        gsc_site_url = (request.form.get('gsc_site_url') or '').strip() or None
        ga4_property_id = (request.form.get('ga4_property_id') or '').strip() or None
        ga4_property_name = (request.form.get('ga4_property_name') or '').strip() or None
        conn = get_db()
        conn.execute(
            '''INSERT OR REPLACE INTO domain_settings
            (domain, gsc_site_url, ga4_property_id, ga4_property_name, updated_at)
            VALUES (?,?,?,?,?)''',
            (domain, gsc_site_url, ga4_property_id, ga4_property_name,
             datetime.now().strftime('%Y-%m-%d %H:%M:%S')),
        )
        conn.commit()
        conn.close()
        return jsonify({'ok': True})
    # GET
    domain = (request.args.get('domain') or '').strip()
    if not domain:
        return jsonify({})
    conn = get_db()
    row = conn.execute('SELECT * FROM domain_settings WHERE domain=?', (domain,)).fetchone()
    conn.close()
    return jsonify(dict(row) if row else {})


@app.route('/gsc/fetch', methods=['POST'])
def gsc_fetch():
    """Fetch Search Console data and store in DB."""
    site_url    = request.form.get('site_url', '').strip()
    start_date  = request.form.get('start_date', '').strip()
    end_date    = request.form.get('end_date', '').strip()
    row_limit   = int(request.form.get('row_limit', 1000))

    if not gsc_is_authenticated():
        flash('Search Console não está conectado.', 'warning')
        return redirect(url_for('integrations'))

    if not site_url or not start_date or not end_date:
        flash('Preencha todos os campos.', 'warning')
        return redirect(url_for('integrations'))

    try:
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build

        creds = Credentials.from_authorized_user_file(GSC_TOKEN_PATH)
        service = build('searchconsole', 'v1', credentials=creds)

        body = {
            'startDate': start_date,
            'endDate': end_date,
            'dimensions': ['query', 'page'],
            'rowLimit': min(row_limit, 25000),
            'startRow': 0,
        }
        resp = service.searchAnalytics().query(siteUrl=site_url, body=body).execute()
        rows = resp.get('rows', [])

        if not rows:
            flash('Nenhum dado encontrado para o período selecionado.', 'warning')
            return redirect(url_for('integrations'))

        # Extract domain from site_url
        domain = re.sub(r'^https?://(www\.)?', '', site_url).rstrip('/')
        report_date = end_date  # Use end date as the snapshot date

        conn = get_db()
        existing = conn.execute(
            'SELECT id FROM imports WHERE report_date=? AND domain=? AND filename=?',
            (report_date, domain, f'gsc_{start_date}_{end_date}')
        ).fetchone()
        if existing:
            conn.execute('DELETE FROM rankings WHERE import_id=?', (existing['id'],))
            conn.execute('DELETE FROM imports WHERE id=?', (existing['id'],))

        import_id = conn.execute(
            'INSERT INTO imports (filename, import_date, report_date, domain, keyword_count) VALUES (?,?,?,?,?)',
            (f'gsc_{start_date}_{end_date}', datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
             report_date, domain, len(rows))
        ).lastrowid

        for row in rows:
            keys = row.get('keys', [])
            query = keys[0] if len(keys) > 0 else ''
            page  = keys[1] if len(keys) > 1 else ''
            position   = round(row.get('position', 0))
            clicks     = row.get('clicks', 0)
            impressions = row.get('impressions', 0)

            conn.execute('''
                INSERT INTO rankings
                    (import_id, report_date, domain, keyword, position, landing_url,
                     visibility, is_manual)
                VALUES (?,?,?,?,?,?,?,0)
            ''', (import_id, report_date, domain, query, position, page, impressions))

        conn.commit()
        conn.close()
        flash(f'✓ {len(rows)} palavras-chave importadas do Search Console ({start_date} a {end_date}).', 'success')

    except Exception as e:
        flash(f'Erro ao buscar dados do Search Console: {e}', 'danger')

    return redirect(url_for('integrations'))


@app.template_filter('abs')
def abs_filter(v):
    return abs(v) if v is not None else None

@app.template_global('fmt_range')
def fmt_range(d1, d2):
    return fmt_date_range(d1, d2)


# ── Local Rank Tracker (Google Maps grid) ────────────────────────────────────

import math

def _generate_grid(center_lat, center_lng, grid_size, spacing_km):
    lat_deg = spacing_km / 111.111
    lng_deg = spacing_km / (111.111 * math.cos(math.radians(center_lat)))
    half = grid_size // 2
    points = []
    for row in range(half, -half - 1, -1):
        for col in range(-half, half + 1):
            points.append({
                'lat': center_lat + row * lat_deg,
                'lng': center_lng + col * lng_deg,
                'row': half - row,
                'col': col + half,
            })
    return points


def _mock_rank(row, col, grid_size):
    if __import__('random').random() < 0.07:
        return None
    center = grid_size // 2
    dist = math.sqrt((row - center) ** 2 + (col - center) ** 2)
    max_dist = math.sqrt(2) * center or 1
    base = 1 + int((dist / max_dist) * 16)
    noise = __import__('random').randint(-2, 2)
    return max(1, min(20, base + noise))


def _find_rank(results, business_name):
    target = business_name.lower().strip()
    target_words = [w for w in target.split() if len(w) > 3]
    for i, item in enumerate(results):
        name = (item.get('title') or '').lower()
        name_words = name.split()
        if (target in name or name in target or
                any(w in name for w in target_words) or
                any(w in target for w in name_words if len(w) > 3)):
            return i + 1
    return None


@app.route('/local-rank')
def local_rank():
    load_id = request.args.get('load', type=int)
    preload = None
    if load_id:
        conn = get_db()
        s = conn.execute('SELECT * FROM local_rank_searches WHERE id=?', (load_id,)).fetchone()
        if s:
            pts = conn.execute(
                'SELECT lat, lng, row_idx as row, col_idx as col, rank FROM local_rank_results WHERE search_id=?',
                (load_id,)
            ).fetchall()
            preload = {
                'search': dict(s),
                'points': [dict(p) for p in pts],
            }
        conn.close()
    return render_template('local_rank.html', preload=preload)


@app.route('/api/local-rank/geocode')
def local_rank_geocode():
    location = request.args.get('location', '').strip()
    if not location:
        return jsonify({'error': 'Localização obrigatória'}), 400
    try:
        r = requests.get(
            'https://nominatim.openstreetmap.org/search',
            params={'q': location, 'format': 'json', 'limit': 1},
            headers={'User-Agent': 'Livo-LocalRankTracker/1.0'},
            timeout=10
        )
        r.raise_for_status()
        data = r.json()
    except requests.RequestException as e:
        return jsonify({'error': f'Erro de geocodificação: {e}'}), 502
    if not data:
        return jsonify({'error': 'Localização não encontrada'}), 404
    return jsonify({
        'lat': float(data[0]['lat']),
        'lng': float(data[0]['lon']),
        'display': data[0]['display_name'],
    })


@app.route('/api/local-rank/analyze', methods=['POST'])
def local_rank_analyze():
    body = request.get_json(force=True)
    keyword      = body.get('keyword', '').strip()
    business     = body.get('businessName', '').strip()
    center_lat   = float(body.get('centerLat', 0))
    center_lng   = float(body.get('centerLng', 0))
    grid_size    = int(body.get('gridSize', 5))
    spacing_km   = float(body.get('spacingKm', 1))

    if not keyword or not business:
        return jsonify({'error': 'Keyword e nome do negócio são obrigatórios'}), 400

    settings = load_settings()
    api_key  = settings.get('serpapi_key', '').strip()
    use_demo = not api_key

    points  = _generate_grid(center_lat, center_lng, grid_size, spacing_km)
    results = []

    for pt in points:
        if use_demo:
            results.append({**pt, 'rank': _mock_rank(pt['row'], pt['col'], grid_size)})
        else:
            try:
                r = requests.get(
                    'https://serpapi.com/search.json',
                    params={
                        'engine': 'google_maps',
                        'q': keyword,
                        'll': f"@{pt['lat']},{pt['lng']},14z",
                        'type': 'search',
                        'api_key': api_key,
                        'hl': 'pt-BR',
                    },
                    timeout=15
                )
                r.raise_for_status()
                local_results = r.json().get('local_results', [])
                rank = _find_rank(local_results, business)
                results.append({**pt, 'rank': rank})
                __import__('time').sleep(0.25)
            except Exception:
                results.append({**pt, 'rank': None})

    # ── Save to history ─────────────────────────────────────────────────────
    location_name = body.get('locationName', '').strip() or f'{center_lat:.4f},{center_lng:.4f}'
    ranked = [r['rank'] for r in results if r.get('rank')]
    avg_rank     = round(sum(ranked) / len(ranked), 2) if ranked else None
    top3_count   = sum(1 for r in results if r.get('rank') and r['rank'] <= 3)
    top10_count  = sum(1 for r in results if r.get('rank') and r['rank'] <= 10)
    not_found    = sum(1 for r in results if not r.get('rank'))

    conn = get_db()
    search_id = conn.execute('''
        INSERT INTO local_rank_searches
            (search_date, keyword, business_name, location_name,
             center_lat, center_lng, grid_size, spacing_km,
             avg_rank, top3_count, top10_count, not_found_count, total_points, is_demo)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    ''', (
        datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        keyword, business, location_name,
        center_lat, center_lng, grid_size, spacing_km,
        avg_rank, top3_count, top10_count, not_found,
        len(results), 1 if use_demo else 0,
    )).lastrowid

    conn.executemany(
        'INSERT INTO local_rank_results (search_id, lat, lng, row_idx, col_idx, rank) VALUES (?,?,?,?,?,?)',
        [(search_id, r['lat'], r['lng'], r['row'], r['col'], r.get('rank')) for r in results]
    )
    conn.commit()
    conn.close()

    return jsonify({
        'points': results,
        'gridSize': grid_size,
        'isDemo': use_demo,
        'searchId': search_id,
    })


@app.route('/local-rank/history')
def local_rank_history():
    conn = get_db()
    # All searches ordered newest first
    searches = conn.execute(
        'SELECT * FROM local_rank_searches ORDER BY search_date DESC'
    ).fetchall()

    # Group by keyword + business_name for evolution view
    groups = {}
    for s in searches:
        key = (s['keyword'].lower(), s['business_name'].lower())
        if key not in groups:
            groups[key] = {
                'keyword': s['keyword'],
                'business': s['business_name'],
                'searches': [],
            }
        groups[key]['searches'].append(dict(s))

    # Build trend: compare last two avg_rank in each group
    group_list = []
    for g in groups.values():
        ranked_searches = [s for s in g['searches'] if s['avg_rank'] is not None]
        if len(ranked_searches) >= 2:
            delta = ranked_searches[0]['avg_rank'] - ranked_searches[1]['avg_rank']
            g['trend'] = 'up' if delta < -0.5 else ('down' if delta > 0.5 else 'stable')
            g['trend_delta'] = round(delta, 2)
        else:
            g['trend'] = 'new'
            g['trend_delta'] = None
        g['count'] = len(g['searches'])
        g['latest'] = g['searches'][0]
        group_list.append(g)

    conn.close()
    return render_template('local_rank_history.html', groups=group_list, total=len(searches))


@app.route('/local-rank/history/delete/<int:search_id>', methods=['POST'])
def local_rank_delete(search_id):
    conn = get_db()
    conn.execute('DELETE FROM local_rank_results WHERE search_id=?', (search_id,))
    conn.execute('DELETE FROM local_rank_searches WHERE id=?', (search_id,))
    conn.commit()
    conn.close()
    return ('', 204)


@app.route('/api/local-rank/recent')
def local_rank_recent():
    conn = get_db()
    rows = conn.execute(
        'SELECT id, search_date, keyword, business_name, location_name, avg_rank, top3_count, is_demo '
        'FROM local_rank_searches ORDER BY search_date DESC LIMIT 8'
    ).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route('/bot-view')
def bot_view():
    conn = get_db()
    recent = conn.execute(
        'SELECT id, analysis_date, url, status_code, score, critical_count, warning_count, '
        'improvement_count, title FROM bot_view_analyses ORDER BY analysis_date DESC LIMIT 30'
    ).fetchall()
    conn.close()
    return render_template('bot_view.html', recent=recent)


@app.route('/api/bot-view/analyze', methods=['POST'])
def bot_view_analyze():
    from bot_view import analyze_url
    url = (request.form.get('url') or request.json.get('url') if request.is_json else request.form.get('url')) or ''
    url = url.strip()
    if not url:
        return jsonify({'error': 'URL requerida'}), 400

    ps_key = load_settings().get('pagespeed_key', '')
    result = analyze_url(url, render=True, do_link_check=True, pagespeed_key=ps_key or None)
    if result.get('error') and not result.get('audit'):
        return jsonify({'error': result['error']}), 400

    raw = result.get('raw') or {}
    rendered = result.get('rendered') or {}
    audit_res = result.get('audit') or {}
    sig = audit_res.get('raw_signals') or {}
    counts = audit_res.get('counts') or {}

    conn = get_db()
    cur = conn.execute(
        '''INSERT INTO bot_view_analyses
        (analysis_date, url, final_url, status_code, raw_html, rendered_html,
         raw_fetch_ms, render_ms, raw_size_bytes, rendered_size_bytes,
         title, meta_description, h1_count, findings_json, score,
         critical_count, warning_count, improvement_count, extra_json, error)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
        (
            datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            result['url'], raw.get('final_url'), raw.get('status_code'),
            raw.get('html'), rendered.get('html'),
            raw.get('elapsed_ms'), rendered.get('elapsed_ms'),
            raw.get('size_bytes'), rendered.get('size_bytes'),
            sig.get('title', ''), sig.get('meta_description', ''),
            len(sig.get('h1', [])),
            json.dumps(audit_res.get('findings', []), ensure_ascii=False),
            audit_res.get('score', 0),
            counts.get('critical', 0), counts.get('warning', 0), counts.get('improvement', 0),
            json.dumps({
                'cat_counts': audit_res.get('cat_counts', {}),
                'pagespeed': raw.get('pagespeed'),
                'link_check': result.get('link_check'),
                'raw_signals': {k: v for k, v in sig.items() if k not in ('resources', 'internal_links_list', 'external_links_list', 'outline')},
            }, ensure_ascii=False),
            result.get('error'),
        ),
    )
    analysis_id = cur.lastrowid
    conn.commit()
    conn.close()
    return jsonify({'id': analysis_id, 'redirect': url_for('bot_view_detail', analysis_id=analysis_id)})


@app.route('/bot-view/<int:analysis_id>')
def bot_view_detail(analysis_id):
    conn = get_db()
    row = conn.execute('SELECT * FROM bot_view_analyses WHERE id=?', (analysis_id,)).fetchone()
    conn.close()
    if not row:
        flash('Análisis no encontrado', 'warning')
        return redirect(url_for('bot_view'))
    data = dict(row)
    data['findings'] = json.loads(data.get('findings_json') or '[]')
    data['extra'] = json.loads(data.get('extra_json') or '{}')
    return render_template('bot_view_detail.html', a=data)


@app.route('/bot-view/<int:analysis_id>/delete', methods=['POST'])
def bot_view_delete(analysis_id):
    conn = get_db()
    conn.execute('DELETE FROM bot_view_analyses WHERE id=?', (analysis_id,))
    conn.commit()
    conn.close()
    return ('', 204)


# ── Site Crawler ────────────────────────────────────────────────────────────

@app.route('/crawler')
def crawler_index():
    conn = get_db()
    recent = conn.execute(
        'SELECT id, start_url, domain, started_at, finished_at, status, '
        'max_pages, render_js, pages_done, pages_failed, avg_score '
        'FROM crawls ORDER BY started_at DESC LIMIT 30'
    ).fetchall()
    conn.close()
    return render_template('crawler.html', recent=recent)


@app.route('/api/crawler/start', methods=['POST'])
def crawler_start():
    from crawler import start_crawl_background, normalize_url, _registered_domain
    from urllib.parse import urlparse as _up

    url = (request.form.get('url') or '').strip()
    if not url:
        return jsonify({'error': 'URL requerida'}), 400
    if not re.match(r'^https?://', url, re.I):
        url = 'https://' + url
    url = normalize_url(url)
    if not url:
        return jsonify({'error': 'URL inválida'}), 400

    try:
        max_pages = max(1, min(int(request.form.get('max_pages', 30)), 300))
        max_depth = max(0, min(int(request.form.get('max_depth', 3)), 8))
    except ValueError:
        return jsonify({'error': 'Valores numéricos inválidos'}), 400
    render_js = request.form.get('render_js') in ('1', 'true', 'on')
    respect_robots = request.form.get('respect_robots', '1') in ('1', 'true', 'on')

    domain = _registered_domain(_up(url).netloc)

    conn = get_db()
    cur = conn.execute(
        '''INSERT INTO crawls (start_url, domain, started_at, status, max_pages, max_depth, render_js)
        VALUES (?, ?, ?, 'queued', ?, ?, ?)''',
        (url, domain, datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
         max_pages, max_depth, 1 if render_js else 0)
    )
    crawl_id = cur.lastrowid
    conn.commit()
    conn.close()

    start_crawl_background(
        crawl_id, DB_PATH,
        start_url=url, max_pages=max_pages, max_depth=max_depth,
        render_js=render_js, respect_robots=respect_robots,
    )

    return jsonify({'id': crawl_id, 'redirect': url_for('crawler_detail', crawl_id=crawl_id)})


@app.route('/api/crawler/status/<int:crawl_id>')
def crawler_status(crawl_id):
    from crawler import get_status
    conn = get_db()
    row = conn.execute('SELECT status, pages_done, pages_failed, max_pages, avg_score, finished_at, error FROM crawls WHERE id=?', (crawl_id,)).fetchone()
    conn.close()
    if not row:
        return jsonify({'error': 'not found'}), 404
    live = get_status(crawl_id)
    return jsonify({
        'db_status': row['status'],
        'pages_done': row['pages_done'],
        'pages_failed': row['pages_failed'],
        'max_pages': row['max_pages'],
        'avg_score': row['avg_score'],
        'finished_at': row['finished_at'],
        'error': row['error'],
        'live': live,
    })


@app.route('/crawler/<int:crawl_id>')
def crawler_detail(crawl_id):
    conn = get_db()
    crawl = conn.execute('SELECT * FROM crawls WHERE id=?', (crawl_id,)).fetchone()
    if not crawl:
        conn.close()
        flash('Crawl no encontrado', 'warning')
        return redirect(url_for('crawler_index'))
    pages = conn.execute(
        'SELECT * FROM crawl_pages WHERE crawl_id=? ORDER BY depth ASC, score ASC NULLS LAST, id ASC',
        (crawl_id,)
    ).fetchall()
    conn.close()
    crawl_d = dict(crawl)
    crawl_d['duplicates'] = json.loads(crawl_d.get('duplicates_json') or '{}')
    return render_template('crawler_detail.html', crawl=crawl_d, pages=[dict(p) for p in pages])


@app.route('/crawler/<int:crawl_id>/delete', methods=['POST'])
def crawler_delete(crawl_id):
    conn = get_db()
    conn.execute('DELETE FROM crawl_pages WHERE crawl_id=?', (crawl_id,))
    conn.execute('DELETE FROM crawls WHERE id=?', (crawl_id,))
    conn.commit()
    conn.close()
    return ('', 204)


# ── Report (cliente-ready dashboard) ────────────────────────────────────────

@app.route('/report')
def report():
    import report as R
    conn = get_db()
    domains = R.distinct_domains(conn)
    domain = (request.args.get('domain') or (domains[0] if domains else '')).strip()
    try:
        days = max(1, min(int(request.args.get('period', 7)), 365))
    except ValueError:
        days = 7
    site_url = request.args.get('site_url', '').strip()  # GSC site URL (puede ser distinto del domain)

    # Cargar settings del dominio (property_id, site_url)
    domain_set = None
    if domain:
        row = conn.execute('SELECT * FROM domain_settings WHERE domain=?', (domain,)).fetchone()
        domain_set = dict(row) if row else None
        # Si no se pasó site_url en query, usar el de settings
        if not site_url and domain_set and domain_set.get('gsc_site_url'):
            site_url = domain_set['gsc_site_url']

    ctx = {
        'domains': domains,
        'domain': domain,
        'days': days,
        'site_url': site_url,
        'has_gsc': gsc_is_authenticated(),
        'has_ga4': ga4_is_authenticated(),
        'domain_set': domain_set,
    }
    if domain:
        ctx['distribution'] = R.keyword_distribution_over_time(conn, domain)
        ctx['keywords']     = R.keyword_movement_table(conn, domain, limit=40)
        ctx['authority']    = R.authority_history(conn, domain)
        ctx['backlinks']    = R.backlinks_history(conn, domain)
        ctx['ga4']          = R.ga4_latest(conn, domain)

    conn.close()
    return render_template('report.html', **ctx)


@app.route('/api/report/sync-gsc', methods=['POST'])
def report_sync_gsc():
    """Snapshot rápido: descarga últimas N días de GSC en imports+rankings."""
    site_url = (request.form.get('site_url') or '').strip()
    domain   = (request.form.get('domain') or '').strip()
    try:
        days = max(1, min(int(request.form.get('days', 30)), 90))
    except ValueError:
        days = 30

    if not gsc_is_authenticated():
        return jsonify({'error': 'GSC no conectado'}), 401
    if not site_url:
        return jsonify({'error': 'Indica el site_url de Search Console'}), 400

    try:
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build
        creds = Credentials.from_authorized_user_file(GSC_TOKEN_PATH)
        service = build('searchconsole', 'v1', credentials=creds)

        today = datetime.utcnow().date()
        end = today - timedelta(days=3)
        start = end - timedelta(days=days - 1)

        resp = service.searchAnalytics().query(siteUrl=site_url, body={
            'startDate': start.isoformat(),
            'endDate': end.isoformat(),
            'dimensions': ['query', 'page'],
            'rowLimit': 25000,
        }).execute()
        rows = resp.get('rows', [])
        if not rows:
            return jsonify({'error': 'Sin datos en GSC para ese rango'}), 200

        if not domain:
            domain = re.sub(r'^(sc-domain:|https?://(www\.)?)', '', site_url).rstrip('/')

        report_date = end.isoformat()
        conn = get_db()
        existing = conn.execute(
            'SELECT id FROM imports WHERE report_date=? AND domain=? AND filename=?',
            (report_date, domain, f'gsc_{start.isoformat()}_{end.isoformat()}')
        ).fetchone()
        if existing:
            conn.execute('DELETE FROM rankings WHERE import_id=?', (existing['id'],))
            conn.execute('DELETE FROM imports WHERE id=?', (existing['id'],))

        import_id = conn.execute(
            'INSERT INTO imports (filename, import_date, report_date, domain, keyword_count) VALUES (?,?,?,?,?)',
            (f'gsc_{start.isoformat()}_{end.isoformat()}', datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
             report_date, domain, len(rows))
        ).lastrowid

        for row in rows:
            keys = row.get('keys', [])
            query = keys[0] if len(keys) > 0 else ''
            page  = keys[1] if len(keys) > 1 else ''
            conn.execute('''
                INSERT INTO rankings
                    (import_id, report_date, domain, keyword, position, landing_url,
                     visibility, is_manual)
                VALUES (?,?,?,?,?,?,?,0)
            ''', (import_id, report_date, domain, query,
                  round(row.get('position', 0)), page, int(row.get('impressions', 0))))

        conn.commit()
        conn.close()
        return jsonify({'ok': True, 'rows': len(rows), 'domain': domain, 'date': report_date})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/report/gsc')
def report_gsc_api():
    import report as R
    site_url = (request.args.get('site_url') or '').strip()
    try:
        days = max(1, min(int(request.args.get('period', 7)), 365))
    except ValueError:
        days = 7
    if not site_url:
        return jsonify({'error': 'site_url requerido'}), 400
    if not gsc_is_authenticated():
        return jsonify({'error': 'GSC no conectado'}), 401
    return jsonify({
        'summary':    R.gsc_summary(GSC_TOKEN_PATH, site_url, days=days),
        'timeseries': R.gsc_timeseries(GSC_TOKEN_PATH, site_url, days=180),
        'top_queries': R.gsc_top_queries(GSC_TOKEN_PATH, site_url, days=days, limit=20),
    })


@app.route('/report/manual/authority', methods=['POST'])
def report_manual_authority():
    domain = (request.form.get('domain') or '').strip()
    date   = (request.form.get('date') or datetime.now().strftime('%Y-%m-%d')).strip()
    try:
        score = float(request.form.get('score'))
    except (TypeError, ValueError):
        flash('Score inválido', 'warning')
        return redirect(url_for('report', domain=domain))
    conn = get_db()
    conn.execute(
        'INSERT OR REPLACE INTO authority_history (domain, date, score, source) VALUES (?,?,?,?)',
        (domain, date, score, 'manual')
    )
    conn.commit()
    conn.close()
    flash(f'✓ Authority Score {score} guardado para {date}', 'success')
    return redirect(url_for('report', domain=domain))


@app.route('/report/manual/backlinks', methods=['POST'])
def report_manual_backlinks():
    domain = (request.form.get('domain') or '').strip()
    date   = (request.form.get('date') or datetime.now().strftime('%Y-%m-%d')).strip()
    try:
        total = int(request.form.get('total_backlinks') or 0)
        refdom = int(request.form.get('referring_domains') or 0)
    except ValueError:
        flash('Valores inválidos', 'warning')
        return redirect(url_for('report', domain=domain))
    conn = get_db()
    conn.execute(
        'INSERT OR REPLACE INTO backlinks_history (domain, date, total_backlinks, referring_domains, source) '
        'VALUES (?,?,?,?,?)',
        (domain, date, total, refdom, 'manual')
    )
    conn.commit()
    conn.close()
    flash(f'✓ Backlinks {total} / RD {refdom} guardado para {date}', 'success')
    return redirect(url_for('report', domain=domain))


@app.route('/report/manual/ga4', methods=['POST'])
def report_manual_ga4():
    f = request.form
    domain = (f.get('domain') or '').strip()
    period_start = (f.get('period_start') or '').strip()
    period_end = (f.get('period_end') or '').strip()
    if not (domain and period_start and period_end):
        flash('Datos incompletos', 'warning')
        return redirect(url_for('report', domain=domain))
    def i(k):
        try: return int(f.get(k) or 0)
        except ValueError: return 0
    def fl(k):
        try: return float(f.get(k) or 0)
        except ValueError: return 0
    conn = get_db()
    conn.execute(
        '''INSERT OR REPLACE INTO ga4_traffic
        (domain, period_start, period_end, channel, total_users, new_users, returning_users,
         avg_engagement_seconds, engaged_sessions, events, source)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
        (domain, period_start, period_end, f.get('channel', 'Organic Search'),
         i('total_users'), i('new_users'), i('returning_users'),
         fl('avg_engagement_seconds'), i('engaged_sessions'), i('events'), 'manual'),
    )
    conn.commit()
    conn.close()
    flash(f'✓ GA4 {period_start}→{period_end} guardado', 'success')
    return redirect(url_for('report', domain=domain))


# ══ SOCIAL — Instagram ════════════════════════════════════════════════════════

def ig_load_token():
    if not os.path.exists(IG_TOKEN_PATH):
        return None
    try:
        with open(IG_TOKEN_PATH, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return None


def _ig_token_expired(tok):
    """True si expires_at < ahora. Si no hay expires_at, asume válido (fallará en runtime)."""
    exp = (tok or {}).get('expires_at')
    if not exp:
        return False
    try:
        return datetime.fromisoformat(exp) < datetime.utcnow()
    except Exception:
        return False


def ig_is_authenticated():
    t = ig_load_token()
    if not (t and t.get('access_token') and t.get('ig_id')):
        return False
    if _ig_token_expired(t):
        return False
    return True


@app.route('/social/save-fb-app', methods=['POST'])
def social_save_fb_app():
    settings = load_settings()
    settings['fb_app_id'] = request.form.get('fb_app_id', '').strip()
    settings['fb_app_secret'] = request.form.get('fb_app_secret', '').strip()
    save_settings(settings)
    flash('✓ Credenciais do app de Facebook salvas.', 'success')
    return redirect(url_for('integrations'))


@app.route('/social/instagram/auth')
def social_ig_auth():
    import social as S
    settings = load_settings()
    app_id = settings.get('fb_app_id')
    if not app_id:
        flash('Configura primero el App ID de Facebook en Integrações.', 'warning')
        return redirect(url_for('integrations'))
    import secrets as _secrets
    state = _secrets.token_urlsafe(16)
    session['ig_oauth_state'] = state
    redirect_uri = url_for('social_ig_callback', _external=True)
    return redirect(S.auth_url(app_id, redirect_uri, state))


@app.route('/social/instagram/callback')
def social_ig_callback():
    import social as S
    if request.args.get('state') != session.get('ig_oauth_state'):
        flash('Estado OAuth inválido. Intenta de nuevo.', 'warning')
        return redirect(url_for('integrations'))
    code = request.args.get('code')
    if not code:
        flash(f'Autorización cancelada: {request.args.get("error_description", "")}', 'warning')
        return redirect(url_for('integrations'))

    settings = load_settings()
    try:
        redirect_uri = url_for('social_ig_callback', _external=True)
        tok = S.exchange_code(settings['fb_app_id'], settings['fb_app_secret'], redirect_uri, code)
        accounts = S.discover_ig_accounts(tok['access_token'])
        if not accounts:
            flash('No se encontró ninguna cuenta de Instagram Business vinculada a tus páginas de Facebook.', 'warning')
            return redirect(url_for('integrations'))
        # Usar la primera cuenta por defecto
        primary = accounts[0]
        # Calcular expires_at con buffer de 1 día (long-lived FB token = ~60 días)
        expires_in_s = int(tok.get('expires_in') or 60 * 24 * 3600)
        expires_at = (datetime.utcnow() + timedelta(seconds=max(0, expires_in_s - 86400))).isoformat()
        token_data = {
            'access_token': tok['access_token'],
            'expires_in': tok.get('expires_in'),
            'expires_at': expires_at,
            'ig_id': primary['ig_id'],
            'username': primary['username'],
            'available_accounts': accounts,
            'saved_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        }
        with open(IG_TOKEN_PATH, 'w', encoding='utf-8') as f:
            json.dump(token_data, f, indent=2, ensure_ascii=False)
        flash(f'✓ Instagram conectado: @{primary["username"]}', 'success')
    except Exception as e:
        # No exponer detalles internos al cliente
        flash('Erro ao conectar Instagram. Verifica las credenciales y el redirect URI.', 'danger')
        app.logger.warning(f'IG callback error: {e}')
    return redirect(url_for('social_index'))


@app.route('/social/instagram/select', methods=['POST'])
def social_ig_select():
    """Cambia la cuenta IG activa entre las vinculadas a la cuenta de Meta."""
    tok = ig_load_token()
    if not tok:
        return jsonify({'error': 'Instagram no conectado'}), 401
    ig_id = (request.form.get('ig_id') or '').strip()
    match = next((a for a in tok.get('available_accounts', []) if a['ig_id'] == ig_id), None)
    if not match:
        return jsonify({'error': 'Cuenta no encontrada'}), 400
    tok['ig_id'] = match['ig_id']
    tok['username'] = match['username']
    with open(IG_TOKEN_PATH, 'w', encoding='utf-8') as f:
        json.dump(tok, f, indent=2, ensure_ascii=False)
    flash(f'✓ Cuenta activa: @{match["username"]}', 'success')
    return jsonify({'ok': True, 'username': match['username']})


@app.route('/social/instagram/disconnect', methods=['POST'])
def social_ig_disconnect():
    if os.path.exists(IG_TOKEN_PATH):
        os.remove(IG_TOKEN_PATH)
    flash('Instagram desconectado.', 'info')
    return redirect(url_for('integrations'))


@app.route('/social')
def social_index():
    conn = get_db()
    accounts = conn.execute(
        'SELECT * FROM social_accounts ORDER BY is_own DESC, username'
    ).fetchall()
    # último snapshot por cuenta
    acc_data = []
    for a in accounts:
        snap = conn.execute(
            'SELECT * FROM social_snapshots WHERE account_id=? ORDER BY date DESC LIMIT 1', (a['id'],)
        ).fetchone()
        acc_data.append({**dict(a), 'snapshot': dict(snap) if snap else None})
    conn.close()
    tok = ig_load_token() or {}
    return render_template('social.html',
                           accounts=acc_data,
                           ig_connected=ig_is_authenticated(),
                           ig_username=tok.get('username'),
                           ig_active_id=tok.get('ig_id'),
                           ig_available=tok.get('available_accounts', []),
                           meta_connected=bool(tok.get('access_token')),
                           tt_connected=tt_is_authenticated())


def _upsert_account(conn, platform, profile, is_own):
    """Inserta/actualiza una cuenta y devuelve su id."""
    username = profile.get('username')
    conn.execute(
        '''INSERT INTO social_accounts (platform, username, platform_id, name, is_own,
            profile_picture_url, biography, added_at)
           VALUES (?,?,?,?,?,?,?,?)
           ON CONFLICT(platform, username) DO UPDATE SET
            platform_id=excluded.platform_id, name=excluded.name,
            profile_picture_url=excluded.profile_picture_url, biography=excluded.biography''',
        (platform, username, profile.get('id'), profile.get('name'), 1 if is_own else 0,
         profile.get('profile_picture_url'), profile.get('biography'),
         datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
    )
    return conn.execute('SELECT id FROM social_accounts WHERE platform=? AND username=?',
                        (platform, username)).fetchone()['id']


def _save_posts(conn, account_id, posts):
    for p in posts:
        conn.execute(
            '''INSERT INTO social_posts (account_id, post_id, media_type, caption, like_count,
                comments_count, share_count, view_count, timestamp, permalink, media_url, thumbnail_url, fetched_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(account_id, post_id) DO UPDATE SET
                like_count=excluded.like_count, comments_count=excluded.comments_count,
                share_count=excluded.share_count, view_count=excluded.view_count,
                fetched_at=excluded.fetched_at''',
            (account_id, p.get('id'), p.get('media_type'), p.get('caption'),
             p.get('like_count'), p.get('comments_count'), p.get('share_count'), p.get('view_count'),
             p.get('timestamp'), p.get('permalink'), p.get('media_url'), p.get('thumbnail_url'),
             datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        )


@app.route('/api/social/instagram/sync', methods=['POST'])
def social_ig_sync():
    """Sincroniza la cuenta propia: perfil + media + snapshot + posts."""
    import social as S
    tok = ig_load_token()
    if not tok:
        return jsonify({'error': 'Instagram no conectado'}), 401
    try:
        ig_id, access_token = tok['ig_id'], tok['access_token']
        profile = S.get_profile(ig_id, access_token)
        media = S.get_media(ig_id, access_token, limit=50)
        er = S.compute_engagement_rate(media, profile.get('followers_count', 0))

        conn = get_db()
        acc_id = _upsert_account(conn, 'instagram', profile, is_own=True)
        conn.execute(
            '''INSERT INTO social_snapshots (account_id, date, followers, follows, media_count, engagement_rate)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(account_id, date) DO UPDATE SET
                followers=excluded.followers, follows=excluded.follows,
                media_count=excluded.media_count, engagement_rate=excluded.engagement_rate''',
            (acc_id, datetime.now().strftime('%Y-%m-%d'), profile.get('followers_count'),
             profile.get('follows_count'), profile.get('media_count'), er)
        )
        _save_posts(conn, acc_id, media)
        conn.commit()
        conn.close()
        return jsonify({'ok': True, 'account_id': acc_id, 'username': profile.get('username'),
                        'followers': profile.get('followers_count'), 'posts': len(media),
                        'engagement_rate': er})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/social/instagram/discover', methods=['POST'])
def social_ig_discover():
    """Analiza un competidor por username vía business discovery."""
    import social as S
    tok = ig_load_token()
    if not tok:
        return jsonify({'error': 'Instagram no conectado'}), 401
    username = (request.form.get('username') or '').strip().lstrip('@')
    if not username:
        return jsonify({'error': 'username requerido'}), 400
    try:
        bd = S.business_discovery(tok['ig_id'], tok['access_token'], username)
        media = (bd.get('media') or {}).get('data', [])
        er = S.compute_engagement_rate(media, bd.get('followers_count', 0))

        conn = get_db()
        acc_id = _upsert_account(conn, 'instagram', bd, is_own=False)
        conn.execute(
            '''INSERT INTO social_snapshots (account_id, date, followers, follows, media_count, engagement_rate)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(account_id, date) DO UPDATE SET
                followers=excluded.followers, media_count=excluded.media_count,
                engagement_rate=excluded.engagement_rate''',
            (acc_id, datetime.now().strftime('%Y-%m-%d'), bd.get('followers_count'),
             bd.get('follows_count'), bd.get('media_count'), er)
        )
        _save_posts(conn, acc_id, media)
        conn.commit()
        conn.close()
        return jsonify({'ok': True, 'account_id': acc_id, 'username': bd.get('username'),
                        'followers': bd.get('followers_count'), 'engagement_rate': er,
                        'redirect': url_for('social_account_detail', account_id=acc_id)})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/social/instagram/<int:account_id>')
def social_account_detail(account_id):
    import social as S
    conn = get_db()
    acc = conn.execute('SELECT * FROM social_accounts WHERE id=?', (account_id,)).fetchone()
    if not acc:
        conn.close()
        flash('Cuenta no encontrada', 'warning')
        return redirect(url_for('social_index'))
    acc = dict(acc)
    snapshots = [dict(r) for r in conn.execute(
        'SELECT * FROM social_snapshots WHERE account_id=? ORDER BY date ASC', (account_id,)).fetchall()]
    posts = [dict(r) for r in conn.execute(
        'SELECT * FROM social_posts WHERE account_id=? ORDER BY timestamp DESC', (account_id,)).fetchall()]
    conn.close()

    hashtags = S.extract_hashtags(posts, top=30)
    stats = S.posting_stats(posts)
    tops = S.top_posts(posts, n=9)
    latest = snapshots[-1] if snapshots else None
    # Análisis avanzado
    best_time = S.best_time_analysis(posts)
    by_type = S.engagement_by_type(posts)
    topics = S.caption_topics(posts)
    hashtag_perf = S.hashtag_performance(posts)
    recommendations = S.generate_recommendations(posts)
    return render_template('social_detail.html', acc=acc, snapshots=snapshots, posts=posts,
                           hashtags=hashtags, stats=stats, top_posts=tops, latest=latest,
                           best_time=best_time, by_type=by_type, topics=topics,
                           hashtag_perf=hashtag_perf, recs=recommendations,
                           has_claude=bool(load_settings().get('anthropic_api_key')),
                           has_gemini=bool(load_settings().get('gemini_api_key')))


@app.route('/social/account/<int:account_id>/delete', methods=['POST'])
def social_account_delete(account_id):
    conn = get_db()
    conn.execute('DELETE FROM social_posts WHERE account_id=?', (account_id,))
    conn.execute('DELETE FROM social_snapshots WHERE account_id=?', (account_id,))
    conn.execute('DELETE FROM social_accounts WHERE id=?', (account_id,))
    conn.commit()
    conn.close()
    return ('', 204)


# ── Facebook Pages (reusa token Meta del flujo Instagram) ────────────────────

@app.route('/api/social/facebook/pages')
def social_fb_pages():
    import social as S
    tok = ig_load_token()
    if not tok:
        return jsonify({'error': 'Conecta primero Meta/Instagram en Integrações'}), 401
    try:
        pages = S.get_fb_pages(tok['access_token'])
        return jsonify(pages)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/social/facebook/sync', methods=['POST'])
def social_fb_sync():
    import social as S
    tok = ig_load_token()
    if not tok:
        return jsonify({'error': 'Conecta Meta/Instagram primero'}), 401
    page_id = (request.form.get('page_id') or '').strip()
    if not page_id:
        return jsonify({'error': 'page_id requerido'}), 400
    try:
        pages = S.get_fb_pages(tok['access_token'])
        page = next((p for p in pages if p['id'] == page_id), None)
        if not page:
            return jsonify({'error': 'Página no encontrada'}), 404
        posts = S.get_fb_page_posts(page_id, page['access_token'], limit=50)
        followers = page.get('followers_count') or page.get('fan_count') or 0
        er = S.compute_engagement_rate(posts, followers)

        conn = get_db()
        profile = {'username': page['name'], 'id': page_id, 'name': page['name'],
                   'profile_picture_url': page.get('picture_url'), 'biography': None}
        acc_id = _upsert_account(conn, 'facebook', profile, is_own=True)
        conn.execute(
            '''INSERT INTO social_snapshots (account_id, date, followers, media_count, engagement_rate)
               VALUES (?,?,?,?,?)
               ON CONFLICT(account_id, date) DO UPDATE SET
                followers=excluded.followers, media_count=excluded.media_count,
                engagement_rate=excluded.engagement_rate''',
            (acc_id, datetime.now().strftime('%Y-%m-%d'), followers, len(posts), er)
        )
        _save_posts(conn, acc_id, posts)
        conn.commit()
        conn.close()
        return jsonify({'ok': True, 'account_id': acc_id, 'name': page['name'],
                        'followers': followers, 'posts': len(posts), 'engagement_rate': er,
                        'redirect': url_for('social_account_detail', account_id=acc_id)})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ── TikTok (Display API) ─────────────────────────────────────────────────────

def tt_load_token():
    p = os.path.join(CONFIG_DIR, 'tiktok_token.json')
    if not os.path.exists(p):
        return None
    try:
        with open(p, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return None


def tt_is_authenticated():
    t = tt_load_token()
    return bool(t and t.get('access_token'))


@app.route('/social/tiktok/save-app', methods=['POST'])
def social_tt_save_app():
    settings = load_settings()
    settings['tiktok_client_key'] = request.form.get('tiktok_client_key', '').strip()
    settings['tiktok_client_secret'] = request.form.get('tiktok_client_secret', '').strip()
    save_settings(settings)
    flash('✓ Credenciais do app de TikTok salvas.', 'success')
    return redirect(url_for('integrations'))


@app.route('/social/tiktok/auth')
def social_tt_auth():
    import social as S
    settings = load_settings()
    ck = settings.get('tiktok_client_key')
    if not ck:
        flash('Configura primero el Client Key de TikTok en Integrações.', 'warning')
        return redirect(url_for('integrations'))
    import secrets as _secrets
    state = _secrets.token_urlsafe(16)
    session['tt_oauth_state'] = state
    redirect_uri = url_for('social_tt_callback', _external=True)
    return redirect(S.tt_auth_url(ck, redirect_uri, state))


@app.route('/social/tiktok/callback')
def social_tt_callback():
    import social as S
    if request.args.get('state') != session.get('tt_oauth_state'):
        flash('Estado OAuth inválido (TikTok).', 'warning')
        return redirect(url_for('integrations'))
    code = request.args.get('code')
    if not code:
        flash(f'TikTok canceló: {request.args.get("error_description","")}', 'warning')
        return redirect(url_for('integrations'))
    settings = load_settings()
    try:
        redirect_uri = url_for('social_tt_callback', _external=True)
        tok = S.tt_exchange_code(settings['tiktok_client_key'], settings['tiktok_client_secret'], redirect_uri, code)
        with open(os.path.join(CONFIG_DIR, 'tiktok_token.json'), 'w', encoding='utf-8') as f:
            json.dump(tok, f, indent=2)
        flash('✓ TikTok conectado!', 'success')
    except Exception as e:
        flash(f'Erro ao conectar TikTok: {e}', 'danger')
    return redirect(url_for('social_index'))


@app.route('/social/tiktok/disconnect', methods=['POST'])
def social_tt_disconnect():
    p = os.path.join(CONFIG_DIR, 'tiktok_token.json')
    if os.path.exists(p):
        os.remove(p)
    flash('TikTok desconectado.', 'info')
    return redirect(url_for('integrations'))


@app.route('/api/social/tiktok/sync', methods=['POST'])
def social_tt_sync():
    import social as S
    tok = tt_load_token()
    if not tok:
        return jsonify({'error': 'TikTok no conectado'}), 401
    try:
        at = tok['access_token']
        profile = S.tt_get_profile(at)
        videos = S.tt_get_videos(at, max_count=20)
        followers = profile.get('follower_count', 0)
        er = S.compute_engagement_rate(videos, followers)

        conn = get_db()
        prof = {'username': profile.get('display_name') or profile.get('open_id'),
                'id': profile.get('open_id'), 'name': profile.get('display_name'),
                'profile_picture_url': profile.get('avatar_url'),
                'biography': profile.get('bio_description')}
        acc_id = _upsert_account(conn, 'tiktok', prof, is_own=True)
        conn.execute(
            '''INSERT INTO social_snapshots (account_id, date, followers, follows, media_count, engagement_rate)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(account_id, date) DO UPDATE SET
                followers=excluded.followers, follows=excluded.follows,
                media_count=excluded.media_count, engagement_rate=excluded.engagement_rate''',
            (acc_id, datetime.now().strftime('%Y-%m-%d'), followers,
             profile.get('following_count'), profile.get('video_count'), er)
        )
        _save_posts(conn, acc_id, videos)
        conn.commit()
        conn.close()
        return jsonify({'ok': True, 'account_id': acc_id, 'username': prof['username'],
                        'followers': followers, 'videos': len(videos), 'engagement_rate': er,
                        'redirect': url_for('social_account_detail', account_id=acc_id)})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/social/save-anthropic-key', methods=['POST'])
def social_save_anthropic_key():
    settings = load_settings()
    settings['anthropic_api_key'] = request.form.get('anthropic_api_key', '').strip()
    save_settings(settings)
    flash('✓ API key de Claude guardada.', 'success')
    return redirect(url_for('integrations'))


@app.route('/social/save-gemini-key', methods=['POST'])
def social_save_gemini_key():
    settings = load_settings()
    settings['gemini_api_key'] = request.form.get('gemini_api_key', '').strip()
    save_settings(settings)
    flash('✓ API key de Gemini guardada.', 'success')
    return redirect(url_for('integrations'))


@app.route('/api/social/ai-ideas/<int:account_id>', methods=['POST'])
def social_ai_ideas(account_id):
    """Genera crítica de contenido + ideas con IA (Gemini gratis o Claude)."""
    import social as S
    import social_ai as SA
    settings = load_settings()
    gemini_key = settings.get('gemini_api_key', '')
    claude_key = settings.get('anthropic_api_key', '')

    # Proveedor: el pedido explícito, o el que tenga key (preferir Gemini gratis)
    provider = (request.form.get('provider') or request.args.get('provider') or '').strip()
    if not provider:
        provider = 'gemini' if gemini_key else ('claude' if claude_key else '')
    api_key = gemini_key if provider == 'gemini' else claude_key
    if not provider or not api_key:
        return jsonify({'error': 'Configura una API key (Gemini gratis o Claude) en Integrações.'}), 400

    conn = get_db()
    acc = conn.execute('SELECT * FROM social_accounts WHERE id=?', (account_id,)).fetchone()
    if not acc:
        conn.close()
        return jsonify({'error': 'Cuenta no encontrada'}), 404
    acc = dict(acc)
    posts = [dict(r) for r in conn.execute(
        'SELECT * FROM social_posts WHERE account_id=? ORDER BY timestamp DESC', (account_id,)).fetchall()]
    conn.close()

    if len(posts) < 3:
        return jsonify({'error': 'Necesito al menos 3 publicaciones sincronizadas.'}), 400

    def eng(p):
        return (p.get('like_count') or 0) + (p.get('comments_count') or 0)
    ranked = sorted(posts, key=eng, reverse=True)
    top = ranked[:6]
    bottom = ranked[-3:] if len(ranked) > 6 else []

    # contexto de métricas
    bt = S.best_time_analysis(posts)
    by_type = S.engagement_by_type(posts)
    ctx_parts = []
    if bt.get('best_hours'):
        ctx_parts.append('Mejores horas: ' + ', '.join(f"{h['hour']:02d}h" for h in bt['best_hours'][:3]))
    if by_type:
        ctx_parts.append('Formato top: ' + by_type[0]['type'])
    stats_context = ' · '.join(ctx_parts)

    try:
        result = SA.generate_ideas(provider, api_key, acc, top, bottom, stats_context=stats_context)
        return jsonify({'ok': True, 'result': result, 'provider': provider})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/social/compare')
def social_compare():
    """Datos para tabla comparativa de todas las cuentas (último snapshot)."""
    conn = get_db()
    accounts = conn.execute('SELECT * FROM social_accounts WHERE platform=?', ('instagram',)).fetchall()
    out = []
    for a in accounts:
        snap = conn.execute(
            'SELECT * FROM social_snapshots WHERE account_id=? ORDER BY date DESC LIMIT 1', (a['id'],)
        ).fetchone()
        if snap:
            out.append({
                'id': a['id'], 'username': a['username'], 'name': a['name'], 'is_own': a['is_own'],
                'followers': snap['followers'], 'media_count': snap['media_count'],
                'engagement_rate': snap['engagement_rate'],
            })
    conn.close()
    out.sort(key=lambda x: x.get('followers') or 0, reverse=True)
    return jsonify(out)


if __name__ == '__main__':
    init_db()
    app.run(debug=True, port=5000)
