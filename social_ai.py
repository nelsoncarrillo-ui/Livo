"""Generación de ideas de contenido con Claude Opus 4.7 (visión).

Analiza las imágenes reales de los posts + captions + métricas para producir
una crítica del contenido e ideas accionables. Usa prompt caching en el system.
"""
import base64
import json

import requests
import anthropic

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
    "y content_calendar (parrilla de 30 días con 10-16 posts ya programados en fechas "
    "y horas reales basadas en los mejores días/horas que detectes en los datos)."
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
        max_tokens=12000,
        thinking={"type": "adaptive"},
        system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": content}],
        output_config={"format": {"type": "json_schema", "schema": OUTPUT_SCHEMA}},
    )
    text = next((b.text for b in resp.content if b.type == "text"), "{}")
    data = json.loads(text)
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
        max_output_tokens=8000,
    )

    # Orden de modelos a intentar
    candidates = [model] if model else []
    candidates += [m for m in GEMINI_FALLBACK_MODELS if m not in candidates]

    last_err = None
    for mdl in candidates:
        try:
            resp = client.models.generate_content(
                model=mdl,
                contents=[types.Content(role="user", parts=parts)],
                config=cfg,
            )
            data = json.loads(resp.text)
            data["_provider"] = "gemini"
            data["_model"] = mdl
            return data
        except Exception as e:
            msg = str(e)
            last_err = e
            # Si es cuota agotada (limit 0) o modelo no encontrado, probar el siguiente
            if any(s in msg for s in ("RESOURCE_EXHAUSTED", "429", "NOT_FOUND", "404", "limit: 0", "not found")):
                continue
            raise
    # Si todos fallan
    raise RuntimeError(
        "Ningún modelo gratuito de Gemini tiene cuota disponible para tu key. "
        "Esto suele pasar si el free tier no está habilitado en tu región/proyecto. "
        f"Último error: {last_err}"
    )


def generate_ideas(provider: str, api_key: str, account: dict, top_posts, bottom_posts,
                   stats_context="", max_images=8) -> dict:
    """Dispatcher: provider 'gemini' (gratis) o 'claude' (paga)."""
    if provider == "claude":
        return generate_ideas_claude(api_key, account, top_posts, bottom_posts, stats_context, max_images)
    return generate_ideas_gemini(api_key, account, top_posts, bottom_posts, stats_context, max_images)
