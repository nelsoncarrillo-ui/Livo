"""Site crawler — BFS over a single domain with SEO audit per page.

Reusa fetch_as_googlebot, render_with_playwright y audit de bot_view.py.
Corre en background thread. Estado global en CRAWL_STATUS para polling desde UI.
"""
import os
import re
import time
import json
import sqlite3
import threading
from collections import deque
from datetime import datetime
from urllib.parse import urlparse, urljoin, urldefrag
from urllib import robotparser

import requests
from bs4 import BeautifulSoup

from bot_view import (
    fetch_as_googlebot, render_with_playwright, audit,
    GOOGLEBOT_UA_MOBILE,
)

# Estado global de cada crawl en ejecución (no persiste reinicios — solo para progreso UI)
CRAWL_STATUS: dict[int, dict] = {}
_STATUS_LOCK = threading.Lock()

# Extensiones de assets que no son HTML (para descartar links rápidamente)
ASSET_EXT = {
    '.jpg', '.jpeg', '.png', '.gif', '.webp', '.svg', '.ico', '.bmp',
    '.css', '.js', '.mjs', '.json', '.xml', '.txt',
    '.pdf', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx', '.zip', '.rar',
    '.mp3', '.mp4', '.avi', '.mov', '.webm', '.woff', '.woff2', '.ttf', '.eot',
}


def _set_status(crawl_id: int, **kwargs):
    with _STATUS_LOCK:
        s = CRAWL_STATUS.setdefault(crawl_id, {})
        s.update(kwargs)


def get_status(crawl_id: int) -> dict:
    with _STATUS_LOCK:
        return dict(CRAWL_STATUS.get(crawl_id, {}))


def normalize_url(url: str) -> str:
    """Strip fragment, normalize trailing slash, lowercase host."""
    url, _frag = urldefrag(url)
    p = urlparse(url)
    if not p.scheme or not p.netloc:
        return ''
    host = p.netloc.lower()
    path = p.path or '/'
    # quitar slash final excepto en raíz
    if len(path) > 1 and path.endswith('/'):
        path = path.rstrip('/')
    rebuilt = f'{p.scheme.lower()}://{host}{path}'
    if p.query:
        rebuilt += '?' + p.query
    return rebuilt


def _registered_domain(host: str) -> str:
    """www.example.com -> example.com (super simple, no usa publicsuffix)."""
    host = host.lower()
    if host.startswith('www.'):
        host = host[4:]
    return host


def _same_site(url: str, start_domain: str) -> bool:
    p = urlparse(url)
    return _registered_domain(p.netloc) == start_domain


def _is_html_candidate(url: str) -> bool:
    p = urlparse(url)
    path = p.path.lower()
    for ext in ASSET_EXT:
        if path.endswith(ext):
            return False
    return True


def _extract_internal_links(html: str, base_url: str, start_domain: str) -> list[str]:
    soup = BeautifulSoup(html, 'html.parser')
    out = []
    for a in soup.find_all('a', href=True):
        href = a['href'].strip()
        if not href or href.startswith(('#', 'javascript:', 'mailto:', 'tel:')):
            continue
        absu = urljoin(base_url, href)
        absu = normalize_url(absu)
        if not absu:
            continue
        if not _same_site(absu, start_domain):
            continue
        if not _is_html_candidate(absu):
            continue
        out.append(absu)
    return out


def _load_robots(start_url: str) -> robotparser.RobotFileParser | None:
    p = urlparse(start_url)
    robots_url = f'{p.scheme}://{p.netloc}/robots.txt'
    rp = robotparser.RobotFileParser()
    try:
        r = requests.get(robots_url, timeout=10, headers={'User-Agent': GOOGLEBOT_UA_MOBILE})
        if r.status_code == 200:
            rp.parse(r.text.splitlines())
            return rp
    except Exception:
        pass
    return None


