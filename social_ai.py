"""Generación de ideas de contenido con Claude Opus 4.7 (visión).

Analiza las imágenes reales de los posts + captions + métricas para producir
una crítica del contenido e ideas accionables. Usa prompt caching en el system.
"""
import base64
import json

import requests
import anthropic


# Errores recuperables: cuota (saltar modelo), 503/overload (reintentar mismo modelo).
_GEMINI_QUOTA_MARKERS = ('RESOURCE_EXHAUSTED', '429', 'NOT_FOUND', '404', 'limit: 0', 'not found')
_GEMINI_TRANSIENT_MARKERS = ('503', 'UNAVAILABLE', 'overloaded', 'high demand',
                              'INTERNAL', 'DEADLINE_EXCEEDED', 'Service Unavailable')


def _gemini_call_with_fallback(client, candidates: list[str], contents, cfg):
    """Llama a Gemini probando modelos en orden. 503/overload: reintenta el mismo con backoff."""
    import time
    last_err = None
    for mdl in candidates:
        for attempt in range(3):  # 3 intentos por modelo en 503/overload
            try:
                return mdl, client.models.generate_content(model=mdl, contents=contents, config=cfg)
            except Exception as e:
                msg = str(e)
                last_err = e
                if any(s in msg for s in _GEMINI_QUOTA_MARKERS):
                    break  # saltar al siguiente modelo
                if any(s in msg for s in _GEMINI_TRANSIENT_MARKERS):
                    time.sleep(2 * (attempt + 1))  # 2s, 4s, 6s
                    continue
                raise  # otro error: no enmascarar
    raise RuntimeError(
        "Ningún modelo Gemini respondió. Los modelos gratuitos están saturados o "
        "tu key no tiene cuota. Reintenta en 1-2 minutos o configura Claude. "
        f"Último error: {last_err}"
    )


def _safe_json_loads(text: str) -> dict:
    """json.loads con fallback a json-repair (tolera salidas truncadas/comas finales)."""
    if not text:
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        try:
            from json_repair import repair_json
            return json.loads(repair_json(text))
        except Exception:
            # Último recurso: repair_json con return_objects
            from json_repair import repair_json
            return repair_json(text, return_objects=True) or {}

MODEL = "claude-opus-4-7"

