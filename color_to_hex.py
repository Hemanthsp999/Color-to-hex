from flask import Flask, request, render_template_string, jsonify, flash
from PIL import Image, ImageColor
import io
import base64
import os
import difflib
import re
import colorsys

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "replace-with-a-secure-random-key")

# Try to import webcolors for a full CSS3 name→hex database. If unavailable we'll
# fall back to PIL and a small builtin list.
try:
    import webcolors
    CSS3_NAMES_TO_HEX = {k.lower(): v for k, v in webcolors.CSS3_NAMES_TO_HEX.items()}
except Exception:
    webcolors = None
    # Small fallback map - not exhaustive but covers many common names. If you need full
    # coverage, install `webcolors`.
    CSS3_NAMES_TO_HEX = {
        "black": "#000000", "white": "#FFFFFF", "red": "#FF0000",
        "green": "#008000", "blue": "#0000FF", "yellow": "#FFFF00",
        "crimson": "#DC143C", "maroon": "#800000", "orange": "#FFA500",
        "pink": "#FFC0CB", "purple": "#800080", "violet": "#EE82EE",
        "brown": "#A52A2A", "gray": "#808080", "grey": "#808080",
        "silver": "#C0C0C0", "gold": "#D4AF37", "beige": "#F5F5DC",
        "olive": "#808000", "lime": "#00FF00", "navy": "#000080",
        "teal": "#008080", "magenta": "#FF00FF", "coral": "#FF7F50",
        "salmon": "#FA8072", "turquoise": "#40E0D0", "indigo": "#4B0082", "burgundy": "#800020"
    }

# Build a names list for fuzzy matching
KNOWN_NAMES = sorted(CSS3_NAMES_TO_HEX.keys())

# Helper utilities
HEX_RE = re.compile(r"^#?[0-9a-fA-F]{6}$")
RGB_RE = re.compile(r"rgb\s*\(\s*(\d{1,3})\s*,\s*(\d{1,3})\s*,\s*(\d{1,3})\s*\)")

# New regexes and helpers for the integrated parser
HEX_SHORT_RE = re.compile(r"^#?([0-9a-fA-F]{3})$")
HEX_LONG_RE = re.compile(r"^#?([0-9a-fA-F]{6})$")
RGB_FUNC_RE = re.compile(r"rgba?\s*\(\s*([^)]*)\)")
HSL_FUNC_RE = re.compile(r"hsla?\s*\(\s*([^)]*)\)")
PLAIN_RGB_RE = re.compile(r"^\s*(\d{1,3})\s*[, \s]\s*(\d{1,3})\s*[, \s]\s*(\d{1,3})\s*$")
PERC_RE = re.compile(r"^(\d{1,3})%$")


def clamp255(v):
    return max(0, min(255, int(round(v))))


def parse_numeric_token(tok, is_percent_allowed=True):
    tok = tok.strip()
    m = PERC_RE.match(tok)
    if m and is_percent_allowed:
        pct = float(m.group(1))
        return clamp255(pct / 100.0 * 255.0)
    # plain integer
    if tok.isdigit():
        return clamp255(int(tok))
    # fallback — allow float too
    try:
        return clamp255(float(tok))
    except Exception:
        raise ValueError(f"Can't parse token '{tok}' as numeric color component")


def hsl_to_rgb_tuple(h, s, l):
    # h in degrees, s/l are 0..1 -> colorsys wants h 0..1 and uses HLS
    r, g, b = colorsys.hls_to_rgb(h / 360.0, l, s)
    return (clamp255(r * 255), clamp255(g * 255), clamp255(b * 255))


def rgb_to_hex(rgb):
    """Return uppercase #RRGGBB from an (r,g,b) tuple/list."""
    r, g, b = (int(round(c)) for c in rgb)
    r = max(0, min(255, r))
    g = max(0, min(255, g))
    b = max(0, min(255, b))
    return "#{:02X}{:02X}{:02X}".format(r, g, b)


def adjust_brightness(rgb, factor):
    """Multiply RGB by factor (e.g. 1.2 to lighten, 0.8 to darken)."""
    return tuple(max(0, min(255, int(round(c * factor)))) for c in rgb)


def normalize_name(text):
    """Lowercase, strip, collapse spaces and remove punctuation except '#', '()'"""
    if not text:
        return ""
    s = text.lower().strip()
    s = re.sub(r"[\t\n\r]+", " ", s)
    s = re.sub(r"[^a-z0-9#(),\s-]", " ", s)
    s = re.sub(r"[-_]+", " ", s)
    s = re.sub(r"\s+", " ", s)
    return s


