"""Evidence-first local extraction. Supplier PDF pages never reach the renderer."""
from __future__ import annotations

import hashlib
import io
import os
import re
from pathlib import Path

import pymupdf as fitz
import pytesseract
import zxingcpp
from PIL import Image


class InputError(ValueError):
    pass


def pixel_hash(image: Image.Image) -> str:
    rgb = image.convert('RGB')
    return hashlib.sha256(f'{rgb.width}x{rgb.height}:RGB:'.encode() + rgb.tobytes()).hexdigest()


def clean_text(text: str) -> str:
    # Supplier icon-font glyphs are decoration, not printable passenger information.
    return ''.join(c for c in text if (c in '\n\t' or ord(c) >= 32)
                   and not 0xE000 <= ord(c) <= 0xF8FF).strip()


def validate_pdf(path: Path, max_pages: int = 50, *, allow_blank_pages=False) -> int:
    try:
        # Invalid constructor inputs can leave a filename handle alive on Windows.
        # A bounded upload is already on disk, so validate bytes without that handle.
        with fitz.open(stream=path.read_bytes(), filetype='pdf') as doc:
            if not doc.is_pdf or doc.needs_pass:
                raise InputError('PDF inválido ou protegido por senha.')
            if not 1 <= len(doc) <= max_pages:
                raise InputError(f'O PDF deve ter entre 1 e {max_pages} páginas.')
            for page in doc:
                if not 72 <= page.rect.width <= 1440 or not 72 <= page.rect.height <= 1440:
                    raise InputError('Dimensões de página não suportadas.')
                if not allow_blank_pages and not page.get_contents() and not page.get_images():
                    raise InputError('O PDF contém uma página vazia.')
            if allow_blank_pages and not any(p.get_contents() or p.get_images() for p in doc):
                raise InputError('O PDF não contém conteúdo.')
            return len(doc)
    except InputError:
        raise
    except Exception as exc:
        raise InputError('Não foi possível abrir o PDF.') from exc


def _ocr(page) -> str:
    command = os.environ.get('TESSERACT_CMD')
    if command:
        pytesseract.pytesseract.tesseract_cmd = command
    image = Image.open(io.BytesIO(page.get_pixmap(dpi=180, alpha=False).tobytes('png')))
    # Tesseract inherits TESSDATA_PREFIX. Passing quoted --tessdata-dir through
    # pytesseract on Windows leaves literal quotes in the native argument.
    config = ''
    return clean_text(pytesseract.image_to_string(
        image, lang='por+eng', config=config, timeout=45))


def _read_codes(image: Image.Image):
    return list(zxingcpp.read_barcodes(image.convert('RGB')))


def _empty_frames(page) -> bool:
    """Recognize pale empty rectangular frames, recording an explicit review note."""
    if page.get_images() or page.get_links():
        return False
    drawings = page.get_drawings()
    return bool(drawings) and len(drawings) <= 20 and all(
        (drawing.get('color') or drawing.get('fill'))
        and min(drawing.get('color') or drawing['fill']) >= .85
        and (drawing.get('width') or 0) <= 1.5
        and all(item[0] in {'l', 're'} for item in drawing['items'])
        for drawing in drawings)


def _cropped_code(image: Image.Image, result) -> Image.Image:
    points = [result.position.top_left, result.position.top_right,
              result.position.bottom_left, result.position.bottom_right]
    # Keep the quiet zone and never interpolate the source pixels.
    extent = max(max(p.x for p in points)-min(p.x for p in points),
                 max(p.y for p in points)-min(p.y for p in points))
    margin = max(24, int(extent * .15), int(min(image.size) * .015))
    box = (max(0, min(p.x for p in points)-margin),
           max(0, min(p.y for p in points)-margin),
           min(image.width, max(p.x for p in points)+margin+1),
           min(image.height, max(p.y for p in points)+margin+1))
    return image.crop(box)


