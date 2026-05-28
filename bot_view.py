"""Googlebot view + SEO audit: fetch raw HTML as Googlebot, render with headless Chromium, and audit."""
import time
import json
import re
from urllib.parse import urlparse, urljoin

import requests
from bs4 import BeautifulSoup

GOOGLEBOT_UA_DESKTOP = (
    'Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; Googlebot/2.1; '
    '+http://www.google.com/bot.html) Chrome/W.X.Y.Z Safari/537.36'
)
GOOGLEBOT_UA_MOBILE = (
    'Mozilla/5.0 (Linux; Android 6.0.1; Nexus 5X Build/MMB29P) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/W.X.Y.Z Mobile Safari/537.36 (compatible; Googlebot/2.1; '
    '+http://www.google.com/bot.html)'
)

FETCH_TIMEOUT = 20
RENDER_TIMEOUT_MS = 25_000


def fetch_as_googlebot(url: str) -> dict:
    """Plain HTTP fetch with Googlebot UA — what the bot sees BEFORE running JS."""
    t0 = time.time()
    headers = {
        'User-Agent': GOOGLEBOT_UA_MOBILE,
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.5',
    }
    resp = requests.get(url, headers=headers, timeout=FETCH_TIMEOUT, allow_redirects=True)
    elapsed_ms = int((time.time() - t0) * 1000)
    return {
        'status_code': resp.status_code,
        'final_url': resp.url,
        'html': resp.text,
        'headers': dict(resp.headers),
        'elapsed_ms': elapsed_ms,
        'size_bytes': len(resp.content),
        'redirects': [r.url for r in resp.history],
    }


def render_with_playwright(url: str) -> dict:
    """Headless Chromium render with mobile Googlebot UA. Captures DOM after JS execution."""
    from playwright.sync_api import sync_playwright

    console_msgs = []
    failed_requests = []

    t0 = time.time()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            context = browser.new_context(
                user_agent=GOOGLEBOT_UA_MOBILE,
                viewport={'width': 412, 'height': 823},
            )
            page = context.new_page()

            page.on('console', lambda msg: console_msgs.append({
                'type': msg.type,
                'text': msg.text[:500],
            }))
            page.on('requestfailed', lambda req: failed_requests.append({
                'url': req.url[:300],
                'failure': (req.failure or '')[:200],
            }))

            resp = page.goto(url, wait_until='networkidle', timeout=RENDER_TIMEOUT_MS)
            status = resp.status if resp else None
            final_url = page.url
            html = page.content()
        finally:
            browser.close()

    elapsed_ms = int((time.time() - t0) * 1000)
    return {
        'status_code': status,
        'final_url': final_url,
        'html': html,
        'elapsed_ms': elapsed_ms,
        'size_bytes': len(html.encode('utf-8')),
        'console': console_msgs[:50],
        'errors': [c for c in console_msgs if c['type'] in ('error', 'warning')][:30],
        'failed_requests': failed_requests[:30],
    }


# ── Helpers for extracting SEO signals ───────────────────────────────────────

def _text_len(soup: BeautifulSoup) -> int:
    for tag in soup(['script', 'style', 'noscript']):
        tag.decompose()
    return len(soup.get_text(' ', strip=True))


_GENERIC_ANCHORS = {
    'click here', 'clique aqui', 'aqui', 'aquí', 'here', 'read more', 'leia mais',
    'saiba mais', 'ver mais', 'mais', 'more', 'link', 'this page', 'esta página',
    'continuar', 'continue', 'baixar', 'download', 'ver', 'detalhes', 'detalles',
}
_JSONLD_TYPE_RE = re.compile(r'"@type"\s*:\s*"([^"]+)"')


def _extract_jsonld_types(blocks: list) -> list[str]:
    types = []
    for b in blocks:
        if isinstance(b, dict):
            t = b.get('@type')
            if isinstance(t, str):
                types.append(t)
            elif isinstance(t, list):
                types.extend(x for x in t if isinstance(x, str))
            # @graph
            for g in (b.get('@graph') or []):
                if isinstance(g, dict) and isinstance(g.get('@type'), str):
                    types.append(g['@type'])
    return types


