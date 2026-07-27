import hashlib
import html
import re
import unicodedata
from html.parser import HTMLParser

from description_checks import description_preview


MAX_PALETTE_BLOCK_CHARS = 20_000
_BLOCK_START_RE = re.compile(r"Nasze\s+wydruki\s+z\s+materia(?:ł|l)u\b", re.I)
_BLOCK_END_RE = re.compile(
    r"co\s+ułatwi\s+i\s+przyspieszy\s+realizację\s*\.", re.I
)
_COMPANY_START_RE = re.compile(r"Odkryj\s+fascynujący\s+świat\s+druku\s+3D", re.I)
_MATERIAL_RE = re.compile(r"materia(?:ł|l)u\s+(PLA\s*\+|PLA|PET\s*-?\s*G)\b", re.I)

_NOTICE = (
    "Uwaga: Numery przy kolorach odpowiadają naszej palecie barw. "
    "Przy składaniu zamówienia prosimy o podanie numeru wybranego koloru, "
    "co ułatwi i przyspieszy realizację."
)

_PALETTE_COLORS = {
    "PLA": (
        "Czarny",
        "Biały",
        "Szary",
        "Srebrny",
        "Czerwony",
        "Zielony",
        "Niebieski",
        "Żółty",
        "Pomarańczowy",
        "Różowy",
        "Fioletowy",
        "Brązowy",
        "Beżowy",
        "Złoty",
        "Granatowy",
        "Jasnoniebieski",
        "Jasnozielony",
        "Ciemnozielony",
        "Ciemnoczerwony",
        "Naturalny",
    ),
    "PETG": (
        "Czarny",
        "Biały",
        "Szary",
        "Srebrny",
        "Grafitowy",
        "Przezroczysty",
        "Czerwony",
        "Zielony",
        "Niebieski",
        "Żółty",
        "Pomarańczowy",
        "Różowy",
        "Brązowy",
        "Oliwkowy",
        "Granatowy",
        "Zielony Przezroczysty",
        "Niebieski Przezroczysty",
        "Czerwony Przezroczysty",
        "Pomarańczowy Przezroczysty",
        "Naturalny",
    ),
}

_MATERIAL_COPY = {
    "PLA": (
        "Nasze wydruki z materiału PLA są przeznaczone do typowych zastosowań "
        "dekoracyjnych i prototypowych. Wytrzymują temperatury do 60°C. "
        "Dlaczego warto wybrać PLA? "
        "Łatwość druku i precyzja: PLA pozwala uzyskać czytelne detale i równą powierzchnię. "
        "Odporność na ścieranie: Parametry gotowego elementu zależą od modelu i ustawień druku. "
        "Ekologiczny materiał: Informacje o pochodzeniu i utylizacji sprawdź w dokumentacji producenta filamentu."
    ),
    "PETG": (
        "Nasze wydruki z materiału PETG są przeznaczone do zastosowań wymagających "
        "większej odporności mechanicznej. Wytrzymują temperatury do 85°C. "
        "Dlaczego warto wybrać PETG? "
        "Odporność na wysokie temperatury: Parametry zależą od konkretnego filamentu i geometrii elementu. "
        "Wyjątkowa wytrzymałość: PETG zwykle zapewnia większą elastyczność i udarność niż PLA."
    ),
}

_BENEFIT_PREFIXES = {
    "PLA": (
        "Łatwość druku i precyzja:",
        "Odporność na ścieranie:",
        "Ekologiczny materiał:",
    ),
    "PETG": (
        "Odporność na wysokie temperatury:",
        "Wyjątkowa wytrzymałość:",
    ),
}


class _PaletteStructureParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.counts = {"p": 0, "ul": 0, "ol": 0, "ul_li": 0, "ol_li": 0}

    def handle_starttag(self, tag, attrs):
        del attrs
        tag = tag.casefold()
        if tag in {"p", "ul", "ol"}:
            self.counts[tag] += 1
        if tag == "li":
            if "ol" in self.stack:
                self.counts["ol_li"] += 1
            elif "ul" in self.stack:
                self.counts["ul_li"] += 1
        self.stack.append(tag)

    def handle_endtag(self, tag):
        tag = tag.casefold()
        if tag in self.stack:
            index = len(self.stack) - 1 - self.stack[::-1].index(tag)
            self.stack = self.stack[:index]


def normalize_palette_material(value):
    compact = re.sub(r"[\s-]+", "", str(value or "").upper())
    if compact in {"PLA", "PLA+"}:
        return compact
    if compact == "PETG":
        return "PETG"
    return ""


