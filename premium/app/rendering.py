"""Deterministic Diff Travel document composition.

Only extracted text and original code images enter the layout. Source PDF pages
are deliberately outside this interface. PyMuPDF calls must run in a worker
process, not concurrent threads. The caller owns input validation, permissions,
document semantics and the final comparison of decoded codes.
"""

from __future__ import annotations

import hashlib
import html
import re
from pathlib import Path
from typing import Any

import pymupdf as fitz
from PIL import Image as PILImage
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    BaseDocTemplate, Flowable, Frame, Image, PageBreak,
    PageTemplate, Paragraph, Spacer, Table, TableStyle,
)


NAVY = colors.HexColor("#073B55")
COPPER = colors.HexColor("#B94F18")
IVORY = colors.HexColor("#FCFAF6")
PEACH = colors.HexColor("#F7EEE4")
INK = colors.HexColor("#233E4A")
MUTED = colors.HexColor("#637580")
RULE = colors.HexColor("#DBE1E1")
PAGE_WIDTH, PAGE_HEIGHT = A4
MARGIN = 45.0
CONTENT_WIDTH = PAGE_WIDTH - 2 * MARGIN
MAX_RENDERED_PAGES = 100


class RenderingError(ValueError):
    """The supplied data or brand assets cannot produce a faithful document."""


def _text(value: Any) -> str:
    """Escape untrusted source text, preserving line breaks without HTML markup."""
    if value is None:
        return ""
    printable = str(value).translate(str.maketrans({
        '\u2010':'-', '\u2011':'-', '\u2012':'-', '\u2013':'-', '\u2014':'-', '\u2015':'-',
        '\u2212':'-', '\u25a1':'', '\u25fb':'', '\u2610':'[ ]', '\u2611':'[x]',
        '\u2708':' ', '\ufe0f':'',
    }))
    return html.escape(printable, quote=False).replace("\n", "<br/>")


def _font_path(brand_dir: Path, filename: str) -> Path:
    for candidate in (brand_dir / "fonts" / filename, brand_dir / filename):
        if candidate.is_file():
            return candidate
    raise RenderingError(f"Brand font is missing: {filename}")


def _fonts(brand_dir: Path) -> dict[str, str]:
    paths = {
        "regular": _font_path(brand_dir, "Vera.ttf"),
        "bold": _font_path(brand_dir, "VeraBd.ttf"),
        "display": _font_path(brand_dir, "VeraBd.ttf"),
    }
    # File digest avoids accidental reuse when two brand versions use one worker.
    names = {}
    for role, path in paths.items():
        digest = hashlib.sha256(path.read_bytes()).hexdigest()[:12]
        name = f"DiffTravel-{role}-{digest}"
        if name not in pdfmetrics.getRegisteredFontNames():
            pdfmetrics.registerFont(TTFont(name, str(path)))
        names[role] = name
    pdfmetrics.registerFontFamily(
        names["regular"], normal=names["regular"], bold=names["bold"],
        italic=names["regular"], boldItalic=names["bold"],
    )
    return names


def _styles(fonts):
    body = ParagraphStyle(
        "Body", fontName=fonts["regular"], fontSize=10.1, leading=14.4,
        textColor=INK, spaceAfter=9, allowWidows=0, allowOrphans=0,
        splitLongWords=1,
    )
    return {
        "body": body,
        "title": ParagraphStyle(
            "Title", parent=body, fontName=fonts["display"], fontSize=29,
            leading=34, textColor=NAVY, spaceAfter=12,
        ),
        "subtitle": ParagraphStyle(
            "Subtitle", parent=body, fontSize=12, leading=17,
            textColor=COPPER, spaceAfter=18,
        ),
        "section": ParagraphStyle(
            "Section", parent=body, fontName=fonts["bold"], fontSize=15,
            leading=20, textColor=NAVY, spaceBefore=14, spaceAfter=10,
            keepWithNext=1,
        ),
        "small": ParagraphStyle(
            "Small", parent=body, fontSize=8, leading=11, textColor=MUTED,
        ),
        "label": ParagraphStyle(
            "Label", parent=body, fontName=fonts["bold"], fontSize=8.7,
            leading=12, textColor=COPPER, spaceAfter=3,
        ),
    }


