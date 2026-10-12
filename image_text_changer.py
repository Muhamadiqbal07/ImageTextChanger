"""Image Text Changer (fixed).

Fixes over the previous version
-------------------------------
1. Layout: every control is now created INSIDE its row frame.  Previously the
   widgets were created with `inner` as their master but packed separately
   from the label frames, which produced the diagonal "staircase" layout.
2. The loaded source image is now drawn as the base layer of the preview
   (it was commented out before, so the source never showed).
3. "Text Scale" now scales ONLY the text layer, never the whole composited
   image, so the plate/background is no longer squashed/distorted.
4. Character-by-character drawing uses textlength() advances and Pillow's
   stroke_width outline (single pass) - this fixes the garbled, overlapping
   characters and is far faster than the old 49-pass outline loop.
5. Font family strings such as "FOT-Yuruka Std UB, serif" are resolved to a
   real font file by scanning the system font directories, with graceful
   fallbacks (family name -> file name -> arial/dejavu -> default).
6. "Preset Character" is a dropdown with Japanese -> English presets and the
   "Auto-Translate to English" checkbox is functional.
7. Numeric fields are read safely while typing (no silent TclError crashes)
   and redraws are debounced through variable traces.
"""

import os
import sys

import customtkinter as ctk
from tkinter import filedialog, font as tkfont
from PIL import Image, ImageDraw, ImageFont

try:
    import pytesseract

    if sys.platform.startswith("win"):
        _TESS = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
        if os.path.exists(_TESS):
            pytesseract.pytesseract.tesseract_cmd = _TESS
except Exception:  # OCR is optional
    pytesseract = None

# --------------------------------------------------------------------------
# Theme (colours sampled from the target screenshot)
# --------------------------------------------------------------------------
WINDOW_BG    = "#26262e"
PANEL_BORDER = "#b37b42"
HEADER_FG    = "#f2a65a"
PREVIEW_BG   = "#2b2b2b"
LABEL_W      = 110

TEXT_FILL    = (255, 255, 255, 255)
TEXT_OUTLINE = (80, 50, 30, 255)
OUTLINE_W    = 3

# Preset characters: Japanese id shown in the dropdown -> English translation
PRESETS = {
    "他校の男子生徒a (Male Student A)":   "Other Male Student A",
    "他校の男子生徒b (Male Student B)":   "Other Male Student B",
    "他校の女子生徒a (Female Student A)": "Other School Female Student A",
    "同校の男子生徒a (Male Student A)":   "Same-School Male Student A",
    "同校の女子生徒a (Female Student A)": "Same-School Female Student A",
    "先生 (Teacher)":                     "Teacher",
}