def _audit_page(url: str, render_js: bool) -> dict:
    """Run fetch + (optional) render + audit on a single URL. Never raises."""
    out = {'url': url, 'error': None}
    try:
        raw = fetch_as_googlebot(url)
    except Exception as e:
        out['error'] = f'fetch: {e}'
        return out

    out['status_code'] = raw.get('status_code')
    out['fetch_ms'] = raw.get('elapsed_ms')
    out['size_bytes'] = raw.get('size_bytes')
    out['raw_html'] = raw.get('html', '')
    out['headers'] = raw.get('headers', {})
    out['final_url'] = raw.get('final_url', url)

    # Skip non-HTML responses
    ct = (raw.get('headers') or {}).get('Content-Type', '')
    if 'html' not in ct.lower():
        out['error'] = f'no es HTML (Content-Type: {ct})'
        return out

    rendered = None
    if render_js:
        try:
            rendered = render_with_playwright(url)
            out['rendered_html'] = rendered.get('html', '')
        except Exception as e:
            out['render_error'] = f'render: {e}'

    audit_res = audit(raw, rendered, url)
    out['audit'] = audit_res
    return out


# ── DB helpers (cada thread su conexión) ────────────────────────────────────

def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _save_page(conn: sqlite3.Connection, crawl_id: int, depth: int, parent: str | None, page: dict):
    audit_res = page.get('audit') or {}
    sig = audit_res.get('raw_signals') or {}
    counts = audit_res.get('counts') or {}
    conn.execute(
        '''INSERT INTO crawl_pages
        (crawl_id, url, depth, parent_url, status_code, fetch_ms, size_bytes,
         title, meta_description, h1_count, internal_links, external_links,
         score, critical_count, warning_count, improvement_count,
         findings_json, error)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
        (
            crawl_id, page['url'], depth, parent,
            page.get('status_code'), page.get('fetch_ms'), page.get('size_bytes'),
            sig.get('title', ''), sig.get('meta_description', ''),
            len(sig.get('h1', [])),
            sig.get('internal_links', 0), sig.get('external_links', 0),
            audit_res.get('score'),
            counts.get('critical', 0), counts.get('warning', 0), counts.get('improvement', 0),
            json.dumps(audit_res.get('findings', []), ensure_ascii=False) if audit_res else None,
            page.get('error'),
        ),
    )
    conn.commit()


# ── Crawl runner ────────────────────────────────────────────────────────────

def run_crawl(crawl_id: int, db_path: str, start_url: str,
              max_pages: int = 30, max_depth: int = 3,
              render_js: bool = False, respect_robots: bool = True,
              delay_s: float = 0.4):
    """Ejecuta crawl completo. Diseñado para correr en background thread."""
    start_url = normalize_url(start_url)
    start_domain = _registered_domain(urlparse(start_url).netloc)

    rp = _load_robots(start_url) if respect_robots else None

    conn = _connect(db_path)
    _set_status(crawl_id, status='running', done=0, failed=0, queue=1, current=start_url, total_seen=1)

    queue = deque([(start_url, 0, None)])  # (url, depth, parent)
    seen = {start_url}

    try:
        while queue and len([1 for _ in range(1)]) and True:  # mientras haya cola
            done_count = conn.execute(
                'SELECT COUNT(*) FROM crawl_pages WHERE crawl_id=?', (crawl_id,)
            ).fetchone()[0]
            if done_count >= max_pages:
                break
            if not queue:
                break

            url, depth, parent = queue.popleft()

            # robots
            if rp and not rp.can_fetch('Googlebot', url):
                _save_page(conn, crawl_id, depth, parent, {
                    'url': url, 'error': 'bloqueado por robots.txt',
                })
                _set_status(crawl_id, current=url, queue=len(queue))
                continue

            _set_status(crawl_id, current=url, queue=len(queue), depth=depth)

            page = _audit_page(url, render_js=render_js)
            _save_page(conn, crawl_id, depth, parent, page)

            if page.get('error'):
                conn.execute('UPDATE crawls SET pages_failed = pages_failed + 1 WHERE id=?', (crawl_id,))
            else:
                conn.execute('UPDATE crawls SET pages_done = pages_done + 1 WHERE id=?', (crawl_id,))
            conn.commit()

            # Descubrir links si está dentro de la profundidad
            if not page.get('error') and depth < max_depth and page.get('raw_html'):
                links = _extract_internal_links(page['raw_html'], page.get('final_url', url), start_domain)
                # SPA fallback: si raw tiene 0 enlaces, renderizar con JS para descubrirlos
                if not links and not render_js:
                    try:
                        rendered = render_with_playwright(url)
                        links = _extract_internal_links(rendered.get('html', ''), rendered.get('final_url', url), start_domain)
                        _set_status(crawl_id, last_spa_render=url)
                    except Exception:
                        pass
                elif not links and page.get('rendered_html'):
                    links = _extract_internal_links(page['rendered_html'], page.get('final_url', url), start_domain)
                for link in links:
                    if link not in seen and len(seen) < max_pages * 3:  # tope soft
                        seen.add(link)
                        queue.append((link, depth + 1, url))
            _set_status(crawl_id, total_seen=len(seen), queue=len(queue))

            time.sleep(delay_s)

        # Calcular avg_score
        avg = conn.execute(
            'SELECT AVG(score) FROM crawl_pages WHERE crawl_id=? AND score IS NOT NULL',
            (crawl_id,)
        ).fetchone()[0]

        # ── Duplicados cross-page (titles / meta / H1) ──
        duplicates = _detect_duplicates(conn, crawl_id)

        conn.execute(
            'UPDATE crawls SET status=?, finished_at=?, avg_score=?, duplicates_json=? WHERE id=?',
            ('completed', datetime.now().strftime('%Y-%m-%d %H:%M:%S'), avg,
             json.dumps(duplicates, ensure_ascii=False), crawl_id),
        )
        conn.commit()
        _set_status(crawl_id, status='completed', current=None, queue=0)
    except Exception as e:
        conn.execute(
            'UPDATE crawls SET status=?, finished_at=?, error=? WHERE id=?',
            ('error', datetime.now().strftime('%Y-%m-%d %H:%M:%S'), str(e)[:500], crawl_id),
        )
        conn.commit()
        _set_status(crawl_id, status='error', error=str(e)[:200])
    finally:
        conn.close()


def _detect_duplicates(conn: sqlite3.Connection, crawl_id: int) -> dict:
    """Detecta titles, meta descriptions y H1 duplicados entre páginas del crawl."""
    rows = conn.execute(
        'SELECT url, title, meta_description, h1_count FROM crawl_pages '
        'WHERE crawl_id=? AND error IS NULL', (crawl_id,)
    ).fetchall()

    def group_by(field_getter):
        groups = {}
        for r in rows:
            val = (field_getter(r) or '').strip()
            if not val:
                continue
            groups.setdefault(val, []).append(r['url'])
        return [{'value': v, 'urls': urls, 'count': len(urls)}
                for v, urls in groups.items() if len(urls) > 1]

    dup_titles = group_by(lambda r: r['title'])
    dup_descs = group_by(lambda r: r['meta_description'])

    # Páginas sin title / sin meta / sin h1
    missing_title = [r['url'] for r in rows if not (r['title'] or '').strip()]
    missing_desc  = [r['url'] for r in rows if not (r['meta_description'] or '').strip()]
    missing_h1    = [r['url'] for r in rows if not r['h1_count']]

    return {
        'duplicate_titles': sorted(dup_titles, key=lambda x: -x['count'])[:50],
        'duplicate_descriptions': sorted(dup_descs, key=lambda x: -x['count'])[:50],
        'missing_title': missing_title[:50],
        'missing_description': missing_desc[:50],
        'missing_h1': missing_h1[:50],
        'summary': {
            'duplicate_titles': len(dup_titles),
            'duplicate_descriptions': len(dup_descs),
            'missing_title': len(missing_title),
            'missing_description': len(missing_desc),
            'missing_h1': len(missing_h1),
        },
    }


def start_crawl_background(crawl_id: int, db_path: str, **kwargs) -> threading.Thread:
    t = threading.Thread(target=run_crawl, args=(crawl_id, db_path), kwargs=kwargs, daemon=True)
    t.start()
    return t
