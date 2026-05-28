"""Instagram Graph API — perfil propio, media, business discovery (competidores), hashtags."""
import re
import json
from collections import Counter

import requests

GRAPH = 'https://graph.facebook.com/v21.0'
FB_OAUTH = 'https://www.facebook.com/v21.0/dialog/oauth'

# Scopes necesarios para IG Graph API (cuenta Business/Creator vinculada a una página FB)
IG_SCOPES = [
    'instagram_basic',
    'pages_show_list',
    'instagram_manage_insights',
    'pages_read_engagement',
    'pages_read_user_content',   # leer posts de páginas (Facebook)
    'business_management',
]

_HASHTAG_RE = re.compile(r'#(\w+)', re.UNICODE)


# ── OAuth ────────────────────────────────────────────────────────────────────

def auth_url(app_id: str, redirect_uri: str, state: str) -> str:
    from urllib.parse import urlencode
    params = {
        'client_id': app_id,
        'redirect_uri': redirect_uri,
        'state': state,
        'response_type': 'code',
        'scope': ','.join(IG_SCOPES),
    }
    return f'{FB_OAUTH}?{urlencode(params)}'


def exchange_code(app_id: str, app_secret: str, redirect_uri: str, code: str) -> dict:
    """code -> short-lived token -> long-lived token (60 días)."""
    r = requests.get(f'{GRAPH}/oauth/access_token', params={
        'client_id': app_id,
        'client_secret': app_secret,
        'redirect_uri': redirect_uri,
        'code': code,
    }, timeout=30)
    data = r.json()
    if 'access_token' not in data:
        raise RuntimeError(data.get('error', {}).get('message', 'No se obtuvo token'))
    short = data['access_token']
    # long-lived
    r2 = requests.get(f'{GRAPH}/oauth/access_token', params={
        'grant_type': 'fb_exchange_token',
        'client_id': app_id,
        'client_secret': app_secret,
        'fb_exchange_token': short,
    }, timeout=30)
    d2 = r2.json()
    return {
        'access_token': d2.get('access_token', short),
        'expires_in': d2.get('expires_in'),
    }


def discover_ig_accounts(access_token: str) -> list[dict]:
    """Lista las cuentas IG Business vinculadas a las páginas FB del usuario."""
    r = requests.get(f'{GRAPH}/me/accounts', params={
        'fields': 'name,instagram_business_account{id,username,name,profile_picture_url,followers_count}',
        'access_token': access_token,
        'limit': 50,
    }, timeout=30)
    data = r.json()
    if 'error' in data:
        raise RuntimeError(data['error'].get('message'))
    out = []
    for page in data.get('data', []):
        iga = page.get('instagram_business_account')
        if iga:
            out.append({
                'page_name': page.get('name'),
                'ig_id': iga.get('id'),
                'username': iga.get('username'),
                'name': iga.get('name'),
                'profile_picture_url': iga.get('profile_picture_url'),
                'followers_count': iga.get('followers_count'),
            })
    return out


# ── Datos de la cuenta propia ────────────────────────────────────────────────

def get_profile(ig_id: str, access_token: str) -> dict:
    r = requests.get(f'{GRAPH}/{ig_id}', params={
        'fields': 'username,name,biography,followers_count,follows_count,media_count,profile_picture_url,website',
        'access_token': access_token,
    }, timeout=30)
    data = r.json()
    if 'error' in data:
        raise RuntimeError(data['error'].get('message'))
    return data


def get_media(ig_id: str, access_token: str, limit: int = 50) -> list[dict]:
    r = requests.get(f'{GRAPH}/{ig_id}/media', params={
        'fields': 'id,caption,media_type,like_count,comments_count,timestamp,permalink,media_url,thumbnail_url',
        'access_token': access_token,
        'limit': limit,
    }, timeout=30)
    data = r.json()
    if 'error' in data:
        raise RuntimeError(data['error'].get('message'))
    return data.get('data', [])


# ── Business Discovery (competidores: cualquier cuenta Business/Creator pública) ─