def image_dominant_rgb(file_stream, resize_for_speed=(150, 150)):
    im = Image.open(file_stream)
    # Composite alpha onto white
    if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
        bg = Image.new("RGBA", im.size, (255, 255, 255, 255))
        im = im.convert("RGBA")
        bg.paste(im, mask=im.split()[-1])
        im = bg.convert("RGB")
    else:
        im = im.convert("RGB")

    im = im.resize(resize_for_speed, Image.Resampling.BILINEAR)
    maxcolors = resize_for_speed[0] * resize_for_speed[1] + 1
    colors = im.getcolors(maxcolors=maxcolors)
    if colors:
        dominant = max(colors, key=lambda t: t[0])[1]
        return tuple(dominant[:3])
    # fallback to adaptive palette
    pal = im.convert('P', palette=Image.ADAPTIVE, colors=16)
    palette = pal.getpalette()
    color_counts = pal.getcolors()
    dominant_index = max(color_counts, key=lambda t: t[0])[1]
    r = palette[dominant_index * 3]
    g = palette[dominant_index * 3 + 1]
    b = palette[dominant_index * 3 + 2]
    return (r, g, b)


def try_imagecolor_getrgb(text):
    """Try PIL ImageColor.getrgb safely and return an (r,g,b) tuple or raise ValueError."""
    try:
        rgb = ImageColor.getrgb(text)
        if len(rgb) >= 3:
            return tuple(int(c) for c in rgb[:3])
        raise ValueError("Unexpected color tuple")
    except Exception as e:
        raise ValueError(str(e))


def find_by_css3(name):
    """Exact CSS3 lookup (from webcolors or fallback table). Returns rgb or None."""
    if not name:
        return None
    hexv = CSS3_NAMES_TO_HEX.get(name)
    if hexv:
        return try_imagecolor_getrgb(hexv)
    return None


def fuzzy_lookup(name, n=3, cutoff=0.6):
    """Find close matches in KNOWN_NAMES and return the rgb of the best match if any."""
    if not KNOWN_NAMES:
        return None
    matches = difflib.get_close_matches(name, KNOWN_NAMES, n=n, cutoff=cutoff)
    for m in matches:
        rgb = find_by_css3(m)
        if rgb:
            return rgb
    return None


def text_to_rgb_extended(text):
    """Robustly convert user-entered text to an (r,g,b) tuple.

    Strategies (in order):
    - Direct ImageColor.getrgb (handles hex, rgb(), many color names)
    - Direct CSS3 lookup (webcolors)
    - Heuristics: remove adjectives (light/dark/pale/deep/very), try base token
    - If phrase contains two color words, try each and return the closest single color
    - Fuzzy match against known color names
    - If nothing works, raise ValueError
    """
    if not text or not text.strip():
        raise ValueError("Empty color text")

    raw = text.strip()
    norm = normalize_name(raw)

    # 1) direct attempt (handles '#rrggbb', 'rgb()', many CSS names already)
    try:
        return try_imagecolor_getrgb(raw)
    except Exception:
        pass

    # 2) if user provided 6-hex without '#', add it
    if HEX_RE.match(norm.replace(' ', '')):
        try:
            return try_imagecolor_getrgb('#' + norm.replace(' ', '').lstrip('#'))
        except Exception:
            pass

    # 3) try exact css3 lookup
    rgb = find_by_css3(norm)
    if rgb:
        return rgb

    # 4) strip common adjectives and retry
    adjectives = ['light', 'dark', 'pale', 'deep', 'medium', 'very', 'vivid', 'bright']
    tokens = norm.split()
    # if tokens contain words like 'red' 'crimson', try subphrases
    # Try decreasing length subphrases (last word, first word, last two words)
    tries = []
    # last word and first word
    if len(tokens) >= 1:
        tries.append(tokens[-1])
        tries.append(tokens[0])
    if len(tokens) >= 2:
        tries.append(' '.join(tokens[-2:]))
        tries.append(' '.join(tokens[:2]))
    # remove adjectives
    filtered = [t for t in tokens if t not in adjectives]
    if filtered and ' '.join(filtered) not in tries:
        tries.append(' '.join(filtered))

    # try all candidate subphrases
    for candidate in tries:
        try:
            return try_imagecolor_getrgb(candidate)
        except Exception:
            # try css3 exact
            rgb = find_by_css3(candidate)
            if rgb:
                return rgb

    # 5) if phrase contains two known color words, try to combine by averaging
    color_tokens = []
    for t in tokens:
        try:
            rgb_t = try_imagecolor_getrgb(t)
            color_tokens.append(rgb_t)
        except Exception:
            rgb_css = find_by_css3(t)
            if rgb_css:
                color_tokens.append(rgb_css)
    if len(color_tokens) == 1:
        return color_tokens[0]
    if len(color_tokens) >= 2:
        # average the colors
        n = len(color_tokens)
        avg = tuple(sum(c[i] for c in color_tokens) / n for i in range(3))
        return tuple(int(round(x)) for x in avg)

    # 6) fuzzy lookup against KNOWN_NAMES
    fuzzy = fuzzy_lookup(norm, n=5, cutoff=0.55)
    if fuzzy:
        return fuzzy

    # 7) as last attempt, try fuzzy on individual tokens
    for t in tokens:
        f = fuzzy_lookup(t, n=5, cutoff=0.55)
        if f:
            return f

    raise ValueError(f"Can't interpret '{text}' as a color")