def canonical_material_palette_text(material):
    normalized = normalize_palette_material(material)
    template_key = "PLA" if normalized == "PLA+" else normalized
    if template_key not in _MATERIAL_COPY:
        return ""
    colors = " ".join(_PALETTE_COLORS[template_key])
    return (
        f"{_MATERIAL_COPY[template_key]} "
        f"Nasza bogata gama kolorów {template_key}: {colors} {_NOTICE}"
    )


def canonical_material_palette_html(material):
    normalized = normalize_palette_material(material)
    template_key = "PLA" if normalized == "PLA+" else normalized
    if template_key not in _MATERIAL_COPY:
        return ""
    heading = f"Dlaczego warto wybrać {template_key}?"
    intro, benefits_text = _MATERIAL_COPY[template_key].split(heading, 1)
    prefixes = _BENEFIT_PREFIXES[template_key]
    positions = [benefits_text.index(prefix) for prefix in prefixes]
    temperature = "60°C" if template_key == "PLA" else "85°C"
    benefits = []
    for index, start in enumerate(positions):
        stop = positions[index + 1] if index + 1 < len(positions) else len(benefits_text)
        benefit = benefits_text[start:stop].strip()
        prefix = prefixes[index]
        detail = benefit[len(prefix) :].strip()
        detail_html = html.escape(detail).replace(
            temperature, f"<b>{temperature}</b>"
        )
        benefits.append(f"<li><b>{html.escape(prefix)}</b> {detail_html}</li>")
    benefit_items = "".join(benefits)
    color_items = "".join(
        f"<li><b>{html.escape(color)}</b></li>"
        for color in _PALETTE_COLORS[template_key]
    )
    intro_html = html.escape(intro.strip())
    intro_html = intro_html.replace(
        f"materiału {template_key}",
        f"materiału <b>{template_key}</b>",
        1,
    )
    intro_html = intro_html.replace(temperature, f"<b>{temperature}</b>")
    notice_html = (
        "<b>Uwaga:</b> Numery przy kolorach odpowiadają naszej palecie barw. "
        "<b>Przy składaniu zamówienia prosimy o podanie numeru wybranego koloru</b>, "
        "co ułatwi i przyspieszy realizację."
    )
    return (
        f"<p>{intro_html}</p>"
        f"<p><b>{html.escape(heading)}</b></p>"
        f"<ul>{benefit_items}</ul>"
        f"<p><b>Nasza bogata gama kolorów {template_key}:</b></p>"
        f"<ol>{color_items}</ol>"
        f"<p>{notice_html}</p>"
    )


def normalize_material_palette_text(value):
    text = unicodedata.normalize("NFKC", str(value or "")).replace("\xa0", " ")
    text = re.sub(r"(^|\s)•\s*", r"\1", text)
    return re.sub(r"\s+", " ", text).strip()


def material_palette_digest(value):
    normalized = normalize_material_palette_text(value)
    if not normalized:
        return ""
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _extract_block_candidate(value):
    raw = str(value or "")
    start = _BLOCK_START_RE.search(raw)
    if not start:
        return ""
    end = _BLOCK_END_RE.search(raw, start.end())
    if end and end.end() - start.start() <= MAX_PALETTE_BLOCK_CHARS:
        return raw[start.start() : end.end()]
    company = _COMPANY_START_RE.search(raw, start.end())
    stop = company.start() if company else start.start() + MAX_PALETTE_BLOCK_CHARS
    return raw[start.start() : min(len(raw), stop)]


def analyze_material_palette_block(value, *, require_structure=False):
    candidate = _extract_block_candidate(value)
    if not candidate:
        return {
            "status": "absent",
            "material": "",
            "text": "",
            "block_hash": "",
            "expected_text": "",
        }
    text = description_preview(candidate)[:MAX_PALETTE_BLOCK_CHARS]
    material_match = _MATERIAL_RE.search(text)
    material = normalize_palette_material(
        material_match.group(1) if material_match else ""
    )
    expected_text = canonical_material_palette_text(material)
    structure_valid = True
    if require_structure:
        parser = _PaletteStructureParser()
        parser.feed(candidate)
        parser.close()
        template_key = "PLA" if material == "PLA+" else material
        structure_valid = parser.counts == {
            "p": 3,
            "ul": 1,
            "ol": 1,
            "ul_li": len(_BENEFIT_PREFIXES.get(template_key, ())),
            "ol_li": 20,
        }
    status = "mismatch"
    if (
        expected_text
        and structure_valid
        and normalize_material_palette_text(text)
        == normalize_material_palette_text(expected_text)
    ):
        status = "match"
    return {
        "status": status,
        "material": material,
        "text": text,
        "block_hash": material_palette_digest(text),
        "expected_text": expected_text,
    }
