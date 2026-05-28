"""Report data helpers — GSC live fetch + SEMrush aggregates from DB."""
import os
import json
import sqlite3
from datetime import datetime, timedelta


def _gsc_credentials(token_path: str):
    if not os.path.exists(token_path):
        return None
    try:
        from google.oauth2.credentials import Credentials
        return Credentials.from_authorized_user_file(token_path)
    except Exception:
        return None


def gsc_summary(token_path: str, site_url: str, days: int = 7) -> dict:
    """Clics e impresiones del periodo + comparación con periodo anterior."""
    creds = _gsc_credentials(token_path)
    if not creds:
        return {'error': 'GSC no conectado'}
    from googleapiclient.discovery import build
    service = build('searchconsole', 'v1', credentials=creds)

    today = datetime.utcnow().date()
    # GSC retrasa 2-3 días los datos; restamos 3 días para asegurar
    end = today - timedelta(days=3)
    start = end - timedelta(days=days - 1)
    prev_end = start - timedelta(days=1)
    prev_start = prev_end - timedelta(days=days - 1)

    def fetch(s, e):
        try:
            resp = service.searchAnalytics().query(siteUrl=site_url, body={
                'startDate': s.isoformat(),
                'endDate': e.isoformat(),
                'dimensions': [],
                'rowLimit': 1,
            }).execute()
            rows = resp.get('rows', [])
            if rows:
                r = rows[0]
                return {
                    'clicks': int(r.get('clicks', 0)),
                    'impressions': int(r.get('impressions', 0)),
                    'ctr': round(r.get('ctr', 0) * 100, 2),
                    'position': round(r.get('position', 0), 2),
                }
            return {'clicks': 0, 'impressions': 0, 'ctr': 0, 'position': 0}
        except Exception as e:
            return {'error': str(e)}

    cur = fetch(start, end)
    prev = fetch(prev_start, prev_end)
    return {
        'current': cur,
        'previous': prev,
        'period': {'start': start.isoformat(), 'end': end.isoformat()},
        'previous_period': {'start': prev_start.isoformat(), 'end': prev_end.isoformat()},
    }


def gsc_timeseries(token_path: str, site_url: str, days: int = 180) -> dict:
    """Serie temporal por día — para línea de clics/impresiones."""
    creds = _gsc_credentials(token_path)
    if not creds:
        return {'error': 'GSC no conectado'}
    from googleapiclient.discovery import build
    service = build('searchconsole', 'v1', credentials=creds)

    today = datetime.utcnow().date()
    end = today - timedelta(days=3)
    start = end - timedelta(days=days - 1)

    try:
        resp = service.searchAnalytics().query(siteUrl=site_url, body={
            'startDate': start.isoformat(),
            'endDate': end.isoformat(),
            'dimensions': ['date'],
            'rowLimit': days + 5,
        }).execute()
        rows = resp.get('rows', [])
        series = [{
            'date': r['keys'][0],
            'clicks': int(r.get('clicks', 0)),
            'impressions': int(r.get('impressions', 0)),
            'ctr': round(r.get('ctr', 0) * 100, 2),
            'position': round(r.get('position', 0), 2),
        } for r in rows]
        # Totales
        total_clicks = sum(s['clicks'] for s in series)
        total_imp    = sum(s['impressions'] for s in series)
        avg_ctr      = round((total_clicks / total_imp * 100) if total_imp else 0, 2)
        avg_pos      = round(sum(s['position'] for s in series) / len(series), 2) if series else 0
        return {
            'series': series,
            'totals': {
                'clicks': total_clicks,
                'impressions': total_imp,
                'ctr': avg_ctr,
                'position': avg_pos,
            },
            'period': {'start': start.isoformat(), 'end': end.isoformat()},
        }
    except Exception as e:
        return {'error': str(e)}


def gsc_top_queries(token_path: str, site_url: str, days: int = 7, limit: int = 50) -> dict:
    creds = _gsc_credentials(token_path)
    if not creds:
        return {'error': 'GSC no conectado'}
    from googleapiclient.discovery import build
    service = build('searchconsole', 'v1', credentials=creds)

    today = datetime.utcnow().date()
    end = today - timedelta(days=3)
    start = end - timedelta(days=days - 1)

    try:
        resp = service.searchAnalytics().query(siteUrl=site_url, body={
            'startDate': start.isoformat(),
            'endDate': end.isoformat(),
            'dimensions': ['query', 'page'],
            'rowLimit': limit,
        }).execute()
        rows = resp.get('rows', [])
        out = [{
            'keyword': r['keys'][0],
            'url': r['keys'][1] if len(r['keys']) > 1 else '',
            'clicks': int(r.get('clicks', 0)),
            'impressions': int(r.get('impressions', 0)),
            'ctr': round(r.get('ctr', 0) * 100, 2),
            'position': round(r.get('position', 0), 2),
        } for r in rows]
        return {'rows': out, 'period': {'start': start.isoformat(), 'end': end.isoformat()}}
    except Exception as e:
        return {'error': str(e)}


