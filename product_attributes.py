import html
import re
from html.parser import HTMLParser


class _HTMLTextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []

    def handle_data(self, data):
        if data:
            self.parts.append(data)

    def handle_starttag(self, tag, attrs):
        if tag in {"br", "p", "li", "tr", "div", "h1", "h2", "h3"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"p", "li", "tr", "div", "h1", "h2", "h3"}:
            self.parts.append("\n")

    def text(self):
        return " ".join("".join(self.parts).split())


def html_to_text(value):
    if value is None:
        return ""
    raw = html.unescape(str(value))
    parser = _HTMLTextExtractor()
    try:
        parser.feed(raw)
        text = parser.text()
    except Exception:
        text = re.sub(r"<[^>]+>", " ", raw)
    return " ".join(text.split())


def description_to_text(value):
    if value is None:
        return ""
    if isinstance(value, str):
        return html_to_text(value)
    if isinstance(value, list):
        return "\n".join(filter(None, (description_to_text(item) for item in value)))
    if isinstance(value, dict):
        parts = []
        if isinstance(value.get("content"), str):
            parts.append(html_to_text(value["content"]))
        for key in ("sections", "items", "description", "longDescription", "shortDescription"):
            if key in value:
                parts.append(description_to_text(value[key]))
        if not parts:
            for child in value.values():
                if isinstance(child, (dict, list)):
                    parts.append(description_to_text(child))
        return "\n".join(filter(None, parts))
    return ""


def description_primary_section_text(value):
    """Return the product-description section, excluding color palette/company sections."""
    if isinstance(value, dict) and isinstance(value.get("sections"), list):
        for section in value["sections"]:
            text = description_to_text(section)
            if text:
                return text
        return ""
    return description_to_text(value)


_MATERIAL_PATTERNS = [
    (re.compile(r"\bCARBON\b|\bwłókn(?:o|em|a)\s+węglow(?:e|ym|ego|ych)\b|\bwlokn(?:o|em|a)\s+weglow(?:e|ym|ego|ych)\b", re.I), "CARBON"),
    (re.compile(r"\bFLEX\b(?!\s+Mini\b)|\bTPU\b|\bgum(?:a|y|owy|owa|owe|owego|owym)\b", re.I), "FLEX"),
    (re.compile(r"\bPLA\s*(?:\+|PLUS)\b", re.I), "PLA+"),
    (re.compile(r"\bPET\s*-?\s*G\b", re.I), "PETG"),
    (re.compile(r"\bPLA\b", re.I), "PLA"),
    (re.compile(r"\bASA\b", re.I), "ASA"),
    (re.compile(r"\bABS\b", re.I), "ABS"),
    (re.compile(r"\bPCTG\b", re.I), "PCTG"),
    (re.compile(r"\bHIPS\b", re.I), "HIPS"),
    (re.compile(r"\bNYLON\b|\bPA\b", re.I), "PA/Nylon"),
    (re.compile(r"\bPC\b", re.I), "PC"),
]

_COLOR_PATTERNS = [
    (re.compile(r"\bwielokolorow(?:y|a|e|ego|ym|ą)\b|\bmultikolor\b|\bmulti\s*color\b|\bkilku kolorach\b|\bkolorach do wyboru\b", re.I), "wielokolorowy"),
    (re.compile(r"\bczarn(?:y|a|e|ego|ym|ą)\b", re.I), "czarny"),
    (re.compile(r"\bbia(?:ł|l)(?:y|a|e|ego|ym|ą)\b|\bbiel(?:ą|a|i)?\b", re.I), "biały"),
    (re.compile(r"\bszar(?:y|a|e|ego|ym|ą)\b", re.I), "szary"),
    (re.compile(r"\bgrafitow(?:y|a|e|ego|ym|ą)\b|\bgrafit\b", re.I), "grafitowy"),
    (re.compile(r"\bczerwon(?:y|a|e|ego|ym|ą)\b", re.I), "czerwony"),
    (re.compile(r"\bzielon(?:y|a|e|ego|ym|ą)\b|\bzieleń\b|\bzieleni(?:ą)?\b", re.I), "zielony"),
    (re.compile(r"\bniebiesk(?:i|a|ie|iego|im|ą)\b|\bgranatow(?:y|a|e|ego|ym|ą)\b", re.I), "niebieski"),
    (re.compile(r"\bżółt(?:y|a|e|ego|ym|ą)\b|\bzolt(?:y|a|e|ego|ym|a)\b", re.I), "żółty"),
    (re.compile(r"\bpomarańczow(?:y|a|e|ego|ym|ą)\b|\bpomaranczow(?:y|a|e|ego|ym|a)\b|\bmandarynkow(?:y|a|e|ego|ym|ą)\b", re.I), "pomarańczowy"),
    (re.compile(r"\bfioletow(?:y|a|e|ego|ym|ą)\b", re.I), "fioletowy"),
    (re.compile(r"\bróżow(?:y|a|e|ego|ym|ą)\b|\brozow(?:y|a|e|ego|ym|a)\b|\bmagenta\b", re.I), "różowy"),
    (re.compile(r"\bbrązow(?:y|a|e|ego|ym|ą)\b|\bbrazow(?:y|a|e|ego|ym|a)\b", re.I), "brązowy"),
    (re.compile(r"\bbeżow(?:y|a|e|ego|ym|ą)\b|\bbezow(?:y|a|e|ego|ym|a)\b", re.I), "beżowy"),
    (re.compile(r"\bsrebrn(?:y|a|e|ego|ym|ą)\b", re.I), "srebrny"),
    (re.compile(r"\bzłot(?:y|a|e|ego|ym|ą)\b|\bzlot(?:y|a|e|ego|ym|a)\b", re.I), "złoty"),
    (re.compile(r"\btransparentn(?:y|a|e|ego|ym|ą)\b|\bprzezroczyst(?:y|a|e|ego|ym|ą)\b", re.I), "transparentny"),
    (re.compile(r"\bnaturaln(?:y|a|e|ego|ym|ą)\b", re.I), "naturalny"),
]