def _extract_signals(html: str, base_url: str) -> dict:
    soup = BeautifulSoup(html, 'lxml')

    title_tags = soup.find_all('title')
    title = title_tags[0].get_text(strip=True) if title_tags else ''

    desc_tags = soup.find_all('meta', attrs={'name': 'description'})
    meta_description = (desc_tags[0].get('content') or '').strip() if desc_tags else ''

    def meta(name):
        t = soup.find('meta', attrs={'name': name})
        return (t.get('content') or '').strip() if t else ''

    # charset
    charset = ''
    cs = soup.find('meta', attrs={'charset': True})
    if cs:
        charset = cs.get('charset', '')
    else:
        ct = soup.find('meta', attrs={'http-equiv': lambda v: v and v.lower() == 'content-type'})
        if ct:
            m = re.search(r'charset=([\w-]+)', ct.get('content', ''), re.I)
            charset = m.group(1) if m else ''

    canonical_tags = soup.find_all('link', attrs={'rel': 'canonical'})
    canonical = (canonical_tags[0].get('href') or '').strip() if canonical_tags else ''

    # Headings full
    headings = {f'h{i}': [h.get_text(' ', strip=True) for h in soup.find_all(f'h{i}')] for i in range(1, 7)}
    outline = []
    for tag in soup.find_all(['h1', 'h2', 'h3', 'h4', 'h5', 'h6']):
        outline.append((int(tag.name[1]), tag.get_text(' ', strip=True)[:80]))

    # Images
    imgs = soup.find_all('img')
    img_no_alt, img_no_dims = [], []
    for i in imgs:
        src = i.get('src', '') or i.get('data-src', '')
        if not (i.get('alt') or '').strip():
            img_no_alt.append(src)
        if not (i.get('width') and i.get('height')):
            img_no_dims.append(src)

    # hreflang
    hreflangs = []
    for l in soup.find_all('link', attrs={'rel': 'alternate'}):
        hl = l.get('hreflang')
        if hl:
            hreflangs.append({'hreflang': hl, 'href': l.get('href', '')})

    # OG + Twitter
    og, twitter = {}, {}
    for t in soup.find_all('meta'):
        prop = t.get('property', '')
        name = t.get('name', '')
        if prop.startswith('og:'):
            og[prop] = (t.get('content') or '').strip()
        elif prop.startswith('twitter:') or name.startswith('twitter:'):
            twitter[prop or name] = (t.get('content') or '').strip()

    # JSON-LD
    json_ld = []
    for s in soup.find_all('script', attrs={'type': 'application/ld+json'}):
        raw = s.string or s.get_text() or ''
        try:
            json_ld.append(json.loads(raw))
        except Exception:
            json_ld.append({'_parse_error': True, '_raw': raw[:300]})
    jsonld_types = _extract_jsonld_types(json_ld)

    # Microdata
    microdata_types = []
    for el in soup.find_all(attrs={'itemtype': True}):
        microdata_types.append(el.get('itemtype', ''))

    # Links (con detalle)
    host = urlparse(base_url).netloc
    internal, external = [], []
    empty_anchor, generic_anchor, nofollow_internal, blank_no_noopener = [], [], [], []
    for a in soup.find_all('a', href=True):
        href = a['href'].strip()
        if not href or href.startswith(('#', 'javascript:', 'mailto:', 'tel:')):
            continue
        absu = urljoin(base_url, href)
        anchor = a.get_text(' ', strip=True)
        rel = ' '.join(a.get('rel', [])) if a.get('rel') else ''
        is_internal = urlparse(absu).netloc == host
        (internal if is_internal else external).append(absu)
        if not anchor and not a.find('img'):
            empty_anchor.append(absu)
        elif anchor.lower() in _GENERIC_ANCHORS:
            generic_anchor.append({'url': absu, 'anchor': anchor})
        if 'nofollow' in rel.lower() and is_internal:
            nofollow_internal.append(absu)
        if a.get('target') == '_blank' and 'noopener' not in rel.lower() and 'noreferrer' not in rel.lower():
            blank_no_noopener.append(absu)

    # Recursos (para mixed content)
    resources = []
    for tag, attr in [('img', 'src'), ('script', 'src'), ('link', 'href'),
                      ('iframe', 'src'), ('source', 'src'), ('video', 'src')]:
        for el in soup.find_all(tag):
            v = el.get(attr)
            if v:
                resources.append(urljoin(base_url, v.strip()))

    # otros
    favicon = bool(soup.find('link', attrs={'rel': lambda v: v and 'icon' in (v if isinstance(v, str) else ' '.join(v)).lower()}))
    amp = bool(soup.find('link', attrs={'rel': 'amphtml'}))
    rel_next = soup.find('link', attrs={'rel': 'next'})
    rel_prev = soup.find('link', attrs={'rel': 'prev'})
    inline_styles = len(soup.find_all(style=True))
    text_len = _text_len(BeautifulSoup(html, 'lxml'))
    word_count = len(re.findall(r'\w+', BeautifulSoup(html, 'lxml').get_text(' ', strip=True)))
    html_len = len(html) or 1

    return {
        'title': title,
        'title_len': len(title),
        'title_count': len(title_tags),
        'meta_description': meta_description,
        'meta_description_count': len(desc_tags),
        'meta_robots': meta('robots'),
        'meta_viewport': meta('viewport'),
        'charset': charset,
        'canonical': canonical,
        'canonical_count': len(canonical_tags),
        'headings': headings,
        'outline': outline,
        'h1': headings['h1'],
        'h2_count': len(headings['h2']),
        'h3_count': len(headings['h3']),
        'img_count': len(imgs),
        'img_no_alt': img_no_alt[:10],
        'img_no_alt_count': len(img_no_alt),
        'img_no_dims_count': len(img_no_dims),
        'hreflang': hreflangs,
        'og': og,
        'twitter': twitter,
        'json_ld': json_ld,
        'jsonld_types': jsonld_types,
        'microdata_types': microdata_types,
        'internal_links': len(internal),
        'external_links': len(external),
        'internal_links_list': internal,
        'external_links_list': external,
        'empty_anchor': empty_anchor[:10],
        'empty_anchor_count': len(empty_anchor),
        'generic_anchor': generic_anchor[:10],
        'generic_anchor_count': len(generic_anchor),
        'nofollow_internal_count': len(nofollow_internal),
        'blank_no_noopener_count': len(blank_no_noopener),
        'resources': resources,
        'favicon': favicon,
        'amp': amp,
        'rel_next': rel_next.get('href') if rel_next else '',
        'rel_prev': rel_prev.get('href') if rel_prev else '',
        'inline_styles': inline_styles,
        'text_length': text_len,
        'word_count': word_count,
        'text_html_ratio': round(text_len / html_len * 100, 1),
        'lang': (soup.html.get('lang') if soup.html else '') or '',
    }