def business_discovery(ig_id: str, access_token: str, target_username: str, media_limit: int = 30) -> dict:
    """Datos públicos de otra cuenta Business/Creator por username."""
    fields = (
        f'business_discovery.username({target_username})'
        '{username,name,biography,followers_count,follows_count,media_count,profile_picture_url,'
        'media.limit(' + str(media_limit) + '){caption,media_type,like_count,comments_count,timestamp,permalink,media_url,thumbnail_url}}'
    )
    r = requests.get(f'{GRAPH}/{ig_id}', params={
        'fields': fields,
        'access_token': access_token,
    }, timeout=30)
    data = r.json()
    if 'error' in data:
        raise RuntimeError(data['error'].get('message', 'Error en business discovery'))
    bd = data.get('business_discovery')
    if not bd:
        raise RuntimeError('No se encontró la cuenta (¿es Business/Creator y pública?)')
    return bd


# ── Análisis ─────────────────────────────────────────────────────────────────

def compute_engagement_rate(posts: list[dict], followers: int) -> float:
    """ER medio = (likes+comments promedio por post) / followers * 100."""
    if not posts or not followers:
        return 0.0
    total = sum((p.get('like_count') or 0) + (p.get('comments_count') or 0) for p in posts)
    avg_per_post = total / len(posts)
    return round(avg_per_post / followers * 100, 2)


def extract_hashtags(posts: list[dict], top: int = 30) -> list[dict]:
    counter = Counter()
    for p in posts:
        for tag in _HASHTAG_RE.findall(p.get('caption') or ''):
            counter[tag.lower()] += 1
    return [{'tag': t, 'count': c} for t, c in counter.most_common(top)]


def posting_stats(posts: list[dict]) -> dict:
    """Frecuencia de publicación, tipos de contenido, mejores horarios."""
    from datetime import datetime
    types = Counter()
    hours = Counter()
    weekdays = Counter()
    dates = []
    for p in posts:
        types[p.get('media_type') or 'UNKNOWN'] += 1
        ts = p.get('timestamp')
        if ts:
            try:
                dt = datetime.fromisoformat(ts.replace('+0000', '+00:00'))
                hours[dt.hour] += 1
                weekdays[dt.weekday()] += 1
                dates.append(dt)
            except Exception:
                pass
    freq_per_week = None
    if len(dates) >= 2:
        dates.sort()
        span_days = (dates[-1] - dates[0]).days or 1
        freq_per_week = round(len(dates) / span_days * 7, 1)
    return {
        'types': dict(types),
        'top_hours': hours.most_common(5),
        'top_weekdays': weekdays.most_common(7),
        'freq_per_week': freq_per_week,
    }


def top_posts(posts: list[dict], n: int = 9) -> list[dict]:
    def eng(p):
        return (p.get('like_count') or 0) + (p.get('comments_count') or 0)
    return sorted(posts, key=eng, reverse=True)[:n]


# ── Análisis avanzado (cross-platform) ──────────────────────────────────────

_WEEKDAYS = ['Lun', 'Mar', 'Mié', 'Jue', 'Vie', 'Sáb', 'Dom']

_STOPWORDS = set('''
a al algo algunas algunos ante antes como con contra cual cuando de del desde donde dos el ella ellas
ellos en entre era erais eran es esa esas ese eso esos esta estas este esto estos ha hace hacia hasta hay
la las le les lo los mas más me mi mis mucho muy nada ni no nos nuestra nuestro o os otra otro para pero
poco por porque que quien se sea ser si sin sobre solo son su sus te tu tus un una uno unos vos y ya
o a as os um uma uns umas do da dos das no na nos nas pra para com sem mais muito você voce seu sua
the and for you your with this that are our from have has was will can all out get more your們
de e que não com uma por mais para
http https www com br t co amp
'''.split())

_EMOJI_RE = re.compile(
    '[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F000-\U0001F02F\U0001F900-\U0001F9FF✀-➿]',
    flags=re.UNICODE
)


def _eng(p):
    return (p.get('like_count') or 0) + (p.get('comments_count') or 0)