def _image_size(path: Path):
    try:
        with PILImage.open(path) as img:
            img.load()
            width, height = img.size
            if width < 1 or height < 1:
                raise RenderingError("An image has empty dimensions")
            return width, height
    except (OSError, ValueError) as exc:
        raise RenderingError("A supplied image cannot be read") from exc


def _brand_image(brand_dir: Path, filename: str, required=False):
    # An explicit asset can select a file in the approved pack, not arbitrary paths.
    if Path(filename).name != filename or "/" in filename or "\\" in filename:
        raise RenderingError("Brand images must be basenames in the brand pack")
    path = brand_dir / filename
    if not path.is_file():
        if required:
            raise RenderingError(f"Brand image is missing: {filename}")
        return None
    _image_size(path)
    return path


def _hero(data, brand_dir):
    explicit = data.get("hero_asset")
    if explicit:
        return _brand_image(brand_dir, str(explicit), required=True)
    haystack = f"{data.get('document_type', '')} {data.get('title', '')}".casefold()
    candidates = (
        (("universal", "orlando", "ticket"), "premium-orlando.png"),
        (("comandatuba",), "premium-comandatuba.png"),
        (("campos",), "premium-campos.png"),
        (("proposal", "proposta", "dif pass", "dif-pass"), "premium-dif-pass.png"),
    )
    for keys, filename in candidates:
        if any(key in haystack for key in keys):
            return _brand_image(brand_dir, filename)
    return None


class _BrandDocument(BaseDocTemplate):
    def __init__(self, path, logo, mascot, fonts, title):
        super().__init__(
            str(path), pagesize=A4, leftMargin=MARGIN, rightMargin=MARGIN,
            topMargin=96, bottomMargin=50, title=title,
            author="Diff Travel", subject="Documento de viagem",
            pageCompression=1,
        )
        self.logo, self.mascot, self.fonts = logo, mascot, fonts
        self.addPageTemplates([
            PageTemplate(
                id="premium",
                frames=Frame(
                    MARGIN, 50, CONTENT_WIDTH, PAGE_HEIGHT - 146,
                    leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0,
                ),
                onPage=self._page,
            )
        ])

    def _page(self, canvas, doc):
        canvas.saveState()
        canvas.setFillColor(IVORY)
        canvas.rect(0, 0, PAGE_WIDTH, PAGE_HEIGHT, fill=1, stroke=0)
        canvas.setFillColor(NAVY)
        canvas.rect(0, PAGE_HEIGHT - 6, PAGE_WIDTH, 6, fill=1, stroke=0)
        logo_w, logo_h = _image_size(self.logo)
        width = 146
        height = min(52, width * logo_h / logo_w)
        canvas.drawImage(str(self.logo), MARGIN, PAGE_HEIGHT - 75,
                         width=width, height=height, mask="auto",
                         preserveAspectRatio=True, anchor="sw")
        if doc.page == 1 and self.mascot:
            canvas.drawImage(str(self.mascot), PAGE_WIDTH - MARGIN - 60,
                             PAGE_HEIGHT - 80, width=60, height=60, mask="auto",
                             preserveAspectRatio=True, anchor="c")
        canvas.setStrokeColor(RULE)
        canvas.setLineWidth(.6)
        canvas.line(MARGIN, PAGE_HEIGHT - 86, PAGE_WIDTH - MARGIN, PAGE_HEIGHT - 86)
        canvas.line(MARGIN, 34, PAGE_WIDTH - MARGIN, 34)
        canvas.setFillColor(MUTED)
        canvas.setFont(self.fonts["regular"], 8)
        canvas.drawString(MARGIN, 20, "DIFF TRAVEL")
        canvas.drawRightString(PAGE_WIDTH - MARGIN, 20, str(doc.page))
        canvas.restoreState()