SYSTEM_PROMPT = """Eres un estratega senior de redes sociales y dirección creativa. \
Analizas cuentas reales de Instagram/Facebook/TikTok mirando el CONTENIDO VISUAL de las publicaciones \
(no solo los textos), cruzándolo con sus métricas de engagement, para dar recomendaciones accionables.

Recibirás: datos del perfil, y un conjunto de publicaciones marcadas como [ALTO ENGAGEMENT] o \
[BAJO ENGAGEMENT], cada una con su imagen/miniatura real, su caption y sus métricas.

Tu trabajo:
1. MIRAR las imágenes con atención: estética, colores, composición, tipo de contenido (producto, \
persona, lifestyle, texto sobre imagen, meme, infografía, etc.), calidad visual, coherencia de marca.
2. Comparar qué tienen en común los posts de ALTO engagement vs los de BAJO — visualmente y en temática.
3. Producir una crítica honesta y concreta, IDEAS sueltas de publicaciones y una PARRILLA DE 30 DÍAS \
(content calendar) con fechas reales y horarios ya distribuidos.

Reglas para la parrilla de 30 días:
- Usa FECHAS REALES en formato YYYY-MM-DD a partir de la fecha de "HOY" que se te indique.
- Distribúyelas según los mejores días/horas que detectes en sus datos (no fechas aleatorias).
- Respeta una cadencia realista. Si la cuenta publica 2 veces por semana ahora, la parrilla puede \
proponer subir a 3-4/semana, no 7. Total típico: 10-16 posts en 30 días.
- Cada entrada debe ser distinta (alterna formatos y temas) y mantener coherencia de marca.
- Indica el día de la semana en abreviado (Lun/Mar/Mié/Jue/Vie/Sáb/Dom).

Reglas generales:
- Sé específico y concreto. Nada de consejos de manual ("publica contenido de calidad").
- Basa todo en lo que REALMENTE ves en las imágenes y en los datos. Cita ejemplos.
- Captions y ganchos deben estar listos para copiar/pegar, en español.
- Responde SIEMPRE en español.
- Devuelve únicamente el JSON con el esquema solicitado."""

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "content_review": {"type": "string", "description": "Crítica general honesta del contenido actual, basada en lo que se ve"},
        "visual_observations": {"type": "string", "description": "Qué se observa en las imágenes: estética, patrones visuales, coherencia de marca"},
        "whats_working": {"type": "array", "items": {"type": "string"}, "description": "Qué está funcionando (visual + temático), con evidencia"},
        "whats_not_working": {"type": "array", "items": {"type": "string"}, "description": "Qué frena el rendimiento o se puede mejorar"},
        "ideas": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Título corto de la idea"},
                    "format": {"type": "string", "description": "Reel, Carrusel, Imagen, Vídeo, Story, etc."},
                    "topic": {"type": "string", "description": "Tema concreto"},
                    "hook": {"type": "string", "description": "Gancho/primer segundo o primera línea"},
                    "caption_example": {"type": "string", "description": "Caption completo listo para publicar"},
                    "hashtags": {"type": "array", "items": {"type": "string"}},
                    "best_time": {"type": "string", "description": "Mejor día/hora sugerido"},
                    "rationale": {"type": "string", "description": "Por qué esta idea, basada en sus datos"},
                },
                "required": ["title", "format", "topic", "hook", "caption_example", "hashtags", "best_time", "rationale"],
                "additionalProperties": False,
            },
        },
        "content_calendar": {
            "type": "array",
            "description": "Parrilla de 30 días con fechas y horas reales",
            "items": {
                "type": "object",
                "properties": {
                    "date": {"type": "string", "description": "Fecha YYYY-MM-DD"},
                    "weekday": {"type": "string", "description": "Abreviatura: Lun, Mar, Mié, Jue, Vie, Sáb, Dom"},
                    "time": {"type": "string", "description": "HH:MM (24h)"},
                    "format": {"type": "string", "description": "Reel, Carrusel, Imagen, Vídeo, Story"},
                    "topic": {"type": "string", "description": "Tema corto del post"},
                    "hook": {"type": "string", "description": "Gancho de apertura"},
                    "caption_example": {"type": "string", "description": "Caption completo listo para publicar"},
                    "hashtags": {"type": "array", "items": {"type": "string"}},
                    "rationale": {"type": "string", "description": "Por qué este día/hora/contenido"},
                },
                "required": ["date", "weekday", "time", "format", "topic", "hook", "caption_example", "hashtags", "rationale"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["content_review", "visual_observations", "whats_working", "whats_not_working", "ideas", "content_calendar"],
    "additionalProperties": False,
}


def _fetch_image_b64(url: str, timeout: int = 15):
    """Descarga una imagen y la devuelve como (media_type, base64). None si falla."""
    try:
        r = requests.get(url, timeout=timeout)
        if r.status_code != 200 or not r.content:
            return None
        ct = r.headers.get("Content-Type", "image/jpeg").split(";")[0].strip()
        if ct not in ("image/jpeg", "image/png", "image/gif", "image/webp"):
            ct = "image/jpeg"
        return ct, base64.standard_b64encode(r.content).decode("utf-8")
    except Exception:
        return None


def _post_line(p: dict, tag: str) -> str:
    likes = p.get("like_count") or 0
    comments = p.get("comments_count") or 0
    views = p.get("view_count")
    shares = p.get("share_count")
    cap = (p.get("caption") or "").strip().replace("\n", " ")
    if len(cap) > 300:
        cap = cap[:300] + "…"
    metrics = f"{likes} likes, {comments} comentarios"
    if views:
        metrics += f", {views} views"
    if shares:
        metrics += f", {shares} shares"
    fecha = (p.get("timestamp") or "")[:10]
    tipo = p.get("media_type") or ""
    return f"[{tag}] {tipo} · {fecha} · {metrics}\nCaption: {cap or '(sin texto)'}"


def _build_account_header(account: dict, stats_context: str) -> str:
    from datetime import date, timedelta
    today = date.today()
    end = today + timedelta(days=29)
    header = (
        f"CUENTA: @{account.get('username')} ({account.get('platform', 'instagram')})\n"
        f"Nombre: {account.get('name') or '—'}\n"
        f"Bio: {account.get('biography') or '—'}\n"
        f"HOY: {today.isoformat()} ({['Lun','Mar','Mié','Jue','Vie','Sáb','Dom'][today.weekday()]})\n"
        f"VENTANA PARRILLA: {today.isoformat()} → {end.isoformat()} (30 días)\n"
    )
    if stats_context:
        header += f"\nContexto de métricas:\n{stats_context}\n"
    header += "\nA continuación, publicaciones con su imagen real, caption y métricas:"
    return header


def _collect_posts(top_posts, bottom_posts, max_images):
    """Devuelve lista ordenada de items: ('text', str) o ('image', (mime, b64), linea)."""
    n_top = min(len(top_posts), max(3, max_images - 3))
    n_bottom = min(len(bottom_posts), max_images - n_top)
    items = []
    for p in top_posts[:n_top]:
        img = p.get("thumbnail_url") or p.get("media_url")
        fetched = _fetch_image_b64(img) if img else None
        items.append((fetched, _post_line(p, "ALTO ENGAGEMENT")))
    for p in bottom_posts[:n_bottom]:
        img = p.get("thumbnail_url") or p.get("media_url")
        fetched = _fetch_image_b64(img) if img else None
        items.append((fetched, _post_line(p, "BAJO ENGAGEMENT")))
    return items


_FINAL_INSTRUCTION = (
    "Analiza el contenido visual y los datos. Devuelve el JSON con: content_review, "
    "visual_observations, whats_working, whats_not_working, ideas (5-7 ideas concretas), "
    "y content_calendar (parrilla de 30 días con 8-12 posts ya programados en fechas "
    "y horas reales basadas en los mejores días/horas que detectes en los datos). "
    "Mantén los campos concisos pero accionables."
)


# ── Claude (paga, mejor calidad) ────────────────────────────────────────────

def generate_ideas_claude(api_key: str, account: dict, top_posts, bottom_posts,
                          stats_context="", max_images=8) -> dict:
    client = anthropic.Anthropic(api_key=api_key)
    content = [{"type": "text", "text": _build_account_header(account, stats_context)}]
    for fetched, line in _collect_posts(top_posts, bottom_posts, max_images):
        if fetched:
            content.append({"type": "image", "source": {
                "type": "base64", "media_type": fetched[0], "data": fetched[1]}})
        content.append({"type": "text", "text": line})
    content.append({"type": "text", "text": _FINAL_INSTRUCTION})

    resp = client.messages.create(
        model=MODEL,
        max_tokens=20000,
        thinking={"type": "adaptive"},
        system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": content}],
        output_config={"format": {"type": "json_schema", "schema": OUTPUT_SCHEMA}},
    )
    text = next((b.text for b in resp.content if b.type == "text"), "{}")
    data = _safe_json_loads(text)
    data["_provider"] = "claude"
    return data


# ── Gemini (tier gratuito, también con visión) ──────────────────────────────

# Modelos gratuitos a probar en orden (la cuota free es POR modelo; si uno da
# limit:0 probamos el siguiente). El usuario puede forzar uno con gemini_model.
GEMINI_FALLBACK_MODELS = [
    "gemini-2.5-flash",
    "gemini-2.0-flash",
    "gemini-2.5-flash-lite",
    "gemini-flash-latest",
    "gemini-1.5-flash",
    "gemini-1.5-flash-8b",
]


def gemini_list_models(api_key: str) -> list[str]:
    """Lista modelos que soportan generateContent (para que el usuario elija)."""
    from google import genai
    client = genai.Client(api_key=api_key)
    out = []
    try:
        for m in client.models.list():
            actions = getattr(m, "supported_actions", None) or getattr(m, "supported_generation_methods", None) or []
            name = (m.name or "").replace("models/", "")
            if (not actions) or ("generateContent" in actions):
                if "flash" in name or "pro" in name:
                    out.append(name)
    except Exception:
        pass
    return out


def generate_ideas_gemini(api_key: str, account: dict, top_posts, bottom_posts,
                          stats_context="", max_images=8, model: str = None) -> dict:
    import base64 as _b64
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=api_key)
    parts = [types.Part.from_text(text=_build_account_header(account, stats_context))]
    for fetched, line in _collect_posts(top_posts, bottom_posts, max_images):
        if fetched:
            parts.append(types.Part.from_bytes(
                data=_b64.b64decode(fetched[1]), mime_type=fetched[0]))
        parts.append(types.Part.from_text(text=line))
    parts.append(types.Part.from_text(text=_FINAL_INSTRUCTION + """

Responde ÚNICAMENTE con un JSON válido con esta forma EXACTA:
{
  "content_review": "texto",
  "visual_observations": "texto",
  "whats_working": ["..."],
  "whats_not_working": ["..."],
  "ideas": [{"title":"","format":"","topic":"","hook":"","caption_example":"","hashtags":["..."],"best_time":"","rationale":""}],
  "content_calendar": [
    {"date":"YYYY-MM-DD","weekday":"Lun|Mar|Mié|Jue|Vie|Sáb|Dom","time":"HH:MM","format":"","topic":"","hook":"","caption_example":"","hashtags":["..."],"rationale":""}
  ]
}

content_calendar debe tener 10-16 entradas con fechas REALES dentro de la ventana indicada arriba, ordenadas cronológicamente."""))

    cfg = types.GenerateContentConfig(
        system_instruction=SYSTEM_PROMPT,
        response_mime_type="application/json",
        max_output_tokens=32000,
    )

    # Orden de modelos a intentar (con retry por modelo en 503/overload)
    candidates = [model] if model else []
    candidates += [m for m in GEMINI_FALLBACK_MODELS if m not in candidates]
    contents = [types.Content(role="user", parts=parts)]
    mdl, resp = _gemini_call_with_fallback(client, candidates, contents, cfg)
    data = _safe_json_loads(resp.text)
    data["_provider"] = "gemini"
    data["_model"] = mdl
    return data


def generate_ideas(provider: str, api_key: str, account: dict, top_posts, bottom_posts,
                   stats_context="", max_images=8) -> dict:
    """Dispatcher: provider 'gemini' (gratis) o 'claude' (paga)."""
    if provider == "claude":
        return generate_ideas_claude(api_key, account, top_posts, bottom_posts, stats_context, max_images)
    return generate_ideas_gemini(api_key, account, top_posts, bottom_posts, stats_context, max_images)


# ══ PLAN DE RECUPERACIÓN DE CUENTA ═══════════════════════════════════════════

RECOVERY_SYSTEM = """Eres consultor senior de social media y community management con 10 años de \
experiencia recuperando cuentas con engagement caído. Una marca te pide diagnosticar el porqué de su \
caída de engagement y construir un PLAN DE RECUPERACIÓN realista por fases (4-8 semanas).

Recibirás:
- Datos del perfil y bio
- Historial de seguidores (snapshots)
- Sus mejores y peores publicaciones con IMAGEN real, caption y métricas
- Contexto que la marca escribe libremente (qué publica en stories, qué creen que está fallando, \
objetivos, etc.)

Tu trabajo:
1. DIAGNÓSTICO honesto — qué ves visualmente en los posts (estética, repetición, fatiga visual, \
exceso de promoción), patrones de captions, tendencia de seguidores. Cita ejemplos concretos.
2. CAUSAS RAÍZ — lista corta y específica, no de manual.
3. PLAN POR FASES (4 a 6 fases) — tipo "Detox de promo → Reconexión con valor → Reactivación de \
comunidad → Reintroducción balanceada de promo". Cada fase con semanas, objetivo, qué hacer / qué NO \
hacer y 2-3 ejemplos de posts.
4. MIX DE CONTENIDO objetivo: % educativo / entretenimiento / inspiracional / comunidad / promo.
5. FRECUENCIA recomendada: feed/semana, reels/semana, stories/día (con justificación).
6. ESTRATEGIA DE STORIES — si están haciendo spam de promociones, da el detox concreto: cuántas, qué \
tipo, qué cortar ya.
7. CALENDARIO de los PRIMEROS 14 DÍAS día a día (feed + stories de cada día).
8. HITOS POR SEMANA — qué KPI medir y un objetivo realista por semana.
9. LISTA EXPLÍCITA de lo que deben DEJAR DE HACER inmediatamente.

Reglas:
- Sé específico y honesto. Si abusan de promo, dilo claro.
- Basa todo en lo que VES + contexto. Cita ejemplos visuales y de captions.
- Cadencia realista: si están agotados de spam, no propongas más posts — propon menos pero mejores.
- Si el contexto del usuario menciona algo, abórdalo directamente.
- En español. Devuelve ÚNICAMENTE el JSON con el esquema solicitado."""

RECOVERY_SCHEMA = {
    "type": "object",
    "properties": {
        "diagnosis": {"type": "string", "description": "Diagnóstico honesto y específico, con ejemplos"},
        "root_causes": {"type": "array", "items": {"type": "string"}, "description": "Causas raíz identificadas"},
        "phases": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "number": {"type": "integer"},
                    "name": {"type": "string"},
                    "weeks": {"type": "string", "description": "Ej: 'Semana 1-2'"},
                    "goal": {"type": "string"},
                    "rules_do": {"type": "array", "items": {"type": "string"}},
                    "rules_dont": {"type": "array", "items": {"type": "string"}},
                    "example_posts": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["number", "name", "weeks", "goal", "rules_do", "rules_dont", "example_posts"],
                "additionalProperties": False,
            },
        },
        "content_mix": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "category": {"type": "string", "description": "Educativo, Entretenimiento, Inspiracional, Comunidad, Promo, UGC, etc."},
                    "percent": {"type": "integer"},
                    "description": {"type": "string"},
                },
                "required": ["category", "percent", "description"],
                "additionalProperties": False,
            },
        },
        "posting_frequency": {
            "type": "object",
            "properties": {
                "feed_per_week": {"type": "integer"},
                "reels_per_week": {"type": "integer"},
                "stories_per_day": {"type": "integer"},
                "notes": {"type": "string"},
            },
            "required": ["feed_per_week", "reels_per_week", "stories_per_day", "notes"],
            "additionalProperties": False,
        },
        "stories_strategy": {"type": "string", "description": "Detox y nueva estrategia de stories"},
        "first_14_days": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "day": {"type": "integer"},
                    "date": {"type": "string", "description": "YYYY-MM-DD"},
                    "weekday": {"type": "string"},
                    "feed": {"type": "string", "description": "Qué publicar en feed ese día (vacío si descanso)"},
                    "stories": {"type": "string", "description": "Qué stories ese día"},
                    "rationale": {"type": "string"},
                },
                "required": ["day", "date", "weekday", "feed", "stories", "rationale"],
                "additionalProperties": False,
            },
        },
        "weekly_milestones": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "week": {"type": "integer"},
                    "kpi": {"type": "string"},
                    "target": {"type": "string"},
                },
                "required": ["week", "kpi", "target"],
                "additionalProperties": False,
            },
        },
        "stop_doing": {"type": "array", "items": {"type": "string"}, "description": "Lista de cosas a DEJAR de hacer inmediatamente"},
    },
    "required": ["diagnosis", "root_causes", "phases", "content_mix", "posting_frequency",
                 "stories_strategy", "first_14_days", "weekly_milestones", "stop_doing"],
    "additionalProperties": False,
}