# --------------------------------------------------------------------------
# Font resolution: map family names ("FOT-Yuruka Std UB, serif") to real files
# --------------------------------------------------------------------------
class FontResolver:
    EXTENSIONS = (".ttf", ".otf", ".ttc")

    def __init__(self):
        self._family_map = None   # lower-case family name -> file path
        self._file_map = None     # lower-case file name   -> file path

    # -- scanning ----------------------------------------------------------
    @staticmethod
    def _font_dirs():
        dirs = []
        if sys.platform.startswith("win"):
            windir = os.environ.get("WINDIR", r"C:\Windows")
            dirs.append(os.path.join(windir, "Fonts"))
            local = os.environ.get("LOCALAPPDATA", "")
            if local:
                dirs.append(os.path.join(local, "Microsoft", "Windows", "Fonts"))
        elif sys.platform == "darwin":
            dirs += ["/System/Library/Fonts", "/Library/Fonts",
                     os.path.expanduser("~/Library/Fonts")]
        else:
            dirs += ["/usr/share/fonts", "/usr/local/share/fonts",
                     os.path.expanduser("~/.fonts"),
                     os.path.expanduser("~/.local/share/fonts")]
        return [d for d in dirs if d and os.path.isdir(d)]

    def _scan(self):
        files = {}
        for directory in self._font_dirs():
            for root, _, names in os.walk(directory):
                for name in names:
                    if name.lower().endswith(self.EXTENSIONS):
                        files.setdefault(name.lower(), os.path.join(root, name))
        families = {}
        for path in files.values():
            try:
                family = ImageFont.truetype(path, 12).getname()[0].lower()
                families.setdefault(family, path)
            except Exception:
                continue
        self._family_map, self._file_map = families, files

    def _ensure(self):
        if self._family_map is None:
            self._scan()

    @property
    def families(self):
        self._ensure()
        return sorted(self._family_map)

    # -- resolving ---------------------------------------------------------
    def resolve(self, family_string, size):
        """Return a PIL font for a CSS-like family string, with fallbacks."""
        self._ensure()
        for part in str(family_string).split(","):
            part = part.strip().strip("'\"")
            if not part:
                continue
            low = part.lower()
            if low in self._family_map:                      # by family name
                return ImageFont.truetype(self._family_map[low], size)
            for ext in self.EXTENSIONS:                      # by file name
                if low + ext in self._file_map:
                    return ImageFont.truetype(self._file_map[low + ext], size)
            if os.path.isfile(part):                         # by full path
                return ImageFont.truetype(part, size)
        for fallback in ("arial", "liberation sans", "dejavu sans",
                         "noto sans", "segui ui", "verdana"):
            if fallback in self._family_map:
                return ImageFont.truetype(self._family_map[fallback], size)
        if self._family_map:  # last resort: the first font we found
            return ImageFont.truetype(next(iter(self._family_map.values())), size)
        return ImageFont.load_default()