# ── Audit ────────────────────────────────────────────────────────────────────

# Severity: critical | warning | improvement | info

def _finding(severity, code, message, detail=None, category='general'):
    return {'severity': severity, 'code': code, 'message': message,
            'detail': detail, 'category': category}


def audit(raw_fetch: dict, rendered: dict, url: str, link_check: dict = None) -> dict:
    findings = []
    raw_html = raw_fetch.get('html', '')
    rendered_html = rendered.get('html', '') if rendered else ''

    raw_sig = _extract_signals(raw_html, raw_fetch.get('final_url', url))
    rendered_sig = _extract_signals(rendered_html, rendered.get('final_url', url)) if rendered else None

    headers = {k.lower(): v for k, v in (raw_fetch.get('headers') or {}).items()}
    final_url = raw_fetch.get('final_url', url)
    rendered_title = (rendered_sig or {}).get('title', '') if rendered_sig else ''
    rendered_md = (rendered_sig or {}).get('meta_description', '') if rendered_sig else ''

    F = findings.append

    # ══ INDEXABILITY ══════════════════════════════════════════════════════════
    sc = raw_fetch.get('status_code')
    if sc and sc >= 500:
        F(_finding('critical', 'http_5xx', f'Error de servidor HTTP {sc}', {'status': sc}, 'indexability'))
    elif sc and sc >= 400:
        F(_finding('critical', 'http_4xx', f'HTTP {sc} — Googlebot no podrá indexar', {'status': sc}, 'indexability'))
    elif sc and 300 <= sc < 400:
        F(_finding('warning', 'http_redirect', f'Respuesta {sc} — la URL redirige', {'status': sc, 'final': final_url}, 'indexability'))

    robots = (raw_sig['meta_robots'] or '').lower()
    x_robots = headers.get('x-robots-tag', '')
    if 'noindex' in robots:
        F(_finding('critical', 'noindex', 'Meta robots = noindex — Google no la indexará', {'robots': robots}, 'indexability'))
    if 'noindex' in x_robots.lower():
        F(_finding('critical', 'x_robots_noindex', 'Header X-Robots-Tag contiene noindex', {'header': x_robots}, 'indexability'))
    if 'nofollow' in robots:
        F(_finding('warning', 'meta_nofollow', 'Meta robots = nofollow — no se sigue ningún enlace', {'robots': robots}, 'indexability'))

    # Canonical
    if raw_sig['canonical_count'] > 1:
        F(_finding('warning', 'multiple_canonical', f'{raw_sig["canonical_count"]} etiquetas canonical — Google ignorará todas', None, 'indexability'))
    if not raw_sig['canonical']:
        F(_finding('improvement', 'no_canonical', 'Falta link rel="canonical"', None, 'indexability'))
    elif raw_sig['canonical'] not in (url, final_url):
        F(_finding('info', 'canonical_different', 'Canonical apunta a otra URL', {'canonical': raw_sig['canonical'], 'requested': url}, 'indexability'))

    # ══ CONTENT (title / meta / content) ══════════════════════════════════════
    if raw_sig['title_count'] > 1:
        F(_finding('warning', 'multiple_title', f'{raw_sig["title_count"]} etiquetas <title>', None, 'content'))
    if not raw_sig['title']:
        if not rendered_title:
            F(_finding('critical', 'no_title', 'Falta etiqueta <title>', None, 'content'))
    else:
        tl = raw_sig['title_len']
        if tl < 20:
            F(_finding('warning', 'title_short', f'Title corto ({tl} car.) — recomendado 30-60', {'title': raw_sig['title']}, 'content'))
        elif tl > 65:
            F(_finding('warning', 'title_long', f'Title largo ({tl} car.) — se cortará en SERP', {'title': raw_sig['title']}, 'content'))
        if raw_sig['h1'] and raw_sig['title'].strip().lower() == raw_sig['h1'][0].strip().lower():
            F(_finding('improvement', 'title_equals_h1', 'Title idéntico al H1 — diferéncialos', None, 'content'))

    if raw_sig['meta_description_count'] > 1:
        F(_finding('warning', 'multiple_meta_desc', f'{raw_sig["meta_description_count"]} meta descriptions', None, 'content'))
    md = raw_sig['meta_description']
    if not md:
        if rendered_md:
            F(_finding('warning', 'meta_description_js_only', 'Meta description solo aparece tras JS', {'rendered': rendered_md[:200]}, 'content'))
        else:
            F(_finding('warning', 'no_meta_description', 'Falta meta description', None, 'content'))
    else:
        ml = len(md)
        if ml < 70:
            F(_finding('improvement', 'meta_description_short', f'Meta description corta ({ml} car.) — ideal 120-160', None, 'content'))
        elif ml > 165:
            F(_finding('improvement', 'meta_description_long', f'Meta description larga ({ml} car.) — se cortará', None, 'content'))

    wc = raw_sig['word_count']
    if wc < 100 and not (rendered_sig and rendered_sig['word_count'] >= 100):
        F(_finding('warning', 'thin_content', f'Contenido pobre ({wc} palabras) — Google puede verlo como thin content', {'words': wc}, 'content'))
    if raw_sig['text_html_ratio'] < 5:
        F(_finding('improvement', 'low_text_ratio', f'Ratio texto/HTML bajo ({raw_sig["text_html_ratio"]}%) — mucho código, poco contenido', None, 'content'))

    # ══ HEADINGS ══════════════════════════════════════════════════════════════
    h1c = len(raw_sig['h1'])
    if h1c == 0:
        if not (rendered_sig and rendered_sig['h1']):
            F(_finding('warning', 'no_h1', 'Falta H1', None, 'headings'))
    elif h1c > 1:
        F(_finding('improvement', 'multiple_h1', f'{h1c} etiquetas H1 — usa solo una', {'h1s': raw_sig['h1'][:5]}, 'headings'))
    if h1c and len(raw_sig['h1'][0]) > 70:
        F(_finding('improvement', 'h1_long', f'H1 muy largo ({len(raw_sig["h1"][0])} car.)', None, 'headings'))
    if raw_sig['h2_count'] == 0 and wc > 300:
        F(_finding('improvement', 'no_h2', 'Sin H2 en contenido extenso — mejora la estructura', None, 'headings'))
    # Jerarquía saltada (H1 → H3 sin H2, etc.)
    prev = 0
    for lvl, _txt in raw_sig['outline']:
        if prev and lvl > prev + 1:
            F(_finding('improvement', 'heading_skip', f'Salto de jerarquía: H{prev} → H{lvl}', None, 'headings'))
            break
        prev = lvl

    # ══ IMAGES ════════════════════════════════════════════════════════════════
    if raw_sig['img_no_alt_count'] > 0:
        sev = 'warning' if raw_sig['img_no_alt_count'] >= 5 else 'improvement'
        F(_finding(sev, 'images_no_alt', f'{raw_sig["img_no_alt_count"]}/{raw_sig["img_count"]} imágenes sin alt', {'samples': raw_sig['img_no_alt']}, 'images'))
    if raw_sig['img_no_dims_count'] > 0:
        F(_finding('improvement', 'images_no_dims', f'{raw_sig["img_no_dims_count"]} imágenes sin width/height — causan layout shift (CLS)', None, 'images'))

    # ══ LINKS ═════════════════════════════════════════════════════════════════
    if raw_sig['internal_links'] == 0 and not (rendered_sig and rendered_sig['internal_links'] > 0):
        F(_finding('warning', 'no_internal_links', 'Sin enlaces internos — página huérfana de navegación', None, 'links'))
    if raw_sig['empty_anchor_count'] > 0:
        F(_finding('improvement', 'empty_anchor', f'{raw_sig["empty_anchor_count"]} enlaces sin texto ancla', {'samples': raw_sig['empty_anchor']}, 'links'))
    if raw_sig['generic_anchor_count'] > 0:
        F(_finding('improvement', 'generic_anchor', f'{raw_sig["generic_anchor_count"]} enlaces con anchor genérico ("clique aqui", etc.)', {'samples': raw_sig['generic_anchor']}, 'links'))
    if raw_sig['blank_no_noopener_count'] > 0:
        F(_finding('improvement', 'blank_no_noopener', f'{raw_sig["blank_no_noopener_count"]} enlaces target="_blank" sin rel="noopener" (seguridad)', None, 'links'))
    # Enlaces rotos (si se pasó link_check)
    if link_check:
        broken = link_check.get('broken', [])
        redirected = link_check.get('redirected', [])
        if broken:
            F(_finding('critical', 'broken_links', f'{len(broken)} enlaces rotos (4xx/5xx)', {'samples': broken[:15]}, 'links'))
        if redirected:
            F(_finding('improvement', 'redirected_links', f'{len(redirected)} enlaces apuntan a redirecciones', {'samples': redirected[:15]}, 'links'))

    # ══ INTERNATIONAL ═════════════════════════════════════════════════════════
    if not raw_sig['lang']:
        F(_finding('improvement', 'no_lang', 'Falta atributo lang en <html>', None, 'international'))
    for hl in raw_sig['hreflang']:
        code = hl['hreflang']
        if code != 'x-default' and not re.match(r'^[a-z]{2}(-[A-Za-z]{2})?$', code):
            F(_finding('warning', 'hreflang_invalid', f'Código hreflang inválido: "{code}"', None, 'international'))
            break

    # ══ SOCIAL (Open Graph + Twitter) ═════════════════════════════════════════
    for og_key in ('og:title', 'og:description', 'og:image'):
        raw_og = raw_sig['og']; rendered_og = (rendered_sig or {}).get('og', {})
        in_raw, raw_val = og_key in raw_og, raw_og.get(og_key, '')
        rendered_val = rendered_og.get(og_key, '')
        code = og_key.replace(':', '_')
        if in_raw and raw_val:
            pass
        elif in_raw and not raw_val:
            F(_finding('warning', f'{code}_empty', f'{og_key} existe pero vacío', None, 'social'))
        elif (not in_raw) and rendered_val:
            F(_finding('warning', f'{code}_js_only', f'{og_key} solo se inyecta por JS (Facebook/WhatsApp/Twitter no lo verán)', {'rendered_value': rendered_val[:200]}, 'social'))
        else:
            F(_finding('improvement', f'no_{code}', f'Falta {og_key}', None, 'social'))
    if not raw_sig['twitter']:
        F(_finding('improvement', 'no_twitter_card', 'Sin Twitter Card — previews pobres al compartir en X/Twitter', None, 'social'))

    # ══ STRUCTURED DATA ═══════════════════════════════════════════════════════
    raw_jsonld = raw_sig['json_ld']
    rendered_jsonld = (rendered_sig or {}).get('json_ld', []) if rendered_sig else []
    if not raw_jsonld and not rendered_jsonld and not raw_sig['microdata_types']:
        F(_finding('improvement', 'no_structured_data', 'Sin datos estructurados (JSON-LD ni microdata) — pierdes rich results', None, 'structured_data'))
    elif not raw_jsonld and rendered_jsonld:
        F(_finding('warning', 'structured_data_js_only', f'JSON-LD ({len(rendered_jsonld)} bloques) solo aparece tras JS', {'types': _extract_jsonld_types(rendered_jsonld)}, 'structured_data'))
    for jl in raw_jsonld:
        if isinstance(jl, dict) and jl.get('_parse_error'):
            F(_finding('warning', 'json_ld_invalid', 'Bloque JSON-LD con sintaxis inválida', {'raw': jl.get('_raw', '')[:200]}, 'structured_data'))

    # ══ SECURITY ══════════════════════════════════════════════════════════════
    is_https = final_url.startswith('https://')
    if not is_https:
        F(_finding('critical', 'no_https', 'La página no usa HTTPS', None, 'security'))
    else:
        mixed = [r for r in raw_sig['resources'] if r.startswith('http://')]
        if mixed:
            F(_finding('critical', 'mixed_content', f'{len(mixed)} recursos cargados por HTTP en página HTTPS (mixed content)', {'samples': mixed[:10]}, 'security'))
    if is_https and not headers.get('strict-transport-security'):
        F(_finding('improvement', 'no_hsts', 'Falta header Strict-Transport-Security (HSTS)', None, 'security'))
    if not headers.get('x-content-type-options'):
        F(_finding('improvement', 'no_xcto', 'Falta header X-Content-Type-Options: nosniff', None, 'security'))
    if not headers.get('content-security-policy'):
        F(_finding('info', 'no_csp', 'Sin Content-Security-Policy', None, 'security'))

    # ══ PERFORMANCE ═══════════════════════════════════════════════════════════
    raw_ms = raw_fetch.get('elapsed_ms', 0)
    if raw_ms > 3000:
        F(_finding('warning', 'slow_response', f'Respuesta lenta ({raw_ms} ms) — afecta crawl budget', None, 'performance'))
    elif raw_ms > 1500:
        F(_finding('improvement', 'medium_response', f'Respuesta algo lenta ({raw_ms} ms)', None, 'performance'))
    raw_size_kb = raw_fetch.get('size_bytes', 0) / 1024
    if raw_size_kb > 500:
        F(_finding('improvement', 'large_html', f'HTML grande ({raw_size_kb:.0f} KB)', None, 'performance'))
    if not headers.get('content-encoding'):
        F(_finding('improvement', 'no_compression', 'Sin compresión (gzip/brotli) en la respuesta', None, 'performance'))
    if len(raw_sig['resources']) > 100:
        F(_finding('improvement', 'many_resources', f'{len(raw_sig["resources"])} recursos en la página', None, 'performance'))
    if not raw_sig['meta_viewport']:
        F(_finding('warning', 'no_viewport', 'Falta meta viewport — afecta mobile-first indexing', None, 'performance'))
    if not raw_sig['favicon']:
        F(_finding('info', 'no_favicon', 'Sin favicon', None, 'performance'))

    # ══ URL ═══════════════════════════════════════════════════════════════════
    parsed = urlparse(final_url)
    path = parsed.path
    if len(final_url) > 115:
        F(_finding('improvement', 'url_long', f'URL larga ({len(final_url)} car.)', None, 'url'))
    if re.search(r'[A-Z]', path):
        F(_finding('improvement', 'url_uppercase', 'URL con mayúsculas — pueden causar duplicados', None, 'url'))
    if '_' in path:
        F(_finding('info', 'url_underscore', 'URL con guiones bajos — Google prefiere guiones medios', None, 'url'))
    if parsed.query and len(parsed.query.split('&')) > 3:
        F(_finding('improvement', 'url_many_params', f'URL con {len(parsed.query.split("&"))} parámetros', None, 'url'))

    # ══ RENDERING (JS) ════════════════════════════════════════════════════════
    js_only_findings = []
    if rendered_sig:
        if not raw_sig['title'] and rendered_sig['title']:
            js_only_findings.append(_finding('critical', 'title_js_only', 'El <title> solo aparece tras ejecutar JS', {'rendered_title': rendered_sig['title']}, 'rendering'))
        if not raw_sig['h1'] and rendered_sig['h1']:
            js_only_findings.append(_finding('critical', 'h1_js_only', 'El H1 solo aparece tras ejecutar JS', {'rendered_h1': rendered_sig['h1'][:3]}, 'rendering'))
        delta = rendered_sig['text_length'] - raw_sig['text_length']
        if raw_sig['text_length'] == 0 and rendered_sig['text_length'] > 100:
            js_only_findings.append(_finding('critical', 'content_js_only', f'HTML crudo sin contenido. Todo el texto ({rendered_sig["text_length"]} car.) viene de JS — SPA sin SSR', None, 'rendering'))
        elif raw_sig['text_length'] > 0 and delta > raw_sig['text_length'] * 2 and delta > 1000:
            js_only_findings.append(_finding('warning', 'content_js_heavy', f'La mayor parte del contenido depende de JS ({rendered_sig["text_length"]} vs {raw_sig["text_length"]} car.)', None, 'rendering'))
        link_delta = rendered_sig['internal_links'] - raw_sig['internal_links']
        if link_delta > 20 and raw_sig['internal_links'] < 5:
            js_only_findings.append(_finding('warning', 'links_js_only', f'{link_delta} enlaces internos solo existen tras JS', None, 'rendering'))
        errs = rendered.get('errors', []) if rendered else []
        if errs:
            F(_finding('warning', 'console_errors', f'{len(errs)} errores/warnings en consola al renderizar', {'samples': errs[:5]}, 'rendering'))
        fails = rendered.get('failed_requests', []) if rendered else []
        if fails:
            F(_finding('warning', 'failed_requests', f'{len(fails)} recursos fallaron al cargar', {'samples': fails[:5]}, 'rendering'))

    findings = js_only_findings + findings

    # ══ Core Web Vitals (si se pasaron) ═══════════════════════════════════════
    cwv = raw_fetch.get('pagespeed')
    if cwv and not cwv.get('error'):
        for metric, label, good, poor in [
            ('lcp', 'LCP', 2.5, 4.0), ('cls', 'CLS', 0.1, 0.25), ('inp', 'INP', 200, 500),
        ]:
            val = cwv.get(metric)
            if val is None:
                continue
            if val > poor:
                F(_finding('warning', f'cwv_{metric}_poor', f'{label} pobre: {val} (umbral bueno ≤ {good})', None, 'performance'))
            elif val > good:
                F(_finding('improvement', f'cwv_{metric}_avg', f'{label} mejorable: {val} (bueno ≤ {good})', None, 'performance'))

    # ── Score ──
    weights = {'critical': 20, 'warning': 6, 'improvement': 1.5, 'info': 0}
    deduction = sum(weights.get(f['severity'], 0) for f in findings)
    score = max(0, round(100 - deduction))

    counts = {sev: sum(1 for f in findings if f['severity'] == sev)
              for sev in ('critical', 'warning', 'improvement', 'info')}
    cat_counts = {}
    for f in findings:
        cat_counts[f['category']] = cat_counts.get(f['category'], 0) + 1

    return {
        'findings': findings,
        'counts': counts,
        'cat_counts': cat_counts,
        'score': score,
        'raw_signals': raw_sig,
        'rendered_signals': rendered_sig,
    }


