from flask import Flask, request, render_template_string, redirect, url_for, flash
from PIL import Image, ImageColor, ImageOps
import io
import base64
import os

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "replace-with-a-secure-random-key")


def rgb_to_hex(rgb):
    return "#{:02x}{:02x}{:02x}".format(*rgb)


def text_to_hex(text):
    """Convert CSS color name or hex-like text to normalized hex using PIL.ImageColor.
       Returns a hex string like '#rrggbb'."""
    try:
        rgb = ImageColor.getrgb(text)
        if len(rgb) == 4:
            rgb = rgb[:3]
        return rgb_to_hex(tuple(int(c) for c in rgb[:3]))
    except Exception as e:
        raise ValueError(f"Can't interpret '{text}' as a color: {e}")


def remove_alpha(im, bg=(255, 255, 255)):
    """Composite image with alpha over a background color and return RGB image."""
    if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
        bg_img = Image.new("RGBA", im.size, bg + (255,))
        im = im.convert("RGBA")
        bg_img.paste(im, mask=im.split()[-1])
        return bg_img.convert("RGB")
    return im.convert("RGB")


def image_dominant_hex(file_stream, resize_for_speed=(150, 150)):
    """Compute a dominant color for an image file-like object.
       Returns hex string '#rrggbb'."""
    im = Image.open(file_stream)
    im = remove_alpha(im)
    im = im.resize(resize_for_speed, Image.Resampling.BILINEAR)
    maxcolors = resize_for_speed[0] * resize_for_speed[1] + 1
    colors = im.getcolors(maxcolors=maxcolors)
    if not colors:
        # fallback: convert to adaptive palette and choose most common
        pal = im.convert('P', palette=Image.ADAPTIVE, colors=16)
        palette = pal.getpalette()
        color_counts = pal.getcolors()
        dominant_index = max(color_counts, key=lambda t: t[0])[1]
        r = palette[dominant_index * 3]
        g = palette[dominant_index * 3 + 1]
        b = palette[dominant_index * 3 + 2]
        rgb = (r, g, b)
    else:
        dominant = max(colors, key=lambda t: t[0])[1]
        rgb = tuple(dominant[:3])
    return rgb_to_hex(rgb)


# ---------- Flask Routes & Template ----------
HTML = """
<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>Image ↔ Hex Color Tool</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css" rel="stylesheet">
    <style>
      .swatch {
        width: 20px;
        height: 20px;
        border-radius: 50%;
        display: inline-block;
        vertical-align: middle;
        border: 1px solid rgba(0,0,0,0.15);
      }
      .preview-img { max-width: 200px; max-height: 150px; display:block; margin-top:10px; }
    </style>
  </head>
  <body class="bg-light">
    <div class="container py-4">
      <h3>Image → Hex Color converter</h3>
      <p class="text-muted">Upload an image (preferred) or type a color name/hex. Result shows HEX and a 20×20 round swatch.</p>

      {% with messages = get_flashed_messages() %}
      {% if messages %}
        <div class="alert alert-warning" role="alert">
          {% for m in messages %}{{ m }}{% endfor %}
        </div>
      {% endif %}
      {% endwith %}

      <form method="post" enctype="multipart/form-data" class="row g-3">
        <div class="col-md-6">
          <label class="form-label">Color (text)</label>
          <input type="text" name="color_text" class="form-control" placeholder="e.g. black, #ff00ff, rgb(255,0,0)" value="{{ request.form.get('color_text','') }}">
          <div class="form-text">Used if no image is uploaded.</div>
        </div>

        <div class="col-md-6">
          <label class="form-label">Image (upload)</label>
          <input class="form-control" type="file" accept="image/*" name="image_file" id="image_file">
          <div class="form-text">Any common image format (PNG, JPG, GIF, WebP...)</div>
        </div>

        <div class="col-12">
          <button type="submit" class="btn btn-primary">Get Hex Color</button>
          <button type="button" class="btn btn-secondary" id="clearBtn">Clear</button>
        </div>
      </form>

      {% if hex_result %}
      <hr>
      <h5>Result</h5>
      <p>
        <strong>HEX:</strong>
        <span id="hexcode">{{ hex_result }}</span>
        <button class="btn btn-sm btn-outline-secondary" id="copyBtn">Copy</button>
        &nbsp;
        <span class="swatch" id="swatch" style="background-color: {{ hex_result }};"></span>
      </p>

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
        try {
          await navigator.clipboard.writeText(hex);
          this.innerText = 'Copied!';
          setTimeout(()=> this.innerText = 'Copy', 1200);
        } catch (e) {
          alert('Copy failed: ' + e);
        }
      });
      document.getElementById('clearBtn').addEventListener('click', function(){
        window.location = window.location.pathname;
      });
    </script>
  </body>
</html>
"""


@app.route("/", methods=["GET", "POST"])
def index():
    hex_result = None
    preview_b64 = None
    preview_mime = None

    if request.method == "POST":
        file = request.files.get("image_file")
        color_text = (request.form.get("color_text") or "").strip()

        if file and file.filename != "":
            try:
                raw = file.read()
                file_stream = io.BytesIO(raw)
                hexcode = image_dominant_hex(file_stream)
                preview_b64 = base64.b64encode(raw).decode('ascii')
                preview_mime = file.mimetype or 'image/png'
                hex_result = hexcode
            except Exception as e:
                flash(f"Could not process uploaded image: {e}")
        elif color_text:
            try:
                hexcode = text_to_hex(color_text)
                hex_result = hexcode
            except Exception as e:
                flash(str(e))
        else:
            flash("Please upload an image or enter a color name/hex.")
    return render_template_string(HTML, hex_result=hex_result, preview_data=preview_b64, preview_mime=preview_mime)


if __name__ == "__main__":
    app.run(debug=True, port=5000)