def _build_recovery_header(account: dict, stats_context: str, snapshots_summary: str, user_context: str) -> str:
    from datetime import date, timedelta
    today = date.today()
    end = today + timedelta(days=13)
    header = (
        f"CUENTA: @{account.get('username')} ({account.get('platform', 'instagram')})\n"
        f"Nombre: {account.get('name') or '—'}\n"
        f"Bio: {account.get('biography') or '—'}\n"
        f"HOY: {today.isoformat()} ({['Lun','Mar','Mié','Jue','Vie','Sáb','Dom'][today.weekday()]})\n"
        f"VENTANA PRIMEROS 14 DÍAS: {today.isoformat()} → {end.isoformat()}\n"
    )
    if snapshots_summary:
        header += f"\nTendencia de seguidores:\n{snapshots_summary}\n"
    if stats_context:
        header += f"\nContexto de métricas:\n{stats_context}\n"
    if user_context:
        header += f"\nCONTEXTO QUE NOS DA LA MARCA (importante, abórdalo directamente):\n{user_context}\n"
    header += "\nA continuación, publicaciones con su imagen real, caption y métricas:"
    return header


def generate_recovery_claude(api_key: str, account: dict, top_posts, bottom_posts,
                             stats_context="", snapshots_summary="", user_context="",
                             max_images=8) -> dict:
    client = anthropic.Anthropic(api_key=api_key)
    content = [{"type": "text", "text": _build_recovery_header(account, stats_context, snapshots_summary, user_context)}]
    for fetched, line in _collect_posts(top_posts, bottom_posts, max_images):
        if fetched:
            content.append({"type": "image", "source": {
                "type": "base64", "media_type": fetched[0], "data": fetched[1]}})
        content.append({"type": "text", "text": line})
    content.append({"type": "text", "text":
        "Diagnostica y construye el plan de recuperación. Devuelve el JSON completo con el esquema."})

    resp = client.messages.create(
        model=MODEL,
        max_tokens=14000,
        thinking={"type": "adaptive"},
        system=[{"type": "text", "text": RECOVERY_SYSTEM, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": content}],
        output_config={"format": {"type": "json_schema", "schema": RECOVERY_SCHEMA}},
    )
    text = next((b.text for b in resp.content if b.type == "text"), "{}")
    data = _safe_json_loads(text)
    data["_provider"] = "claude"
    return data


def generate_recovery_gemini(api_key: str, account: dict, top_posts, bottom_posts,
                             stats_context="", snapshots_summary="", user_context="",
                             max_images=8, model: str = None) -> dict:
    import base64 as _b64
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=api_key)
    parts = [types.Part.from_text(text=_build_recovery_header(account, stats_context, snapshots_summary, user_context))]
    for fetched, line in _collect_posts(top_posts, bottom_posts, max_images):
        if fetched:
            parts.append(types.Part.from_bytes(
                data=_b64.b64decode(fetched[1]), mime_type=fetched[0]))
        parts.append(types.Part.from_text(text=line))
    parts.append(types.Part.from_text(text="""
Diagnostica y construye el plan de recuperación. Responde ÚNICAMENTE con un JSON válido con esta forma EXACTA:
{
  "diagnosis": "texto",
  "root_causes": ["..."],
  "phases": [{"number":1,"name":"","weeks":"","goal":"","rules_do":["..."],"rules_dont":["..."],"example_posts":["..."]}],
  "content_mix": [{"category":"","percent":0,"description":""}],
  "posting_frequency": {"feed_per_week":0,"reels_per_week":0,"stories_per_day":0,"notes":""},
  "stories_strategy": "texto",
  "first_14_days": [{"day":1,"date":"YYYY-MM-DD","weekday":"","feed":"","stories":"","rationale":""}],
  "weekly_milestones": [{"week":1,"kpi":"","target":""}],
  "stop_doing": ["..."]
}
first_14_days debe tener 14 entradas en orden cronológico (días 1 a 14)."""))

    cfg = types.GenerateContentConfig(
        system_instruction=RECOVERY_SYSTEM,
        response_mime_type="application/json",
        max_output_tokens=32000,
    )

    candidates = [model] if model else []
    candidates += [m for m in GEMINI_FALLBACK_MODELS if m not in candidates]
    contents = [types.Content(role="user", parts=parts)]
    mdl, resp = _gemini_call_with_fallback(client, candidates, contents, cfg)
    data = _safe_json_loads(resp.text)
    data["_provider"] = "gemini"
    data["_model"] = mdl
    return data


def generate_recovery_plan(provider: str, api_key: str, account: dict, top_posts, bottom_posts,
                           stats_context="", snapshots_summary="", user_context="",
                           max_images=8) -> dict:
    """Dispatcher para el plan de recuperación."""
    if provider == "claude":
        return generate_recovery_claude(api_key, account, top_posts, bottom_posts,
                                        stats_context, snapshots_summary, user_context, max_images)
    return generate_recovery_gemini(api_key, account, top_posts, bottom_posts,
                                    stats_context, snapshots_summary, user_context, max_images)