class _OriginalCode(Flowable):
    """Place source image on white space and record exact PDF-space coordinates."""

    def __init__(self, code, placements):
        super().__init__()
        self.code = code
        self.path = Path(code["path"])
        self.placements = placements
        width, height = _image_size(self.path)
        factor = min(300.0 / width, 224.0 / height)
        self.image_width = width * factor
        self.image_height = height * factor
        self.width = CONTENT_WIDTH
        self.padding = 18.0
        self.height = self.image_height + self.padding * 2

    def draw(self):
        canvas = self.canv
        x = (self.width - self.image_width) / 2
        y = self.padding
        canvas.setFillColor(colors.white)
        canvas.roundRect(x - self.padding, 0,
                         self.image_width + 2 * self.padding, self.height,
                         radius=8, fill=1, stroke=0)
        canvas.drawImage(str(self.path), x, y, width=self.image_width,
                         height=self.image_height, mask="auto")
        absolute_x, absolute_y = canvas.absolutePosition(x, y)
        page_height = canvas._pagesize[1]
        self.placements.append({
            "id": str(self.code["id"]),
            "source_page": int(self.code.get("source_page", 1)),
            "page": canvas.getPageNumber(),
            # One-based page; bbox uses top-left origin in PDF points (not pixels).
            "bbox": [absolute_x, page_height - absolute_y - self.image_height,
                     absolute_x + self.image_width, page_height - absolute_y],
            "path": str(self.path.resolve()),
            "sha256": self.code.get("sha256"),
            "pixel_sha256": self.code.get("pixel_sha256"),
            "payloads": list(self.code.get("payloads", [])),
        })


def _validate_codes(codes):
    ids = set()
    for code in codes:
        if not isinstance(code, dict) or not code.get("id") or not code.get("path"):
            raise RenderingError("Each original code requires id and path")
        identifier = str(code["id"])
        if identifier in ids:
            raise RenderingError("Original code ids must be unique")
        ids.add(identifier)
        path = Path(code["path"])
        if not path.is_file():
            raise RenderingError("An original code image is missing")
        expected = code.get("sha256")
        if expected and hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise RenderingError("An original code image has changed since extraction")
        _image_size(path)


def _summary_flow(items, styles):
    if not items:
        return []
    rows = []
    for item in items:
        if not isinstance(item, dict):
            raise RenderingError("Summary entries must be objects")
        rows.append([
            Paragraph(_text(item.get("label", "")), styles["label"]),
            Paragraph(_text(item.get("value", "")), styles["body"]),
        ])
    table = Table(rows, colWidths=[CONTENT_WIDTH * .32, CONTENT_WIDTH * .68],
                  hAlign="LEFT", splitByRow=1)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), PEACH),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 12),
        ("RIGHTPADDING", (0, 0), (-1, -1), 12),
        ("TOPPADDING", (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
    ]))
    return [table, Spacer(1, 15)]