def check_links(urls: list[str], max_workers: int = 12, timeout: int = 10) -> dict:
    """Comprueba estado HTTP de una lista de URLs. Devuelve broken / redirected / ok."""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    urls = list(dict.fromkeys(urls))  # dedupe preservando orden
    broken, redirected, ok = [], [], []
    headers = {'User-Agent': GOOGLEBOT_UA_MOBILE}

    def _check(u):
        try:
            r = requests.head(u, headers=headers, timeout=timeout, allow_redirects=False)
            # algunos servidores no soportan HEAD -> reintentar GET
            if r.status_code in (405, 403, 501):
                r = requests.get(u, headers=headers, timeout=timeout, allow_redirects=False, stream=True)
            return u, r.status_code, r.headers.get('Location', '')
        except Exception as e:
            return u, None, str(e)[:80]

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = [ex.submit(_check, u) for u in urls]
        for fut in as_completed(futures):
            u, code, extra = fut.result()
            if code is None:
                broken.append({'url': u, 'status': 'error', 'detail': extra})
            elif code >= 400:
                broken.append({'url': u, 'status': code})
            elif 300 <= code < 400:
                redirected.append({'url': u, 'status': code, 'to': extra})
            else:
                ok.append(u)
    return {'broken': broken, 'redirected': redirected, 'ok_count': len(ok), 'checked': len(urls)}


