"""Google PageSpeed Insights API — Core Web Vitals + Lighthouse score."""
import requests

PSI_ENDPOINT = 'https://www.googleapis.com/pagespeedonline/v5/runPagespeed'


def fetch_pagespeed(url: str, api_key: str, strategy: str = 'mobile') -> dict:
    """Devuelve métricas clave de PageSpeed Insights para una URL.

    strategy: 'mobile' o 'desktop'.
    Métricas:
      - performance_score (0-100)
      - lcp (segundos), cls (score), inp/fid (ms), fcp (segundos), ttfb (ms)
      - field_data: bool (si hay datos CrUX reales de usuarios)
    """
    params = {
        'url': url,
        'key': api_key,
        'strategy': strategy,
        'category': 'performance',
    }
    try:
        r = requests.get(PSI_ENDPOINT, params=params, timeout=60)
        if r.status_code != 200:
            return {'error': f'PSI HTTP {r.status_code}: {r.text[:200]}'}
        data = r.json()
    except Exception as e:
        return {'error': str(e)}

    out = {'strategy': strategy}

    # Lighthouse lab score
    lh = data.get('lighthouseResult', {})
    cats = lh.get('categories', {})
    perf = cats.get('performance', {})
    if perf.get('score') is not None:
        out['performance_score'] = round(perf['score'] * 100)

    audits = lh.get('audits', {})
    def lab_seconds(key):
        a = audits.get(key, {})
        v = a.get('numericValue')
        return round(v / 1000, 2) if v is not None else None
    def lab_raw(key):
        a = audits.get(key, {})
        return a.get('numericValue')

    out['lcp'] = lab_seconds('largest-contentful-paint')
    out['fcp'] = lab_seconds('first-contentful-paint')
    out['tbt'] = lab_raw('total-blocking-time')
    cls_a = audits.get('cumulative-layout-shift', {})
    out['cls'] = round(cls_a['numericValue'], 3) if cls_a.get('numericValue') is not None else None
    out['speed_index'] = lab_seconds('speed-index')

    # Field data (CrUX, usuarios reales) — sobreescribe con datos reales si existen
    loading = data.get('loadingExperience', {})
    metrics = loading.get('metrics', {})
    if metrics:
        out['field_data'] = True
        lcp_f = metrics.get('LARGEST_CONTENTFUL_PAINT_MS', {}).get('percentile')
        if lcp_f is not None:
            out['lcp'] = round(lcp_f / 1000, 2)
        cls_f = metrics.get('CUMULATIVE_LAYOUT_SHIFT_SCORE', {}).get('percentile')
        if cls_f is not None:
            out['cls'] = round(cls_f / 100, 3)
        inp_f = metrics.get('INTERACTION_TO_NEXT_PAINT', {}).get('percentile')
        if inp_f is not None:
            out['inp'] = inp_f
        ttfb_f = metrics.get('EXPERIENCE_TIME_TO_FIRST_BYTE', {}).get('percentile')
        if ttfb_f is not None:
            out['ttfb'] = ttfb_f
        overall = loading.get('overall_category')
        out['crux_category'] = overall
    else:
        out['field_data'] = False

    return out