# ── SEMrush aggregates from imports table ───────────────────────────────────

def imports_for_domain(conn: sqlite3.Connection, domain: str) -> list[dict]:
    rows = conn.execute(
        'SELECT id, report_date, keyword_count FROM imports WHERE domain=? ORDER BY report_date DESC',
        (domain,)
    ).fetchall()
    return [dict(r) for r in rows]


def keyword_distribution_over_time(conn: sqlite3.Connection, domain: str, limit_imports: int = 8) -> dict:
    """Para gráfico apilado de distribución por bucket (#1-3, #4-10, etc) a lo largo del tiempo."""
    imports = conn.execute(
        'SELECT id, report_date FROM imports WHERE domain=? ORDER BY report_date ASC',
        (domain,)
    ).fetchall()
    imports = imports[-limit_imports:] if len(imports) > limit_imports else imports

    labels = []
    buckets = {'top3': [], 'top10': [], 'top20': [], 'top100': [], 'beyond': []}
    for imp in imports:
        labels.append(imp['report_date'])
        row = conn.execute('''
            SELECT
                SUM(CASE WHEN position BETWEEN 1 AND 3 THEN 1 ELSE 0 END) AS top3,
                SUM(CASE WHEN position BETWEEN 4 AND 10 THEN 1 ELSE 0 END) AS top10,
                SUM(CASE WHEN position BETWEEN 11 AND 20 THEN 1 ELSE 0 END) AS top20,
                SUM(CASE WHEN position BETWEEN 21 AND 100 THEN 1 ELSE 0 END) AS top100,
                SUM(CASE WHEN position IS NULL OR position > 100 THEN 1 ELSE 0 END) AS beyond
            FROM rankings WHERE import_id=?
        ''', (imp['id'],)).fetchone()
        buckets['top3'].append(row['top3'] or 0)
        buckets['top10'].append(row['top10'] or 0)
        buckets['top20'].append(row['top20'] or 0)
        buckets['top100'].append(row['top100'] or 0)
        buckets['beyond'].append(row['beyond'] or 0)

    return {'labels': labels, 'buckets': buckets}


def keyword_movement_table(conn: sqlite3.Connection, domain: str, limit: int = 40) -> dict:
    """Tabla de keywords con posición actual vs anterior, igual al reporte."""
    imports = conn.execute(
        'SELECT id, report_date FROM imports WHERE domain=? ORDER BY report_date DESC LIMIT 2',
        (domain,)
    ).fetchall()
    if not imports:
        return {'rows': [], 'dates': []}

    latest = imports[0]
    prev = imports[1] if len(imports) > 1 else None

    rows = conn.execute('''
        SELECT keyword, position, search_volume, landing_url, intents, cpc, position_diff,
               visibility, visibility_diff
        FROM rankings
        WHERE import_id=?
        ORDER BY (search_volume IS NULL), search_volume DESC, position ASC
        LIMIT ?
    ''', (latest['id'], limit)).fetchall()

    return {
        'rows': [dict(r) for r in rows],
        'dates': {
            'latest': latest['report_date'],
            'previous': prev['report_date'] if prev else None,
        },
    }


# ── Authority / Backlinks / GA4 from DB ─────────────────────────────────────

def authority_history(conn: sqlite3.Connection, domain: str, limit: int = 12) -> list[dict]:
    rows = conn.execute(
        'SELECT date, score, source FROM authority_history WHERE domain=? ORDER BY date ASC LIMIT ?',
        (domain, limit)
    ).fetchall()
    return [dict(r) for r in rows]


def backlinks_history(conn: sqlite3.Connection, domain: str, limit: int = 60) -> list[dict]:
    rows = conn.execute(
        'SELECT date, total_backlinks, referring_domains, source FROM backlinks_history '
        'WHERE domain=? ORDER BY date ASC LIMIT ?',
        (domain, limit)
    ).fetchall()
    return [dict(r) for r in rows]


def ga4_latest(conn: sqlite3.Connection, domain: str) -> dict | None:
    row = conn.execute(
        'SELECT * FROM ga4_traffic WHERE domain=? ORDER BY period_end DESC LIMIT 2',
        (domain,)
    ).fetchall()
    if not row:
        return None
    out = {'current': dict(row[0])}
    if len(row) > 1:
        out['previous'] = dict(row[1])
    return out


def distinct_domains(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute('SELECT DISTINCT domain FROM imports ORDER BY domain').fetchall()
    return [r['domain'] for r in rows]
