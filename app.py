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
app.secret_key = 'serp-tracker-secret-2026'
UPLOAD_FOLDER = os.path.join(os.path.dirname(__file__), 'uploads')
DB_PATH       = os.path.join(os.path.dirname(__file__), 'rankings.db')
CONFIG_DIR    = os.path.join(os.path.dirname(__file__), 'config')
SETTINGS_PATH = os.path.join(CONFIG_DIR, 'settings.json')
GSC_CREDS_PATH  = os.path.join(CONFIG_DIR, 'gsc_credentials.json')
GSC_TOKEN_PATH  = os.path.join(CONFIG_DIR, 'gsc_token.json')
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

def gsc_is_authenticated():
    if not os.path.exists(GSC_TOKEN_PATH):
        return False
    try:
        from google.oauth2.credentials import Credentials
        creds = Credentials.from_authorized_user_file(GSC_TOKEN_PATH)
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
    ''')
    conn.commit()
    conn.close()


def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


def parse_semrush_excel(filepath):
    """Parse a SEMrush position tracking Excel export."""
    xl = pd.ExcelFile(filepath)
    sheet = xl.parse(xl.sheet_names[0], header=None)

    # Extract metadata from first rows
    domain = ''
    report_date_str = ''
    for i in range(6):
        row_val = str(sheet.iloc[i, 0]) if pd.notna(sheet.iloc[i, 0]) else ''
        if 'Period:' in row_val:
            # Period: 20260507 - 20260513  -> take the end date
            match = re.search(r'(\d{8})\s*$', row_val.strip())
            if match:
                report_date_str = match.group(1)

    # Row 6 (index 6) contains headers
    df = xl.parse(xl.sheet_names[0], header=6)
    df.columns = df.iloc[0]
    df = df[1:].reset_index(drop=True)

    # Detect domain and date from column names (e.g. *.livo.com.br/*_20260513)
    pos_col = None
    for col in df.columns:
        col_str = str(col)
        if col_str.startswith('*.') and '_2' in col_str and '_visibility' not in col_str \
                and '_type' not in col_str and '_landing' not in col_str \
                and '_difference' not in col_str:
            pos_col = col_str
            # Extract domain and date from pattern *.domain/*_YYYYMMDD
            m = re.match(r'\*\.(.+?)/\*_(\d{8})$', col_str)
            if m:
                domain = m.group(1)
                report_date_str = report_date_str or m.group(2)
            break

    if not pos_col:
        raise ValueError('No se encontró columna de posición en el archivo.')

    # Build column map
    col_map = {}
    for col in df.columns:
        col_s = str(col)
        if col_s == pos_col:
            col_map['position'] = col_s
        elif col_s.endswith('_visibility') and '_difference' not in col_s:
            col_map['visibility'] = col_s
        elif col_s.endswith('_type'):
            col_map['result_type'] = col_s
        elif col_s.endswith('_landing'):
            col_map['landing_url'] = col_s
        elif col_s.endswith('_difference') and 'visibility' not in col_s:
            col_map['position_diff'] = col_s
        elif col_s.endswith('_visibility_difference'):
            col_map['visibility_diff'] = col_s
        elif col_s == 'Keyword':
            col_map['keyword'] = col_s
        elif col_s == 'Tags':
            col_map['tags'] = col_s
        elif col_s == 'Intents':
            col_map['intents'] = col_s
        elif col_s == 'CPC':
            col_map['cpc'] = col_s
        elif col_s == 'Search Volume':
            col_map['search_volume'] = col_s
        elif col_s == 'Keyword Difficulty':
            col_map['keyword_difficulty'] = col_s

    records = []
    for _, row in df.iterrows():
        kw = str(row.get(col_map.get('keyword', 'Keyword'), '')).strip()
        if not kw or kw == 'nan':
            continue

        def safe_int(val):
            try:
                v = float(val)
                return int(v) if not pd.isna(v) else None
            except (TypeError, ValueError):
                return None

        def safe_float(val):
            try:
                v = float(val)
                return round(v, 2) if not pd.isna(v) else None
            except (TypeError, ValueError):
                return None

        def safe_str(val):
            s = str(val).strip()
            return s if s and s != 'nan' else None

        records.append({
            'keyword': kw,
            'position': safe_int(row.get(col_map.get('position'))),
            'visibility': safe_int(row.get(col_map.get('visibility'))),
            'result_type': safe_str(row.get(col_map.get('result_type'))),
            'landing_url': safe_str(row.get(col_map.get('landing_url'))),
            'position_diff': safe_int(row.get(col_map.get('position_diff'))),
            'visibility_diff': safe_int(row.get(col_map.get('visibility_diff'))),
            'tags': safe_str(row.get(col_map.get('tags'))),
            'intents': safe_str(row.get(col_map.get('intents'))),
            'cpc': safe_float(row.get(col_map.get('cpc'))),
            'search_volume': safe_int(row.get(col_map.get('search_volume'))),
            'keyword_difficulty': safe_int(row.get(col_map.get('keyword_difficulty'))),
        })

    return domain, report_date_str, records


@app.route('/')
def index():
    conn = get_db()
    imports = conn.execute(
        'SELECT * FROM imports ORDER BY report_date DESC'
    ).fetchall()
    dates = [r['report_date'] for r in imports]

    # Stats for latest import
    stats = {}
    if imports:
        latest = imports[0]
        stats['domain'] = latest['domain']
        stats['date'] = latest['report_date']
        stats['total'] = latest['keyword_count']
        top3 = conn.execute(
            'SELECT COUNT(*) as c FROM rankings WHERE import_id=? AND position<=3',
            (latest['id'],)
        ).fetchone()['c']
        top10 = conn.execute(
            'SELECT COUNT(*) as c FROM rankings WHERE import_id=? AND position<=10',
            (latest['id'],)
        ).fetchone()['c']
        stats['top3'] = top3
        stats['top10'] = top10

    conn.close()
    return render_template('index.html', imports=imports, dates=dates, stats=stats)


@app.route('/upload', methods=['POST'])
def upload():
    if 'file' not in request.files:
        flash('No se seleccionó archivo.', 'danger')
        return redirect(url_for('index'))

    file = request.files['file']
    if file.filename == '':
        flash('No se seleccionó archivo.', 'danger')
        return redirect(url_for('index'))

    if not allowed_file(file.filename):
        flash('Solo se permiten archivos .xlsx o .xls', 'danger')
        return redirect(url_for('index'))

    filename = secure_filename(file.filename)
    filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
    file.save(filepath)

    try:
        domain, report_date_str, records = parse_semrush_excel(filepath)

        # Format date YYYYMMDD -> YYYY-MM-DD
        if len(report_date_str) == 8:
            report_date = f"{report_date_str[:4]}-{report_date_str[4:6]}-{report_date_str[6:]}"
        else:
            report_date = report_date_str

        conn = get_db()

        # Check if this date+domain already exists
        existing = conn.execute(
            'SELECT id FROM imports WHERE report_date=? AND domain=?',
            (report_date, domain)
        ).fetchone()

        if existing:
            # Delete old and re-import
            conn.execute('DELETE FROM rankings WHERE import_id=?', (existing['id'],))
            conn.execute('DELETE FROM imports WHERE id=?', (existing['id'],))

        import_id = conn.execute(
            'INSERT INTO imports (filename, import_date, report_date, domain, keyword_count) VALUES (?,?,?,?,?)',
            (filename, datetime.now().strftime('%Y-%m-%d %H:%M:%S'), report_date, domain, len(records))
        ).lastrowid

        for r in records:
            conn.execute('''
                INSERT INTO rankings (import_id, report_date, domain, keyword, position, visibility,
                    result_type, landing_url, position_diff, visibility_diff, tags, intents,
                    cpc, search_volume, keyword_difficulty)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ''', (import_id, report_date, domain, r['keyword'], r['position'], r['visibility'],
                  r['result_type'], r['landing_url'], r['position_diff'], r['visibility_diff'],
                  r['tags'], r['intents'], r['cpc'], r['search_volume'], r['keyword_difficulty']))

        conn.commit()
        conn.close()
        flash(f'✓ Importadas {len(records)} palabras clave para {domain} — {report_date}', 'success')
    except Exception as e:
        flash(f'Error al procesar el archivo: {str(e)}', 'danger')

    return redirect(url_for('index'))


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

    return render_template('integrations.html',
                           settings=settings,
                           gsc_has_creds=gsc_has_creds,
                           gsc_connected=gsc_connected,
                           gsc_sites=gsc_sites)


# ── SerpAPI ─────────────────────────────────────────────────────────────────

@app.route('/serpapi/save-key', methods=['POST'])
def serpapi_save_key():
    key = request.form.get('serpapi_key', '').strip()
    settings = load_settings()
    settings['serpapi_key'] = key
    save_settings(settings)
    flash('✓ Chave SerpAPI salva com sucesso.', 'success')
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
        os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1'  # localhost only
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


if __name__ == '__main__':
    init_db()
    app.run(debug=True, port=5000)