# --------------------------------------------------------------------------
# Pure rendering (no Tk involved -> easy to test)
# --------------------------------------------------------------------------
def make_checkerboard(w, h, cell=8, c1=(166, 166, 166, 255), c2=(130, 130, 130, 255)):
    """Transparency backdrop for the on-screen preview only."""
    img = Image.new("RGBA", (w, h), c1)
    px = img.load()
    for y in range(h):
        for x in range(w):
            if ((x // cell) + (y // cell)) % 2:
                px[x, y] = c2
    return img


def render_plate(source, plate_w, plate_h, off_x, off_y,
                 text, font, scale, spacing, margin_x, offset_y,
                 outline_width=OUTLINE_W):
    """Compose one nameplate frame.

    * the source image is pasted UN-SCALED at (off_x, off_y)  -> no distortion
    * the text is rendered on its own layer, that layer alone is horizontally
      scaled by `scale`, then composited at (margin_x, 0)
    """
    base = Image.new("RGBA", (plate_w, plate_h), (0, 0, 0, 0))
    if source is not None:
        base.paste(source, (off_x, off_y), source)   # clip-safe, never stretched

    if text:
        layer = Image.new("RGBA", (plate_w, plate_h), (0, 0, 0, 0))
        draw = ImageDraw.Draw(layer)
        ascent, descent = font.getmetrics()
        top = (plate_h - (ascent + descent)) // 2
        # anchor "la": the y given IS the ascent line (top of the em box),
        # so the text block spans y .. y + ascent + descent
        y = top + offset_y
        x = 0.0
        for ch in text:
            draw.text((x, y), ch, font=font, anchor="la",
                      fill=TEXT_FILL, stroke_width=outline_width,
                      stroke_fill=TEXT_OUTLINE)
            x += draw.textlength(ch, font=font) + spacing

        if scale > 0 and abs(scale - 1.0) > 1e-6:     # scale TEXT ONLY
            new_w = max(1, round(plate_w * scale))
            layer = layer.resize((new_w, plate_h), Image.Resampling.LANCZOS)

        full = Image.new("RGBA", (plate_w, plate_h), (0, 0, 0, 0))
        full.paste(layer, (margin_x, 0), layer)      # left margin applied post-scale
        base.alpha_composite(full)
    return base


# --------------------------------------------------------------------------
# The application
# --------------------------------------------------------------------------
class ImageTextChangerApp(ctk.CTk):
    def __init__(self):
        super().__init__()

        self.title("Image Text Changer")
        self.geometry("900x680")
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")
        self.configure(fg_color=WINDOW_BG)

        # state
        self.source_image_path = None
        self.original_image = None
        self.preview_image = None
        self.font_resolver = FontResolver()
        self._tk_img = None
        self._preview_job = None
        self._last_good = {}

        # UI variables (defaults taken from the target screenshot)
        self.var_preset = ctk.StringVar(value=list(PRESETS)[0])
        self.var_auto_translate = ctk.BooleanVar(value=True)
        self.var_displayed_name = ctk.StringVar(value="Other Male Student A")
        self.var_font_family = ctk.StringVar(value="FOT-Yuruka Std UB, serif")
        self.var_font_size = ctk.DoubleVar(value=24.8)
        self.var_text_scale = ctk.DoubleVar(value=0.71)
        self.var_letter_spacing = ctk.IntVar(value=-3)
        self.var_margin_left = ctk.IntVar(value=16)
        self.var_text_offset_y = ctk.IntVar(value=-7)

        self.var_plate_width = ctk.IntVar(value=208)
        self.var_plate_height = ctk.IntVar(value=42)
        self.var_plate_offset_x = ctk.IntVar(value=0)
        self.var_plate_offset_y = ctk.IntVar(value=0)

        self.create_widgets()

        # any change to any variable -> debounced re-render
        for var in (self.var_preset, self.var_auto_translate, self.var_displayed_name,
                    self.var_font_family, self.var_font_size, self.var_text_scale,
                    self.var_letter_spacing, self.var_margin_left, self.var_text_offset_y,
                    self.var_plate_width, self.var_plate_height,
                    self.var_plate_offset_x, self.var_plate_offset_y):
            var.trace_add("write", self._schedule_preview)

        self.generate_preview()

    # ------------------------------------------------------------------ UI
    def create_widgets(self):
        # --- TOP SECTION (image display) ---------------------------------
        self.top_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.top_frame.pack(pady=20, padx=20, fill="x")

        self.image_label = ctk.CTkLabel(self.top_frame, text="No Image Loaded",
                                        width=340, height=96,
                                        fg_color=PREVIEW_BG, corner_radius=8)
        self.image_label.pack(pady=10)

        self.dimensions_label = ctk.CTkLabel(self.top_frame,
                                             text="Dimensions: 0 x 0 px",
                                             text_color="gray")
        self.dimensions_label.pack()

        btn_frame = ctk.CTkFrame(self.top_frame, fg_color="transparent")
        btn_frame.pack(pady=5)
        ctk.CTkButton(btn_frame, text="Load Source Image",
                      command=self.load_image).pack(side="left", padx=5)
        ctk.CTkButton(btn_frame, text="Save Output Image", command=self.save_image,
                      fg_color="#b37b42", hover_color="#8c5f31").pack(side="left", padx=5)

        # --- BOTTOM SECTION (settings panels) ----------------------------
        self.bottom_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.bottom_frame.pack(pady=10, padx=20, fill="both", expand=True)

        self.left_panel = ctk.CTkFrame(self.bottom_frame, border_width=1,
                                       border_color=PANEL_BORDER)
        self.left_panel.pack(side="left", fill="both", expand=True, padx=(0, 10))
        self._panel_title(self.left_panel, "Text & Translation")
        self.build_left_panel_controls()

        self.right_panel = ctk.CTkFrame(self.bottom_frame, border_width=1,
                                        border_color=PANEL_BORDER)
        self.right_panel.pack(side="right", fill="both", expand=True, padx=(10, 0))
        self._panel_title(self.right_panel, "Plate Asset Geometry")
        self.build_right_panel_controls()

    def _panel_title(self, panel, text):
        ctk.CTkLabel(panel, text=f" {text} ", text_color=HEADER_FG,
                     font=("Arial", 13, "bold"), fg_color=WINDOW_BG
                     ).place(x=12, y=-9)

    def add_row(self, parent, label_text, build, width=None):
        """One settings row: label left, control right.

        `build(master)` must CREATE the widget with the row frame as master -
        this was the bug in the old version (widgets were created with the
        outer container as master and packed separately -> staircase layout).
        """
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", pady=6)
        ctk.CTkLabel(row, text=label_text, width=LABEL_W, anchor="w",
                     justify="left", wraplength=LABEL_W - 8
                     ).pack(side="left", padx=(6, 8))
        widget = build(row)
        widget.pack(side="right", padx=6)
        return widget

    def build_left_panel_controls(self):
        inner = ctk.CTkFrame(self.left_panel, fg_color="transparent")
        inner.pack(fill="both", expand=True, padx=16, pady=26)

        self.preset_menu = self.add_row(
            inner, "Preset Character",
            lambda m: ctk.CTkOptionMenu(m, variable=self.var_preset,
                                        values=list(PRESETS),
                                        command=self.on_preset_change,
                                        width=210, fg_color="#34363c",
                                        button_color="#4a4d55",
                                        button_hover_color="#5a5e68"))

        # Auto-translate row: text label left, checkbox right (as in screenshot)
        cb_row = ctk.CTkFrame(inner, fg_color="transparent")
        cb_row.pack(fill="x", pady=6)
        ctk.CTkLabel(cb_row, text="Auto-Translate to English", width=LABEL_W,
                     anchor="w", justify="left", wraplength=LABEL_W - 8
                     ).pack(side="left", padx=(6, 8))
        ctk.CTkCheckBox(cb_row, text="", variable=self.var_auto_translate,
                        command=self.on_translate_toggle, width=26, height=26,
                        fg_color=HEADER_FG, hover_color="#d98f45",
                        border_color=HEADER_FG, checkmark_color="#26262e",
                        hover=False
                        ).pack(side="right", padx=6)

        self.entry_displayed = self.add_row(
            inner, "Displayed Name",
            lambda m: ctk.CTkEntry(m, textvariable=self.var_displayed_name, width=200))

        self.add_row(
            inner, "Font Family",
            lambda m: ctk.CTkComboBox(m, variable=self.var_font_family,
                                      values=self._combo_font_values(), width=200))

        self.entry_font_size = self.add_row(
            inner, "Font Size (px)",
            lambda m: ctk.CTkEntry(m, textvariable=self.var_font_size,
                                   width=90, justify="center"))
        self.add_row(inner, "Text Scale",
                     lambda m: ctk.CTkEntry(m, textvariable=self.var_text_scale,
                                            width=90, justify="center"))
        self.add_row(inner, "Letter Spacing (px)",
                     lambda m: ctk.CTkEntry(m, textvariable=self.var_letter_spacing,
                                            width=90, justify="center"))
        self.add_row(inner, "Text Left Margin (px)",
                     lambda m: ctk.CTkEntry(m, textvariable=self.var_margin_left,
                                            width=90, justify="center"))
        self.add_row(inner, "Offset Y (px)",
                     lambda m: ctk.CTkEntry(m, textvariable=self.var_text_offset_y,
                                            width=90, justify="center"))

    def build_right_panel_controls(self):
        inner = ctk.CTkFrame(self.right_panel, fg_color="transparent")
        inner.pack(fill="both", expand=True, padx=16, pady=26)

        for label, var in (("Plate Width (px)", self.var_plate_width),
                           ("Plate Height (px)", self.var_plate_height),
                           ("Offset X (px)", self.var_plate_offset_x),
                           ("Offset Y (px)", self.var_plate_offset_y)):
            self.add_row(inner, label,
                         lambda m, v=var: ctk.CTkEntry(m, textvariable=v,
                                                       width=90, justify="center"))

    def _combo_font_values(self):
        try:
            return sorted(set(tkfont.families()))
        except Exception:
            return ["Arial", "serif"]

    # ------------------------------------------------------------ callbacks
    def _schedule_preview(self, *args):
        if self._preview_job is not None:
            try:
                self.after_cancel(self._preview_job)
            except Exception:
                pass
        self._preview_job = self.after(80, self._run_preview)

    def _run_preview(self):
        self._preview_job = None
        self.generate_preview()

    def _num(self, var, default):
        """Read a numeric Tk var safely (users type '-' or '.' mid-edit)."""
        key = var._name  # Tcl variable name (Tk vars are not hashable)
        try:
            value = float(var.get())
            self._last_good[key] = value
            return value
        except Exception:
            return self._last_good.get(key, default)

    def on_preset_change(self, value):
        if self.var_auto_translate.get():
            self.var_displayed_name.set(PRESETS.get(value, value))

    def on_translate_toggle(self):
        if self.var_auto_translate.get():
            self.var_displayed_name.set(PRESETS.get(self.var_preset.get(),
                                                    self.var_displayed_name.get()))

    # ------------------------------------------------------------- load/save
    def load_image(self):
        file_path = filedialog.askopenfilename(
            filetypes=[("Images", "*.png *.jpg *.jpeg *.webp")])
        if not file_path:
            return
        try:
            image = Image.open(file_path)
        except Exception as exc:
            print(f"Could not open image: {exc}")
            return
        self.source_image_path = file_path
        self.set_source(image)

    def set_source(self, image):
        """Programmatic entry point used by load_image() and the tests."""
        self.original_image = image.convert("RGBA")

        w, h = self.original_image.size
        self.dimensions_label.configure(text=f"Dimensions: {w} × {h} px")
        self.var_plate_width.set(w)
        self.var_plate_height.set(h)

        # OCR: auto-detect the original text (optional, best effort)
        if pytesseract is not None:
            detected = ""
            for lang in ("jpn+eng", "eng", None):
                try:
                    detected = (pytesseract.image_to_string(self.original_image, lang=lang)
                                if lang else pytesseract.image_to_string(self.original_image))
                    detected = " ".join(detected.split())
                    break
                except Exception as exc:
                    print(f"OCR ({lang}) failed: {exc}")
            if detected:
                self._apply_ocr(detected)

        self.generate_preview()

    def _apply_ocr(self, detected):
        # match against known presets (Japanese part before the parenthesis)
        for key, english in PRESETS.items():
            japanese = key.split(" (")[0]
            if japanese and japanese in detected:
                self.var_preset.set(key)
                if self.var_auto_translate.get():
                    self.var_displayed_name.set(english)
                return
        # unknown text: add it as a custom preset
        custom = f"{detected} (OCR)"
        if custom not in PRESETS:
            PRESETS[custom] = detected
            self.preset_menu.configure(values=list(PRESETS))
        self.var_preset.set(custom)
        self.var_displayed_name.set(detected)

    def save_image(self):
        if self.preview_image is None:
            return
        file_path = filedialog.asksaveasfilename(
            defaultextension=".png",
            filetypes=[("PNG Image", "*.png")],
            initialfile="translated_nameplate.png")
        if file_path:
            self.preview_image.save(file_path, "PNG")

    # --------------------------------------------------------------- render
    def generate_preview(self):
        plate_w = int(self._num(self.var_plate_width, 208))
        plate_h = int(self._num(self.var_plate_height, 42))
        if plate_w <= 0 or plate_h <= 0:
            return
        off_x = int(self._num(self.var_plate_offset_x, 0))
        off_y = int(self._num(self.var_plate_offset_y, 0))

        text = self.var_displayed_name.get()
        size = max(4.0, self._num(self.var_font_size, 24.8))
        scale = self._num(self.var_text_scale, 1.0)
        spacing = self._num(self.var_letter_spacing, 0)
        margin_x = int(self._num(self.var_margin_left, 16))
        offset_y = int(self._num(self.var_text_offset_y, 0))

        font = self.font_resolver.resolve(self.var_font_family.get(), size)

        img = render_plate(self.original_image, plate_w, plate_h, off_x, off_y,
                           text, font, scale, spacing, margin_x, offset_y)
        self.preview_image = img

        # checkerboard backdrop so transparency is visible, like the screenshot
        display = make_checkerboard(plate_w, plate_h)
        display.alpha_composite(img)

        self._tk_img = ctk.CTkImage(light_image=display, dark_image=display,
                                    size=(plate_w, plate_h))
        self.image_label.configure(image=self._tk_img, text="")


def main():
    app = ImageTextChangerApp()
    app.mainloop()


if __name__ == "__main__":
    main()