def render_document(
    data: dict, codes: list[dict], output_dir: Path, brand_dir: Path, dpi: int = 240,
) -> dict:
    """Produce branded page PNGs and an image-based PDF with intact code overlays.

    Returns filesystem paths owned by the caller, plus code placements. The final
    PDF does not contain any source-page object, attachment, or complete original
    page image. Callers must independently validate content and decoded codes
    before marking this artifact ready for delivery.
    """
    if not isinstance(data, dict):
        raise RenderingError("Document data must be an object")
    if not isinstance(codes, list):
        raise RenderingError("Codes must be a list")
    if not isinstance(dpi, int) or isinstance(dpi, bool) or not 120 <= dpi <= 300:
        raise RenderingError("Render DPI must be an integer between 120 and 300")
    title = str(data.get("title") or "Documento de viagem")
    sections = data.get("sections", [])
    summary = data.get("summary", [])
    if not isinstance(sections, list) or not isinstance(summary, list):
        raise RenderingError("Document sections and summary must be lists")
    if not sections and not summary and not codes:
        raise RenderingError("There is no extracted content to render")
    output_dir, brand_dir = Path(output_dir), Path(brand_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    logo = _brand_image(brand_dir, "wordmark-premium.png", required=True)
    mascot = _brand_image(brand_dir, "mascot-premium.png")
    fonts, placements = _fonts(brand_dir), []
    styles = _styles(fonts)
    _validate_codes(codes)
    flow = [Paragraph(_text(title), styles["title"])]
    if data.get("subtitle"):
        flow.append(Paragraph(_text(data["subtitle"]), styles["subtitle"]))
    hero = _hero(data, brand_dir)
    if hero:
        width, height = _image_size(hero)
        display_width = min(CONTENT_WIDTH, 240 * width / height)
        picture = Image(str(hero), width=display_width,
                        height=display_width * height / width)
        picture.hAlign = "CENTER"
        flow.extend([picture, Spacer(1, 19)])
    flow.extend(_summary_flow(summary, styles))
    for section in sections:
        if not isinstance(section, dict):
            raise RenderingError("Sections must be objects")
        if section.get("title"):
            flow.append(Paragraph(_text(section["title"]), styles["section"]))
        body = str(section.get("body") or "")
        # Separate paragraphs allow pagination of very long terms without clipping.
        for paragraph in re.split(r"\n\s*\n", body):
            if paragraph.strip():
                # Reflow soft-wrapped paragraphs without dropping their words.
                if len(paragraph) > 500:
                    paragraph = re.sub(r'(?<!\n)\n(?!\n)', ' ', paragraph)
                flow.append(Paragraph(_text(paragraph), styles["body"]))
    for code in codes:
        flow.append(PageBreak())
        heading = str(code.get("label") or "Código do documento")
        flow.append(Paragraph(_text(heading), styles["title"]))
        if code.get('ticket_number'):
            flow.append(Paragraph(_text(code['ticket_number']), styles['subtitle']))
        if code.get('validity'):
            flow.append(Paragraph('Validade informada: ' + _text(code['validity']), styles['body']))
        flow.append(Spacer(1, 25))
        flow.append(_OriginalCode(code, placements))
        flow.append(Spacer(1, 18))
        flow.append(Paragraph("Código original do fornecedor", styles["small"]))
        if code.get('details'):
            detail = re.sub(r'\s*\n\s*', ' · ', str(code['details']))
            flow.extend([Spacer(1, 12), Paragraph(_text(detail), styles['body'])])
    vector_path = output_dir / "document.vector.pdf"
    final_path = output_dir / "document.pdf"
    from reportlab.platypus.doctemplate import LayoutError
    try:
        _BrandDocument(vector_path, logo, mascot, fonts, title).build(flow)
    except LayoutError as exc:
        raise RenderingError("Extracted content does not fit the page layout") from exc
    page_paths = []
    with fitz.open(vector_path) as source, fitz.open() as final:
        if len(source) > MAX_RENDERED_PAGES:
            raise RenderingError("The composed document exceeds the render page limit")
        # Sequential rasterization bounds peak memory to roughly one rendered page.
        for index, source_page in enumerate(source):
            png_path = output_dir / f"page-{index + 1:03d}.png"
            source_page.get_pixmap(dpi=dpi, alpha=False).save(png_path)
            page_paths.append(str(png_path.resolve()))
            page = final.new_page(width=source_page.rect.width,
                                  height=source_page.rect.height)
            page.insert_image(page.rect, filename=str(png_path), keep_proportion=False)
            for placement in placements:
                if placement["page"] == index + 1:
                    page.insert_image(fitz.Rect(placement["bbox"]),
                                      filename=placement["path"],
                                      keep_proportion=False, overlay=True)
        final.set_metadata({"title": title, "author": "Diff Travel",
                            "subject": "Documento de viagem"})
        final.save(final_path, garbage=3, deflate=True)
    if len(placements) != len(codes):
        raise RenderingError("Not every original code was placed exactly once")
    return {
        "pdf_path": str(final_path.resolve()),
        "page_paths": page_paths,
        "code_placements": placements,
        "page_count": len(page_paths),
    }
