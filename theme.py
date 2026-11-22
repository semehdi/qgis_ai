"""Readable colours whatever the theme.

QGIS themes (Night Mapping, Blend of Gray), the OS dark mode and stylesheets don't
always agree: the palette can say "black text" while the background is drawn dark,
for instance after switching macOS to dark mode while QGIS runs. Every colour used by
the plugin is checked against its background (WCAG contrast) and replaced when it
would be hard to read.
"""

from qgis.PyQt.QtGui import QColor, QPalette

from .compat import qenum

MIN_TEXT_CONTRAST = 4.5
MIN_LINK_CONTRAST = 3.5
LIGHT_TEXT, DARK_TEXT = "#e6e6e6", "#1f1f1f"
LIGHT_LINK, DARK_LINK = "#6cb4ff", "#0b57d0"


def _role(name):
    return qenum(QPalette, "ColorRole", name)


def luminance(color):
    """WCAG relative luminance of a QColor (0 = black, 1 = white)."""
    def channel(v):
        v /= 255.0
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    return (0.2126 * channel(color.red()) + 0.7152 * channel(color.green())
            + 0.0722 * channel(color.blue()))


def contrast(a, b):
    hi, lo = sorted((luminance(a), luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def is_dark(color):
    return luminance(color) < 0.2


def blend(a, b, t):
    return QColor(round(a.red() + (b.red() - a.red()) * t),
                  round(a.green() + (b.green() - a.green()) * t),
                  round(a.blue() + (b.blue() - a.blue()) * t))


def readable(fg, bg, minimum=MIN_TEXT_CONTRAST):
    """fg if it reads well on bg, else light or dark text to suit bg."""
    fg = QColor(fg)
    if contrast(fg, bg) >= minimum:
        return fg
    return QColor(LIGHT_TEXT if is_dark(bg) else DARK_TEXT)


def readable_link(link, bg):
    link = QColor(link)
    if contrast(link, bg) >= MIN_LINK_CONTRAST:
        return link
    return QColor(LIGHT_LINK if is_dark(bg) else DARK_LINK)


def chat_colors(palette):
    """(background, text, link, muted) for text areas, always readable together."""
    bg = QColor(palette.color(_role("Base")))
    text = readable(palette.color(_role("Text")), bg)
    link = readable_link(palette.color(_role("Link")), bg)
    return bg, text, link, blend(text, bg, 0.4)


def link_color(palette):
    """Colour for links in rich-text labels, which Qt does not take from the label's palette."""
    return readable_link(palette.color(_role("Link")), palette.color(_role("Window")))


def fixed_palette(palette):
    """A copy of palette where every text colour reads well on its background, for
    the plugin's ordinary widgets (labels, check boxes, combo boxes, line edits)."""
    pal = QPalette(palette)
    for fg, bg in (("WindowText", "Window"), ("ButtonText", "Button"), ("Text", "Base"),
                   ("ToolTipText", "ToolTipBase")):
        pal.setColor(_role(fg), readable(pal.color(_role(fg)), pal.color(_role(bg))))
    base = pal.color(_role("Base"))
    pal.setColor(_role("Link"), readable_link(pal.color(_role("Link")), pal.color(_role("Window"))))
    try:  # Qt >= 5.12
        pal.setColor(_role("PlaceholderText"), blend(pal.color(_role("Text")), base, 0.45))
    except AttributeError:
        pass
    return pal


def signature(palette):
    """Changes when any colour that matters here changes."""
    return tuple(palette.color(_role(r)).rgba() for r in (
        "Window", "WindowText", "Base", "Text", "Button", "ButtonText", "Link"))