_COLOR_PALETTES = {
    "PLA": [
        ("Ciemnoczerwony", "czerwony"),
        ("Ciemnoniebieski", "niebieski"),
        ("Jasnoniebieski", "niebieski"),
        ("Ciemnozielony", "zielony"),
        ("Jasnozielony", "zielony"),
        ("Czarny", "czarny"),
        ("Srebrny", "srebrny"),
        ("Szary", "szary"),
        ("Biały", "biały"),
        ("Niebieski", "niebieski"),
        ("Żółty", "żółty"),
        ("Pomarańczowy", "pomarańczowy"),
        ("Różowy", "różowy"),
        ("Fioletowy", "fioletowy"),
        ("Brązowy", "brązowy"),
        ("Beżowy", "beżowy"),
        ("Złoty", "złoty"),
    ],
    "PETG": [
        ("Zielony Przezroczysty", "zielony"),
        ("Pomarańczowy Przezroczysty", "pomarańczowy"),
        ("Czerwony Przezroczysty", "czerwony"),
        ("Niebieski Przezroczysty", "niebieski"),
        ("Przezroczysty", "transparentny"),
        ("Pomarańczowy", "pomarańczowy"),
        ("Grafitowy", "grafitowy"),
        ("Srebrny", "srebrny"),
        ("Brązowy", "brązowy"),
        ("Oliwkowy", "zielony"),
        ("Czerwony", "czerwony"),
        ("Różowy", "różowy"),
        ("Niebieski", "niebieski"),
        ("Zielony", "zielony"),
        ("Czarny", "czarny"),
        ("Szary", "szary"),
        ("Biały", "biały"),
        ("Żółty", "żółty"),
    ],
}


def _first_material(text):
    for pattern, value in _MATERIAL_PATTERNS:
        if pattern.search(text):
            return value
    return ""


def _resolve_colors(colors):
    if "wielokolorowy" in colors:
        return "wielokolorowy"
    unique_colors = set(colors)
    if len(unique_colors) > 1:
        return "wielokolorowy"
    if colors:
        return colors[0]
    return ""


def _palette_entries(material):
    key = "PLA" if material in {"PLA", "PLA+"} else material
    if key in _COLOR_PALETTES:
        return _COLOR_PALETTES[key]
    entries = []
    for palette in _COLOR_PALETTES.values():
        entries.extend(palette)
    return entries


def _first_color(text, material=""):
    colors = []
    masked_text = text or ""
    occupied_spans = []
    palette_entries = sorted(_palette_entries(material), key=lambda item: len(item[0]), reverse=True)
    for phrase, value in palette_entries:
        pattern = re.compile(rf"(?<!\w){re.escape(phrase)}(?!\w)", re.I)
        for match in pattern.finditer(masked_text):
            span = match.span()
            if any(start < span[1] and span[0] < end for start, end in occupied_spans):
                continue
            occupied_spans.append(span)
            colors.append(value)
    if occupied_spans:
        chars = list(masked_text)
        for start, end in occupied_spans:
            chars[start:end] = " " * (end - start)
        masked_text = "".join(chars)
    for pattern, value in _COLOR_PATTERNS:
        if pattern.search(masked_text):
            colors.append(value)
    return _resolve_colors(colors)


def parse_material_color(text):
    clean = " ".join((text or "").split())
    if not clean:
        return {"material": "", "color": ""}

    material = ""
    color = ""
    label_material = re.search(
        r"\bmateria(?:ł|l)\b\s*[:\-–]?\s*(.{1,180}?)(?:\s+Personalizacja\b|\s+Kolor\b|\s+Cechy\b|\s+Zastosowanie\b|[.;|\n]|$)",
        clean,
        flags=re.I,
    )
    if label_material:
        material = _first_material(label_material.group(1))
    label_color = re.search(
        r"(?:\bkolor\b\s*[:\-–]\s*|\bdomyślny\s+kolor\s+to\s+|\bdomyslny\s+kolor\s+to\s+)([^.;|\n]{1,100})",
        clean,
        flags=re.I,
    )
    if label_color:
        color = _first_color(label_color.group(1), material)

    if not material:
        material = _first_material(clean)
    if not color:
        color_context = re.split(
            r"\bNasza\s+bogata\s+gama\s+kolorów\b|\bNasza\s+bogata\s+gama\s+kolorow\b|\bUwaga\s*:\s*Numery\s+przy\s+kolorach\b",
            clean,
            maxsplit=1,
            flags=re.I,
        )[0]
        color = _first_color(color_context, material)
    if material in {"FLEX", "CARBON"}:
        color = "czarny"
    return {"material": material, "color": color}