def analyze_url(url: str, render: bool = True, do_link_check: bool = True,
                pagespeed_key: str = None) -> dict:
    """Top-level entry: fetch, optionally render, link-check, pagespeed, audit."""
    if not re.match(r'^https?://', url, re.I):
        url = 'https://' + url

    error = None
    raw = None
    rendered = None
    try:
        raw = fetch_as_googlebot(url)
    except Exception as e:
        error = f'Error en fetch: {e}'
        return {'error': error, 'url': url}

    if render:
        try:
            rendered = render_with_playwright(url)
        except Exception as e:
            error = f'Error en render (continuamos sin él): {e}'

    # Link check: usar enlaces del HTML renderizado si existe (más completo en SPAs)
    link_check = None
    if do_link_check:
        try:
            sig_for_links = _extract_signals(
                (rendered or raw).get('html', ''), (rendered or raw).get('final_url', url))
            all_links = (sig_for_links['internal_links_list'] + sig_for_links['external_links_list'])[:150]
            if all_links:
                link_check = check_links(all_links)
        except Exception:
            link_check = None

    # PageSpeed / Core Web Vitals
    if pagespeed_key:
        try:
            from pagespeed import fetch_pagespeed
            raw['pagespeed'] = fetch_pagespeed(url, pagespeed_key)
        except Exception:
            pass

    audit_res = audit(raw, rendered, url, link_check=link_check)

    return {
        'url': url,
        'raw': raw,
        'rendered': rendered,
        'audit': audit_res,
        'link_check': link_check,
        'error': error,
    }
