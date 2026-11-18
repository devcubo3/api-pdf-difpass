"""Bounded OCR with spatial evidence for native, scanned and mixed PDF pages."""
import io
import os
import re
from collections import OrderedDict

from PIL import Image
import pymupdf as fitz
import pytesseract


def quality(text):
    useful=sum(c.isalnum() for c in text)
    bad=sum(c=='\ufffd' or 0xE000<=ord(c)<=0xF8FF for c in text)
    return {'useful_characters':useful,'invalid_ratio':round(bad/max(1,len(text)),4)}


def needs_ocr(page,blocks):
    text='\n'.join(b.get('raw_text',b['text']) for b in blocks)
    q=quality(text)
    coverage=0
    for info in page.get_images():
        for rect in page.get_image_rects(info[0]):
            coverage=max(coverage,(rect & page.rect).get_area()/max(1,page.rect.get_area()))
    return (q['useful_characters']<25 or q['invalid_ratio']>.02
            or coverage>.65 and q['useful_characters']<300)


def read_spatial_ocr(page):
    if os.getenv('TESSERACT_CMD'):
        pytesseract.pytesseract.tesseract_cmd=os.environ['TESSERACT_CMD']
    pix=page.get_pixmap(dpi=180,alpha=False)
    image=Image.open(io.BytesIO(pix.tobytes('png')))
    data=pytesseract.image_to_data(image,lang='por+eng',timeout=45,output_type=pytesseract.Output.DICT)
    lines=OrderedDict()
    for i,word in enumerate(data['text']):
        if not word.strip(): continue
        key=(data['block_num'][i],data['par_num'][i],data['line_num'][i])
        line=lines.setdefault(key,{'words':[],'bbox':None})
        line['words'].append(word)
        rect=fitz.Rect(data['left'][i]*page.rect.width/pix.width,
                       data['top'][i]*page.rect.height/pix.height,
                       (data['left'][i]+data['width'][i])*page.rect.width/pix.width,
                       (data['top'][i]+data['height'][i])*page.rect.height/pix.height)
        line['bbox']=rect if line['bbox'] is None else line['bbox']|rect
    return [{'text':' '.join(x['words']),'raw_text':' '.join(x['words']),'bbox':list(x['bbox'])}
            for x in lines.values()]


def merge_readings(native,ocr):
    """Keep native facts; add OCR regions outside their text boxes.

    A broken native layer is retained separately by the caller for diagnosis.
    """
    if quality('\n'.join(b.get('raw_text',b['text']) for b in native))['invalid_ratio']>.02:
        return ocr,'ocr'
    merged=list(native)
    for block in ocr:
        rect=fitz.Rect(block['bbox'])
        if any((rect & fitz.Rect(other['bbox'])).get_area()/max(1,rect.get_area())>.5 for other in native):
            continue
        merged.append(block)
    return sorted(merged,key=lambda b:(round(b['bbox'][1]/4),b['bbox'][0])),'mixed' if native else 'ocr'