# ---------- NEW: robust parse_color_input that recognizes hex/rgb/hsl/plain triples ----------


def parse_color_input(text, fallback_text_to_rgb_fn=None):
    """Return (r,g,b) tuple for input text.
    fallback_text_to_rgb_fn: optional callable(text)->(r,g,b) for names/fuzzy.
    """
    if not text or not text.strip():
        raise ValueError("Empty color input")

    s = text.strip()

    # 1) PIL can parse many notations directly (#hex, 'red', 'rgb(...)')
    try:
        return try_imagecolor_getrgb(s)
    except Exception:
        pass

    # 2) hex short like 'f0f'
    m = HEX_SHORT_RE.match(s)
    if m:
        g = m.group(1)
        r = g[0] * 2
        gr = g[1] * 2
        b = g[2] * 2
        try:
            return try_imagecolor_getrgb(f"#{r}{gr}{b}")
        except Exception:
            pass

    # 3) hex long like 'ff00ff' (without #)
    m = HEX_LONG_RE.match(s)
    if m:
        try:
            return try_imagecolor_getrgb('#' + m.group(1))
        except Exception:
            pass

    # 4) rgb(...) or rgba(...)
    m = RGB_FUNC_RE.search(s)
    if m:
        parts = [p.strip() for p in m.group(1).split(',')]
        if len(parts) < 3:
            raise ValueError('rgb() needs 3 components')
        r = parse_numeric_token(parts[0])
        g = parse_numeric_token(parts[1])
        b = parse_numeric_token(parts[2])
        return (r, g, b)

    # 5) hsl(...) or hsla(...)
    m = HSL_FUNC_RE.search(s)
    if m:
        parts = [p.strip() for p in m.group(1).split(',')]
        if len(parts) < 3:
            raise ValueError('hsl() needs 3 components')
        # allow '30' or '30deg' forms for hue
        h_raw = parts[0].rstrip().lower()
        h = float(h_raw.rstrip('deg')) if h_raw.endswith(
            'deg') or h_raw.replace('.', '', 1).isdigit() else float(h_raw)
        s_comp = parts[1].strip()
        l_comp = parts[2].strip()
        s_val = float(s_comp.rstrip('%')) / 100.0 if s_comp.endswith('%') else float(s_comp)
        l_val = float(l_comp.rstrip('%')) / 100.0 if l_comp.endswith('%') else float(l_comp)
        return hsl_to_rgb_tuple(h, s_val, l_val)

    # 6) plain numeric triples like '255,0,0' or '255 0 0'
    m = PLAIN_RGB_RE.match(s)
    if m:
        r = clamp255(int(m.group(1)))
        g = clamp255(int(m.group(2)))
        b = clamp255(int(m.group(3)))
        return (r, g, b)

    # 7) fallback to provided fuzzy/name handler if given
    if fallback_text_to_rgb_fn:
        return fallback_text_to_rgb_fn(s)

    raise ValueError(f"Unrecognized color format: '{text}'")