def best_time_analysis(posts: list[dict]) -> dict:
    """Heatmap día×hora (frecuencia) + mejores horas/días por engagement medio."""
    from datetime import datetime
    grid = [[0] * 24 for _ in range(7)]            # cuenta de posts
    grid_eng = [[0.0] * 24 for _ in range(7)]      # engagement acumulado
    hour_eng, hour_cnt = {}, {}
    wd_eng, wd_cnt = {}, {}
    for p in posts:
        ts = p.get('timestamp')
        if not ts:
            continue
        try:
            dt = datetime.fromisoformat(ts.replace('Z', '+00:00').replace('+0000', '+00:00'))
        except Exception:
            continue
        wd, hr = dt.weekday(), dt.hour
        e = _eng(p)
        grid[wd][hr] += 1
        grid_eng[wd][hr] += e
        hour_eng[hr] = hour_eng.get(hr, 0) + e
        hour_cnt[hr] = hour_cnt.get(hr, 0) + 1
        wd_eng[wd] = wd_eng.get(wd, 0) + e
        wd_cnt[wd] = wd_cnt.get(wd, 0) + 1

    best_hours = sorted(
        [{'hour': h, 'avg_eng': round(hour_eng[h] / hour_cnt[h]), 'posts': hour_cnt[h]} for h in hour_cnt],
        key=lambda x: -x['avg_eng'])[:5]
    best_weekdays = sorted(
        [{'weekday': _WEEKDAYS[w], 'avg_eng': round(wd_eng[w] / wd_cnt[w]), 'posts': wd_cnt[w]} for w in wd_cnt],
        key=lambda x: -x['avg_eng'])
    max_cell = max((max(row) for row in grid), default=0)
    return {
        'grid': grid, 'max_cell': max_cell, 'weekdays': _WEEKDAYS,
        'best_hours': best_hours, 'best_weekdays': best_weekdays,
    }


def engagement_by_type(posts: list[dict]) -> list[dict]:
    from collections import defaultdict
    agg = defaultdict(lambda: {'count': 0, 'likes': 0, 'comments': 0})
    for p in posts:
        t = p.get('media_type') or 'UNKNOWN'
        agg[t]['count'] += 1
        agg[t]['likes'] += p.get('like_count') or 0
        agg[t]['comments'] += p.get('comments_count') or 0
    out = []
    for t, d in agg.items():
        n = d['count'] or 1
        out.append({
            'type': t, 'count': d['count'],
            'avg_likes': round(d['likes'] / n),
            'avg_comments': round(d['comments'] / n),
            'avg_eng': round((d['likes'] + d['comments']) / n),
        })
    return sorted(out, key=lambda x: -x['avg_eng'])


def caption_topics(posts: list[dict], top: int = 25) -> dict:
    """Palabras más frecuentes (sin stopwords/hashtags), emojis, longitud vs engagement."""
    word_counter = Counter()
    emoji_counter = Counter()
    lengths = []
    buckets = {'Corta (<80)': [], 'Media (80-250)': [], 'Larga (>250)': []}
    for p in posts:
        cap = p.get('caption') or ''
        # quitar hashtags y menciones para topics
        clean = re.sub(r'[#@]\w+', '', cap)
        for w in re.findall(r'\b[a-záéíóúñãõçü]{3,}\b', clean.lower()):
            if w not in _STOPWORDS:
                word_counter[w] += 1
        for em in _EMOJI_RE.findall(cap):
            emoji_counter[em] += 1
        L = len(cap)
        lengths.append(L)
        e = _eng(p)
        if L < 80:
            buckets['Corta (<80)'].append(e)
        elif L <= 250:
            buckets['Media (80-250)'].append(e)
        else:
            buckets['Larga (>250)'].append(e)
    length_vs_eng = [
        {'bucket': k, 'avg_eng': round(sum(v) / len(v)) if v else 0, 'posts': len(v)}
        for k, v in buckets.items()
    ]
    return {
        'top_words': [{'word': w, 'count': c} for w, c in word_counter.most_common(top)],
        'top_emojis': [{'emoji': e, 'count': c} for e, c in emoji_counter.most_common(12)],
        'avg_length': round(sum(lengths) / len(lengths)) if lengths else 0,
        'length_vs_eng': length_vs_eng,
    }