def extract_document(source: Path, workdir: Path, requested_type: str = 'auto', *, generic=False) -> dict:
    workdir.mkdir(parents=True, exist_ok=True)
    pages, codes, issues = [], [], []
    with fitz.open(source) as doc:
        for page_index, page in enumerate(doc):
            number = page_index + 1
            blocks = [{'text': clean_text(b[4]), 'raw_text':b[4], 'bbox': list(b[:4])}
                      for b in page.get_text('blocks', sort=True) if b[6] == 0 and clean_text(b[4])]
            # Some supplier fonts cause MuPDF to infer a space after every letter.
            # Re-read only affected blocks with inferred spaces disabled, leaving
            # real space characters and all other blocks untouched.
            spaced_letters = r'(?:[A-Za-z] {1,2}){8,}'
            if any(re.search(spaced_letters, b['text']) for b in blocks):
                alternatives = {tuple(round(v, 2) for v in b[:4]): clean_text(b[4]) for b in
                    page.get_text('blocks', sort=True,
                                  flags=fitz.TEXTFLAGS_BLOCKS | fitz.TEXT_INHIBIT_SPACES) if b[6] == 0}
                for block in blocks:
                    if re.search(spaced_letters, block['text']):
                        key = tuple(round(v, 2) for v in block['bbox'])
                        block['text'] = alternatives.get(key, block['text'])
            method = 'native'
            native_blocks=[dict(b) for b in blocks]
            if generic and not blocks and not page.get_images() and (not page.get_drawings() or _empty_frames(page)):
                pages.append({'page':number,'method':'empty','blocks':[],'text':'',
                              'quality':{'useful_characters':0,'invalid_ratio':0}})
                issues.append({'code':'EMPTY_PAGE','page':number,'blocking':False,
                               'message':'Página sem conteúdo útil preservada no inventário.'})
                continue
            from .reading_quality import needs_ocr,read_spatial_ocr,merge_readings,quality
            if generic and needs_ocr(page,blocks):
                try:
                    recognized=read_spatial_ocr(page)
                    if recognized:
                        blocks,method=merge_readings(blocks,recognized)
                    issues.append({'code':'OCR_REVIEW','page':number,'blocking':False,
                                   'message':'Leitura OCR/mista; conferir a fonte.'})
                except (pytesseract.TesseractNotFoundError,pytesseract.TesseractError,RuntimeError):
                    issues.append({'code':'OCR_UNAVAILABLE','page':number,'blocking':True,
                                   'message':'A leitura visual necessária não foi concluída.'})
            elif not generic and sum(len(b['text']) for b in blocks) < 25:
                if not blocks and _empty_frames(page):
                    pages.append({'page': number, 'method': 'empty_frames', 'blocks': [], 'text': ''})
                    issues.append({'code':'DECORATIVE_PAGE_OMITTED','page':number,'blocking':False,
                                   'message':'Página com molduras claras sem texto/imagens omitida; confirmar na revisão.'})
                    continue
                method = 'ocr'
                try:
                    content = _ocr(page)
                    blocks = [{'text': part.strip(), 'bbox': list(page.rect)}
                              for part in re.split(r'\n\s*\n', content) if part.strip()]
                    issues.append({'code': 'OCR_REVIEW', 'page': number, 'blocking': False,
                                   'message': 'Texto lido por OCR; conferir com a fonte.'})
                except (pytesseract.TesseractNotFoundError, pytesseract.TesseractError, RuntimeError):
                    issues.append({'code': 'OCR_UNAVAILABLE', 'page': number, 'blocking': True,
                                   'message': 'OCR por/eng indisponível ou excedeu o tempo.'})
            if not blocks:
                issues.append({'code': 'NO_TEXT', 'page': number, 'blocking': True,
                               'message': 'Página sem texto extraído; conteúdo não pode ser omitido.'})
            page_text = '\n'.join(b['text'] for b in blocks)
            pages.append({'page': number, 'method': method, 'blocks': blocks, 'text': page_text,
                          'native_blocks':native_blocks,'quality':quality(page_text)})
            seen_payloads = set()

            def add_code(image, result, mode, xref=None, source_bbox=None):
                key = result.text
                if key in seen_payloads:
                    return
                seen_payloads.add(key)
                code_id = f'p{number}-code{len(seen_payloads)}'
                dest = workdir / f'{code_id}.png'
                image.save(dest, format='PNG')
                codes.append({'id': code_id, 'path': str(dest), 'source_page': number,
                              'label': f'Código da página {number}', 'sha256': hashlib.sha256(dest.read_bytes()).hexdigest(),
                              'pixel_sha256': pixel_hash(image), 'payloads': [key], 'format': str(result.format),
                              'preservation_mode': mode, 'xref': xref,'source_bbox':source_bbox})

            for info in page.get_images(full=True):
                xref = info[0]
                width, height = info[2:4]
                if width * height > 40_000_000:
                    issues.append({'code': 'IMAGE_LIMIT', 'page': number, 'blocking': True,
                                   'message': 'Imagem excede o limite seguro de processamento.'})
                    continue
                try:
                    image = Image.open(io.BytesIO(doc.extract_image(xref)['image']))
                    image.load()
                    found = _read_codes(image)
                    covers_page = any((area & page.rect).get_area() >= page.rect.get_area() * .8
                                      for area in page.get_image_rects(xref))
                    for result in found:
                        # Full-page scans are never embedded as an alleged code image.
                        corners = [result.position.top_left, result.position.top_right,
                                   result.position.bottom_left, result.position.bottom_right]
                        code_area = ((max(p.x for p in corners)-min(p.x for p in corners)) *
                                     (max(p.y for p in corners)-min(p.y for p in corners)))
                        full_image = (not covers_page and len(found) == 1 and max(image.size) <= 1000
                                      and code_area >= image.width * image.height * .5)
                        extracted = image if full_image else _cropped_code(image, result)
                        rects=page.get_image_rects(xref)
                        source_bbox=None
                        if len(rects)==1:
                            r=rects[0]
                            source_bbox=[r.x0+min(p.x for p in corners)/image.width*r.width,
                                r.y0+min(p.y for p in corners)/image.height*r.height,
                                r.x0+max(p.x for p in corners)/image.width*r.width,
                                r.y0+max(p.y for p in corners)/image.height*r.height]
                        add_code(extracted, result, 'original_image' if full_image else 'original_pixel_region', xref,source_bbox)
                except (OSError, ValueError, RuntimeError):
                    continue
            # Also detect vector codes, scans, masks, or codes absent from the image list.
            raster = Image.open(io.BytesIO(page.get_pixmap(dpi=180, alpha=False).tobytes('png')))
            for result in _read_codes(raster):
                if result.text not in seen_payloads:
                    corners=[result.position.top_left,result.position.top_right,result.position.bottom_left,result.position.bottom_right]
                    box=[min(p.x for p in corners)/raster.width*page.rect.width,
                         min(p.y for p in corners)/raster.height*page.rect.height,
                         max(p.x for p in corners)/raster.width*page.rect.width,
                         max(p.y for p in corners)/raster.height*page.rect.height]
                    add_code(_cropped_code(raster, result), result, 'rendered_region',source_bbox=box)

    all_text = '\n'.join(p['text'] for p in pages)
    upper = all_text.upper()
    if generic:
        doc_type,title,hero='unknown','Documentos da sua viagem',None
    elif 'UNIVERSAL' in upper and re.search(r'Adult Admission|Child Admission|Ticket number|Booking Holder',all_text,re.I):
        doc_type, title, hero = 'ticket', 'Universal Orlando', 'premium-orlando.png'
    elif 'COMANDATUBA' in upper:
        doc_type, title, hero = 'travel_bundle', 'Comandatuba', 'premium-comandatuba.png'
    elif 'CAMPOS DO JORD' in upper:
        doc_type, title, hero = 'hotel', 'Campos do Jordão', 'premium-campos.png'
    elif 'DIF' in upper and ('PROPOSTA' in upper or 'PROJETO' in upper or 'INVESTIMENTO' in upper):
        doc_type, title, hero = 'proposal', 'Proposta DIF Pass', 'premium-dif-pass.png'
    else:
        from .media_context import classify_general
        doc_type,title=classify_general(all_text)
        hero=None
        if doc_type=='unknown':
            issues.append({'code': 'UNSUPPORTED_FORMAT', 'blocking': True,
                           'message': 'Formato ainda não homologado. Conferência técnica necessária.'})
    if not generic and requested_type != 'auto' and requested_type != doc_type:
        issues.append({'code': 'TYPE_MISMATCH', 'blocking': True,
                       'message': 'O tipo solicitado não coincide com o conteúdo identificado.'})

    summary, sections, tickets = [], [], []
    shared_blocks = {}
    for page in pages:
        text = page['text']
        number = page['page']
        page_codes = [c for c in codes if c['source_page'] == number]
        if doc_type == 'ticket':
            guest = re.search(r'(?:Adult Admission|Child Admission)\s*\n([^\n]+)', text, re.I)
            guest = guest or re.search(r'Guest\s*:\s*([^\n]+)', text, re.I)
            ticket_no = re.search(r'(?m)^\s*(\d{15,40})\s*$', text)
            ticket_no = ticket_no or re.search(r'Ticket number\s*:\s*(\w+)', text, re.I)
            if guest:
                name = guest.group(1).strip()
                if len(page_codes) != 1 or ticket_no is None:
                    issues.append({'code': 'TICKET_ASSOCIATION', 'page': number, 'blocking': True,
                                   'message': 'Ingresso exige pessoa, número e um código na mesma página.'})
                else:
                    page_codes[0]['label'] = name
                    page_codes[0]['ticket_number'] = ticket_no.group(1)
                    page_codes[0]['purpose'] = 'ticket'
                    if not any(ticket_no.group(1) in payload for payload in page_codes[0]['payloads']):
                        issues.append({'code':'CODE_TICKET_MISMATCH','page':number,'blocking':True,
                                       'message':'Número impresso do ingresso não confere com o código.'})
                    validity = re.search(r'Valid Only from\s*([^\n]+)', text, re.I)
                    if validity:
                        page_codes[0]['validity'] = validity.group(1).strip()
                    tickets.append({'guest': name, 'number': ticket_no.group(1), 'source_page': number,
                                    'code_id': page_codes[0]['id']})
                    summary.append({'label': 'Ingresso', 'value': name, 'source_page': number})
            elif page_codes:
                if re.search(r'Booking\s+\w+', text) and 'Booking Holder' in text:
                    for code in page_codes:
                        code['purpose'] = 'booking'
                        code['label'] = 'Referência da reserva'
                else:
                    issues.append({'code': 'UNASSOCIATED_CODE', 'page': number, 'blocking': True,
                                   'message': 'Código sem titular identificado na página.'})
        reference = re.search(r'(?:Booking|Voucher:|Localizador:)\s*([^\n]+)', text)
        if reference and len(summary) < 20:
            value = reference.group(1).strip()
            if not any(s['value'] == value for s in summary):
                summary.append({'label': 'Referência', 'value': value, 'source_page': number})
        for i, block in enumerate(page['blocks']):
            block_id = f'p{number}-b{i+1}'
            evidence = {'source_page': number, 'bbox': block['bbox'], 'block_id': block_id}
            normalized = re.sub(r'\s+', ' ', block['text']).strip()
            if len(normalized) > 200 and normalized in shared_blocks:
                sections[shared_blocks[normalized]]['sources'].append(evidence)
                continue
            if len(normalized) > 200:
                shared_blocks[normalized] = len(sections)
            section_title = 'Informações da sua viagem' if number == 1 else 'Detalhes e condições'
            sections.append({'title': section_title if i == 0 else '',
                             'body': block['text'], 'source_page': number, 'bbox': block['bbox'],
                             'source_method': page['method'], 'block_id': block_id, 'sources': [evidence]})
    if doc_type == 'ticket' and (not tickets or len(tickets) != sum(c.get('purpose') == 'ticket' for c in codes)):
        issues.append({'code': 'TICKETS_INCOMPLETE', 'blocking': True,
                       'message': 'Quantidade de titulares/ingressos/códigos não corresponde.'})
    ticket_codes = [c for c in codes if c.get('purpose') == 'ticket']
    if (len({c['ticket_number'] for c in ticket_codes}) != len(ticket_codes)
            or len({tuple(c['payloads']) for c in ticket_codes}) != len(ticket_codes)):
        issues.append({'code':'DUPLICATE_TICKET_CODE','blocking':True,
                       'message':'O mesmo ingresso/código aparece em mais de uma página de titular.'})
    if doc_type == 'ticket':
        # Supplier text is frequently fragmented into one PDF block per line.
        # Group ticket details with their code, and render identical common terms
        # once with provenance to every source block they cover.
        sections, common = [], {}
        for page in pages:
            blocks = page['blocks']
            matching = next((c for c in codes if c['source_page'] == page['page']
                             and c.get('purpose') == 'ticket'), None)
            split_at = 0
            if matching:
                split_at = next((i for i,b in enumerate(blocks) if
                    re.search(r'NONTRANSFERABLE|^Conditions:', b['text'], re.I)), len(blocks))
                matching['details'] = '\n'.join(b['text'] for b in blocks[:split_at])
                matching['detail_sources'] = [f'p{page["page"]}-b{i+1}' for i in range(split_at)]
            remaining = blocks[split_at:]
            if not remaining:
                continue
            body = '\n'.join(b['text'] for b in remaining)
            key = re.sub(r'\s+', ' ', body).strip()
            sources = [{'source_page':page['page'], 'bbox':b['bbox'],
                        'block_id':f'p{page["page"]}-b{i+split_at+1}'} for i,b in enumerate(remaining)]
            if key in common:
                sections[common[key]]['sources'].extend(sources)
            else:
                common[key] = len(sections)
                sections.append({'title':'Condições dos ingressos' if matching else 'Informações da reserva',
                                 'body':body, 'source_page':page['page'], 'sources':sources})
    return {'data': {'title': title, 'subtitle': 'Sua próxima experiência começa aqui.',
                     'document_type': doc_type, 'hero_asset': hero, 'summary': summary,
                     'sections': sections, 'tickets': tickets, 'source_page_count': len(pages),
                     'extraction_version': 'physical-evidence-v2' if generic else 'local-evidence-v1',
                     **({'pipeline_version':'canonical_v1'} if generic else {})},
            'codes': codes, 'issues': issues, 'pages': pages}
