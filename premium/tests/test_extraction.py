"""Fidelity and supplier edge cases, with synthetic source PDFs."""
import io
from pathlib import Path
from PIL import Image
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas
from app.extraction import extract_document
from test_api import qr_image


def make_ticket_pdf(*, number='000123456', duplicate=False, scan=False, tracked=False):
    target = io.BytesIO()
    page = canvas.Canvas(target)
    for index in range(2 if duplicate else 1):
        page.drawString(40, 790, 'Universal Orlando')
        page.drawString(40, 760, f'Guest: TESTE VIAJANTE {index+1}')
        page.drawString(40, 730, f'Ticket number: {number}')
        if tracked:
            text = page.beginText(40, 700)
            text.setCharSpace(2)
            text.textOut('to the terms and conditions associated with this ticket')
            page.drawText(text)
        image = qr_image()
        if scan:
            scanned = Image.new('RGB', (500, 700), 'white')
            scanned.paste(image, (100, 280))
            image = scanned
        page.drawImage(ImageReader(image), 40, 40, width=300, height=420 if scan else 300)
        page.showPage()
    page.save()
    return target.getvalue()


def extract(tmp_path: Path, **options):
    source = tmp_path/'source.pdf'
    source.write_bytes(make_ticket_pdf(**options))
    return extract_document(source, tmp_path/'codes')


def test_number_in_printed_ticket_must_match_code_payload(tmp_path):
    result = extract(tmp_path, number='999999999')
    assert any(i['code'] == 'CODE_TICKET_MISMATCH' and i['blocking'] for i in result['issues'])


def test_one_ticket_cannot_be_reused_for_two_passenger_pages(tmp_path):
    result = extract(tmp_path, duplicate=True)
    assert any(i['code'] == 'DUPLICATE_TICKET_CODE' and i['blocking'] for i in result['issues'])


def test_full_scan_is_not_embedded_as_a_code_asset(tmp_path):
    result = extract(tmp_path, scan=True)
    assert len(result['codes']) == 1
    code = result['codes'][0]
    assert code['preservation_mode'] == 'original_pixel_region'
    with Image.open(code['path']) as image:
        assert image.width < 500 and image.height < 700


def test_tracking_spaces_do_not_change_real_text(tmp_path):
    result = extract(tmp_path, tracked=True)
    all_text = '\n'.join(p['text'] for p in result['pages'])
    assert 'to the terms and conditions associated with this ticket' in all_text
