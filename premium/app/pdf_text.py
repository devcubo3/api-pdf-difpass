"""Read printed glyphs while retaining real word gaps outside tracked labels."""
import pymupdf as fitz


def extract_printed_text(page, tracking_boxes=()):
    """Only remove inferred spaces inside measured, application-owned tracking.

    A global TEXT_INHIBIT_SPACES also joins adjacent source fields. Conversely,
    default extraction splits letter-spaced headings. Use the actual glyphs and
    measured template boxes to distinguish these cases, without fuzzy matching
    values or adding any text from the plan to the PDF's extracted text.
    """
    regions=[fitz.Rect(box) for box in tracking_boxes]
    lines=[]
    flags=fitz.TEXTFLAGS_RAWDICT & ~fitz.TEXT_PRESERVE_IMAGES
    for block in page.get_text('rawdict',flags=flags)['blocks']:
        for line in block.get('lines',[]):
            text=[]
            for span in line['spans']:
                for char in span['chars']:
                    rect=fitz.Rect(char['bbox'])
                    center=fitz.Point((rect.x0+rect.x1)/2,(rect.y0+rect.y1)/2)
                    tracked=any(center in region for region in regions)
                    if tracked and char.get('synthetic',False) and char['c'].isspace():
                        continue
                    text.append(char['c'])
            lines.append(''.join(text))
    return '\n'.join(lines)