def generate_recommendations(posts: list[dict]) -> dict:
    """Plan de contenido accionable basado en los posts que MÁS engagement generaron."""
    from datetime import datetime
    import statistics

    valid = [p for p in posts if p.get('timestamp')]
    if len(valid) < 5:
        return {'enough_data': False, 'min_needed': 5, 'have': len(valid)}

    engs = [_eng(p) for p in valid]
    median_eng = statistics.median(engs)
    avg_eng = round(sum(engs) / len(engs))
    # top performers: por encima de la mediana (mínimo 3)
    top = [p for p in valid if _eng(p) >= median_eng]
    if len(top) < 3:
        top = sorted(valid, key=_eng, reverse=True)[:max(3, len(valid)//3)]

    recs = []          # recomendaciones priorizadas
    schedule = []      # franjas recomendadas para el calendario

    # ── 1) Mejores franjas (de los top performers) ──
    slot_eng = {}      # (weekday, hour) -> [engs]
    for p in top:
        try:
            dt = datetime.fromisoformat(p['timestamp'].replace('Z', '+00:00').replace('+0000', '+00:00'))
        except Exception:
            continue
        slot_eng.setdefault((dt.weekday(), dt.hour), []).append(_eng(p))
    ranked_slots = sorted(
        [{'weekday': wd, 'hour': h, 'avg_eng': round(sum(v) / len(v)), 'n': len(v)}
         for (wd, h), v in slot_eng.items()],
        key=lambda x: -x['avg_eng'])
    for s in ranked_slots[:3]:
        schedule.append({'day': _WEEKDAYS[s['weekday']], 'hour': s['hour'], 'avg_eng': s['avg_eng']})
    if schedule:
        slots_txt = ', '.join(f"{s['day']} {s['hour']:02d}:00" for s in schedule)
        recs.append({
            'priority': 'high', 'icon': 'bi-clock-fill', 'category': 'Horarios',
            'title': f'Publica en tus franjas de mayor engagement',
            'detail': f'Tus mejores publicaciones salieron en: {slots_txt}. Prioriza esas franjas.',
            'evidence': f'{len(top)} posts top analizados',
        })

    # ── 2) Formato que mejor rinde ──
    by_type = engagement_by_type(valid)
    if len(by_type) > 1 and by_type[0]['count'] >= 2:
        best = by_type[0]; worst = by_type[-1]
        mult = round(best['avg_eng'] / worst['avg_eng'], 1) if worst['avg_eng'] else None
        detail = f"{best['type']} es tu formato más fuerte ({best['avg_eng']:,} eng. medio)".replace(',', '.')
        if mult and mult >= 1.3:
            detail += f" — {mult}× más que {worst['type']}."
        recs.append({'priority': 'high', 'icon': 'bi-collection-play-fill', 'category': 'Formato',
                     'title': f'Prioriza el formato {best["type"]}', 'detail': detail,
                     'evidence': f"{best['count']} posts de ese tipo"})

    # ── 3) Frecuencia de publicación ──
    dates = sorted(datetime.fromisoformat(p['timestamp'].replace('Z', '+00:00').replace('+0000', '+00:00'))
                   for p in valid)
    span_days = (dates[-1] - dates[0]).days or 1
    per_week = round(len(valid) / span_days * 7, 1)
    if per_week < 3:
        recs.append({'priority': 'high', 'icon': 'bi-calendar-plus-fill', 'category': 'Frecuencia',
                     'title': f'Publica más seguido (ahora ~{per_week}/semana)',
                     'detail': 'Las cuentas que más crecen publican 4-5 veces por semana. Sube la cadencia de forma constante.',
                     'evidence': f'{len(valid)} posts en {span_days} días'})
    elif per_week > 14:
        recs.append({'priority': 'medium', 'icon': 'bi-calendar-check-fill', 'category': 'Frecuencia',
                     'title': f'Cuida la calidad ({per_week}/semana es mucho)',
                     'detail': 'Publicas muy seguido; asegúrate de no sacrificar calidad por cantidad.',
                     'evidence': ''})
    else:
        recs.append({'priority': 'low', 'icon': 'bi-calendar-check-fill', 'category': 'Frecuencia',
                     'title': f'Buena cadencia (~{per_week}/semana)',
                     'detail': 'Mantén la consistencia. La regularidad pesa más que el volumen.',
                     'evidence': ''})

    # ── 4) Temas que funcionan (palabras de los top performers) ──
    top_topics = caption_topics(top, top=12)
    if top_topics['top_words']:
        words = ', '.join(w['word'] for w in top_topics['top_words'][:8])
        recs.append({'priority': 'high', 'icon': 'bi-lightbulb-fill', 'category': 'Temas',
                     'title': 'Crea más contenido sobre estos temas',
                     'detail': f'Tus posts con mejor engagement hablan de: {words}.',
                     'evidence': 'extraído de tus top posts'})

    # ── 5) Hashtags de alto rendimiento ──
    hperf = hashtag_performance(valid, top=10)
    if hperf:
        tags = ' '.join('#' + h['tag'] for h in hperf[:6])
        recs.append({'priority': 'medium', 'icon': 'bi-hash', 'category': 'Hashtags',
                     'title': 'Reutiliza tus hashtags de mayor engagement',
                     'detail': f'Estos hashtags acompañan a tus mejores posts: {tags}',
                     'evidence': f'ordenados por engagement medio'})

    # ── 6) Longitud de caption ──
    lve = sorted(caption_topics(valid)['length_vs_eng'], key=lambda x: -x['avg_eng'])
    if lve and lve[0]['posts'] >= 2:
        recs.append({'priority': 'medium', 'icon': 'bi-textarea-resize', 'category': 'Captions',
                     'title': f"Apunta a captions tipo: {lve[0]['bucket']}",
                     'detail': f"Tus captions {lve[0]['bucket'].lower()} rinden mejor ({lve[0]['avg_eng']:,} eng. medio).".replace(',', '.'),
                     'evidence': ''})

    # ── 7) Emojis ──
    with_em = [_eng(p) for p in valid if _EMOJI_RE.search(p.get('caption') or '')]
    without_em = [_eng(p) for p in valid if not _EMOJI_RE.search(p.get('caption') or '')]
    if with_em and without_em:
        a = sum(with_em) / len(with_em); b = sum(without_em) / len(without_em)
        if a > b * 1.2:
            recs.append({'priority': 'low', 'icon': 'bi-emoji-smile-fill', 'category': 'Estilo',
                         'title': 'Usa emojis en tus captions',
                         'detail': f'Tus posts con emojis tienen ~{round((a/b-1)*100)}% más engagement.',
                         'evidence': ''})

    # ── Ideas de publicación concretas (combinando formato + tema) ──
    ideas = []
    best_fmt = by_type[0]['type'] if by_type else 'REEL/VIDEO'
    fmt_label = {'VIDEO': 'un vídeo', 'REEL': 'un Reel', 'IMAGE': 'una imagen',
                 'CAROUSEL_ALBUM': 'un carrusel', 'POST': 'un post'}.get(best_fmt, best_fmt)
    for w in (top_topics['top_words'][:5] if top_topics['top_words'] else []):
        ideas.append(f'Crea {fmt_label} sobre "{w["word"]}" — es de tus temas con mejor respuesta.')
    if schedule:
        s = schedule[0]
        ideas.append(f'Programa tu próximo post para el {s["day"]} ~{s["hour"]:02d}:00.')

    prio_order = {'high': 0, 'medium': 1, 'low': 2}
    recs.sort(key=lambda r: prio_order.get(r['priority'], 3))

    return {
        'enough_data': True,
        'avg_eng': avg_eng,
        'median_eng': round(median_eng),
        'top_count': len(top),
        'per_week': per_week,
        'schedule': schedule,
        'recommendations': recs,
        'ideas': ideas,
    }


def hashtag_performance(posts: list[dict], top: int = 20) -> list[dict]:
    """Para cada hashtag: nº de usos y engagement medio de los posts que lo usan."""
    from collections import defaultdict
    agg = defaultdict(lambda: {'count': 0, 'eng': 0})
    for p in posts:
        e = _eng(p)
        for tag in set(t.lower() for t in _HASHTAG_RE.findall(p.get('caption') or '')):
            agg[tag]['count'] += 1
            agg[tag]['eng'] += e
    out = [{'tag': t, 'count': d['count'], 'avg_eng': round(d['eng'] / d['count'])}
           for t, d in agg.items() if d['count'] >= 1]
    return sorted(out, key=lambda x: -x['avg_eng'])[:top]


# ── Facebook Pages (reusa el token de Meta) ─────────────────────────────────

def get_fb_pages(access_token: str) -> list[dict]:
    r = requests.get(f'{GRAPH}/me/accounts', params={
        'fields': 'id,name,fan_count,followers_count,picture{url},access_token,link',
        'access_token': access_token, 'limit': 50,
    }, timeout=30)
    data = r.json()
    if 'error' in data:
        raise RuntimeError(data['error'].get('message'))
    out = []
    for p in data.get('data', []):
        out.append({
            'id': p.get('id'), 'name': p.get('name'),
            'fan_count': p.get('fan_count'), 'followers_count': p.get('followers_count'),
            'picture_url': (p.get('picture') or {}).get('data', {}).get('url'),
            'access_token': p.get('access_token'), 'link': p.get('link'),
        })
    return out


def get_fb_page_posts(page_id: str, page_token: str, limit: int = 50) -> list[dict]:
    r = requests.get(f'{GRAPH}/{page_id}/posts', params={
        'fields': 'message,created_time,full_picture,permalink_url,'
                  'reactions.summary(true).limit(0),comments.summary(true).limit(0),shares',
        'access_token': page_token, 'limit': limit,
    }, timeout=30)
    data = r.json()
    if 'error' in data:
        raise RuntimeError(data['error'].get('message'))
    out = []
    for p in data.get('data', []):
        out.append({
            'id': p.get('id'),
            'caption': p.get('message'),
            'media_type': 'POST',
            'like_count': (p.get('reactions') or {}).get('summary', {}).get('total_count', 0),
            'comments_count': (p.get('comments') or {}).get('summary', {}).get('total_count', 0),
            'share_count': (p.get('shares') or {}).get('count', 0),
            'timestamp': p.get('created_time'),
            'permalink': p.get('permalink_url'),
            'media_url': p.get('full_picture'),
            'thumbnail_url': p.get('full_picture'),
        })
    return out


# ── TikTok (Display API) ────────────────────────────────────────────────────

TT_AUTH = 'https://www.tiktok.com/v2/auth/authorize/'
TT_TOKEN = 'https://open.tiktokapis.com/v2/oauth/token/'
TT_USER = 'https://open.tiktokapis.com/v2/user/info/'
TT_VIDEOS = 'https://open.tiktokapis.com/v2/video/list/'
TT_SCOPES = ['user.info.basic', 'user.info.profile', 'user.info.stats', 'video.list']


def tt_auth_url(client_key: str, redirect_uri: str, state: str) -> str:
    from urllib.parse import urlencode
    return TT_AUTH + '?' + urlencode({
        'client_key': client_key, 'scope': ','.join(TT_SCOPES),
        'response_type': 'code', 'redirect_uri': redirect_uri, 'state': state,
    })


def tt_exchange_code(client_key: str, client_secret: str, redirect_uri: str, code: str) -> dict:
    r = requests.post(TT_TOKEN, data={
        'client_key': client_key, 'client_secret': client_secret,
        'code': code, 'grant_type': 'authorization_code', 'redirect_uri': redirect_uri,
    }, headers={'Content-Type': 'application/x-www-form-urlencoded'}, timeout=30)
    d = r.json()
    if 'access_token' not in d:
        raise RuntimeError(d.get('error_description') or d.get('error') or 'No se obtuvo token TikTok')
    return d


def tt_get_profile(access_token: str) -> dict:
    fields = 'open_id,union_id,avatar_url,display_name,bio_description,follower_count,following_count,likes_count,video_count'
    r = requests.get(TT_USER, params={'fields': fields},
                     headers={'Authorization': f'Bearer {access_token}'}, timeout=30)
    d = r.json()
    if d.get('error', {}).get('code') not in (None, 'ok'):
        raise RuntimeError(d['error'].get('message'))
    return d.get('data', {}).get('user', {})


def tt_get_videos(access_token: str, max_count: int = 20) -> list[dict]:
    fields = 'id,title,video_description,create_time,cover_image_url,share_url,view_count,like_count,comment_count,share_count'
    r = requests.post(f'{TT_VIDEOS}?fields={fields}',
                      json={'max_count': max_count},
                      headers={'Authorization': f'Bearer {access_token}', 'Content-Type': 'application/json'},
                      timeout=30)
    d = r.json()
    if d.get('error', {}).get('code') not in (None, 'ok'):
        raise RuntimeError(d['error'].get('message'))
    from datetime import datetime, timezone
    out = []
    for v in d.get('data', {}).get('videos', []):
        ct = v.get('create_time')
        ts = datetime.fromtimestamp(ct, tz=timezone.utc).isoformat() if ct else None
        out.append({
            'id': str(v.get('id')),
            'caption': v.get('title') or v.get('video_description'),
            'media_type': 'VIDEO',
            'like_count': v.get('like_count'),
            'comments_count': v.get('comment_count'),
            'share_count': v.get('share_count'),
            'view_count': v.get('view_count'),
            'timestamp': ts,
            'permalink': v.get('share_url'),
            'thumbnail_url': v.get('cover_image_url'),
            'media_url': v.get('cover_image_url'),
        })
    return out