# ---------- Flask Template (kept compact) ----------
HTML = """
<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>Color name → HEX</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css" rel="stylesheet">
    <style>
      .swatch { width: 20px; height: 20px; border-radius: 50%; display:inline-block; vertical-align: middle; border:1px solid rgba(0,0,0,0.12); }
      .preview-img { max-width:200px; max-height:150px; display:block; margin-top:10px; }
    </style>
  </head>
  <body class="bg-light">
    <div class="container py-4">
      <h3>Color name → HEX</h3>
      <p class="text-muted">Enter a color name (any text), a hex like "#ff00ff", or upload an image to extract its dominant color.</p>

      {% with messages = get_flashed_messages() %}
      {% if messages %}
        <div class="alert alert-warning" role="alert">{% for m in messages %}{{ m }}{% endfor %}</div>
      {% endif %}
      {% endwith %}

      <form method="post" enctype="multipart/form-data" class="row g-3">
        <div class="col-md-6">
          <label class="form-label">Color (text)</label>
          <input type="text" name="color_text" class="form-control" placeholder="e.g. crimson yellow, light blue, #ff00ff" value="{{ request.form.get('color_text','') }}">
          <div class="form-text">Used if no image is uploaded.</div>
        </div>

        <div class="col-md-6">
          <label class="form-label">Image (upload)</label>
          <input class="form-control" type="file" accept="image/*" name="image_file">
          <div class="form-text">Any common image format (PNG, JPG, GIF, WebP...)</div>
        </div>

        <div class="col-12">
          <button type="submit" class="btn btn-primary">Get Color</button>
          <button type="button" class="btn btn-secondary" id="clearBtn">Clear</button>
        </div>
      </form>

      {% if hex_result %}
      <hr>
      <h5>Result</h5>
      <p><strong>HEX:</strong> <span id="hexcode">{{ hex_result }}</span>
      <button class="btn btn-sm btn-outline-secondary" id="copyBtn">Copy</button>
      &nbsp;<span class="swatch" id="swatch" style="background-color: {{ hex_result }};"></span></p>
      <p><strong>RGB:</strong> {{ rgb_result }}</p>
      {% if preview_data %}
      <div>
        <strong>Uploaded image preview:</strong><br>
        <img class="preview-img" src="data:{{ preview_mime }};base64,{{ preview_data }}" alt="preview">
      </div>
      {% endif %}
      {% endif %}
    </div>

    <script>
      document.getElementById('copyBtn')?.addEventListener('click', async function() {
        const hex = document.getElementById('hexcode').innerText.trim();
        try { await navigator.clipboard.writeText(hex); this.innerText = 'Copied!'; setTimeout(()=> this.innerText = 'Copy', 1200); }
        catch (e) { alert('Copy failed: ' + e); }
      });
      document.getElementById('clearBtn')?.addEventListener('click', function(){ window.location = window.location.pathname; });
    </script>
  </body>
</html>
"""


@app.route('/', methods=['GET', 'POST'])
def index():
    hex_result = None
    rgb_result = None
    preview_b64 = None
    preview_mime = None

    if request.method == 'POST':
        file = request.files.get('image_file')
        color_text = (request.form.get('color_text') or '').strip()

        if file and file.filename:
            try:
                raw = file.read()
                preview_b64 = base64.b64encode(raw).decode('ascii')
                preview_mime = file.mimetype or 'image/png'
                rgb = image_dominant_rgb(io.BytesIO(raw))
                hexcode = rgb_to_hex(rgb)
                hex_result = hexcode
                rgb_result = f"({rgb[0]}, {rgb[1]}, {rgb[2]})"
            except Exception as e:
                flash(f"Could not process uploaded image: {e}")
        elif color_text:
            try:
                # Use the robust parser which prefers explicit formats and falls back to fuzzy names
                rgb = parse_color_input(color_text, fallback_text_to_rgb_fn=text_to_rgb_extended)
                hexcode = rgb_to_hex(rgb)
                hex_result = hexcode
                rgb_result = f"({rgb[0]}, {rgb[1]}, {rgb[2]})"
            except Exception as e:
                flash(str(e))
        else:
            flash('Please upload an image or enter a color name/hex.')

    return render_template_string(HTML, hex_result=hex_result, rgb_result=rgb_result, preview_data=preview_b64, preview_mime=preview_mime)


@app.route('/api/hex', methods=['GET', 'POST'])
def api_hex():
    """Simple API endpoint.
    GET params: q=your color text
    POST JSON: {"q": "crimson yellow"}
    Returns JSON: {"hex": "#DC143C", "rgb": [220,20,60], "input": "crimson yellow"}
    """
    if request.method == 'GET':
        q = request.args.get('q', '')
    else:
        data = request.get_json(silent=True) or request.form
        q = (data.get('q') if isinstance(data, dict) else '') or ''

    q = (q or '').strip()
    if not q:
        return jsonify({'error': 'Missing q parameter'}), 400

    # try image upload
    if 'image_file' in request.files:
        try:
            raw = request.files['image_file'].read()
            rgb = image_dominant_rgb(io.BytesIO(raw))
            return jsonify({'hex': rgb_to_hex(rgb), 'rgb': list(rgb), 'input': 'image_file'})
        except Exception as e:
            return jsonify({'error': str(e)}), 400

    try:
        rgb = parse_color_input(q, fallback_text_to_rgb_fn=text_to_rgb_extended)
        return jsonify({'hex': rgb_to_hex(rgb), 'rgb': list(rgb), 'input': q})
    except Exception as e:
        return jsonify({'error': str(e)}), 400


if __name__ == '__main__':
    app.run(debug=True, port=5000)

