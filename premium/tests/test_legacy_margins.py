"""Legacy shell: safe whitespace fitting, edge content and original QR fidelity."""
import importlib.util
import io
from pathlib import Path
import sys
from types import SimpleNamespace

from PIL import Image
import fitz
import pytest
from reportlab.graphics import renderPDF
from reportlab.graphics.barcode.qr import QrCodeWidget
from reportlab.graphics.shapes import Drawing
import zxingcpp


root=Path(__file__).resolve().parents[2]
source=root/'api_pdf.py'
if not source.is_file():
    source=root/'api-pdf-difpass/api_pdf.py'
spec=importlib.util.spec_from_file_location('legacy_margin_subject',source)
legacy=importlib.util.module_from_spec(spec)
sys.modules[spec.name]=legacy
spec.loader.exec_module(legacy)


def test_sparse_page_only_loses_empty_margin_and_zoom_is_capped():
    with fitz.open() as doc:
        page=doc.new_page(width=595,height=842)
        page.insert_text((100,200),'SYNTHETIC TICKET')
        page.draw_rect(fitz.Rect(100,300,450,570),color=(0,0,0))
        page.insert_text((100,650),'Important small print',fontsize=5,color=(.9,.9,.9))
        clip=legacy.safe_content_bounds(page)
        assert clip!=page.rect and page.rect.contains(clip)
        assert clip.width>=page.rect.width/1.3-1e-6
        assert clip.height>=page.rect.height/1.3-1e-6
        for _,box,*rest in page.get_bboxlog():
            assert clip.contains(fitz.Rect(box)&page.rect)


@pytest.mark.parametrize('rotation',[90,180,270])
def test_rotated_pages_preserve_existing_full_page_placement(rotation):
    with fitz.open() as doc:
        page=doc.new_page();page.insert_text((100,200),'ROTATED');page.set_rotation(rotation)
        assert legacy.safe_content_bounds(page)==page.rect


def test_cropboxes_annotations_full_bleed_and_scan_do_not_get_trimmed():
    with fitz.open() as doc:
        cropped=doc.new_page();cropped.insert_text((100,200),'CROPBOX')
        cropped.set_cropbox(fitz.Rect(20,30,570,800))
        assert legacy.safe_content_bounds(cropped)==cropped.rect
        annotated=doc.new_page();annotated.insert_text((100,200),'ANNOTATED')
        annotated.add_text_annot((5,5),'Corner note')
        assert legacy.safe_content_bounds(annotated)==annotated.rect
        bleed=doc.new_page();bleed.draw_rect(bleed.rect,color=None,fill=(.98,.98,.98))
        assert legacy.safe_content_bounds(bleed)==bleed.rect
        scan=doc.new_page(width=600,height=840)
        image=io.BytesIO();Image.new('RGB',(600,840),'white').save(image,format='PNG')
        scan.insert_image(scan.rect,stream=image.getvalue())
        assert legacy.safe_content_bounds(scan)==scan.rect


def test_content_at_all_four_edges_is_preserved():
    with fitz.open() as doc:
        page=doc.new_page(width=595,height=842)
        for rect in (fitz.Rect(1,1,12,12),fitz.Rect(583,1,594,12),
                     fitz.Rect(1,830,12,841),fitz.Rect(583,830,594,841)):
            page.draw_rect(rect,fill=(0,0,0))
        assert legacy.safe_content_bounds(page)==page.rect


@pytest.mark.parametrize('bounds',[None,(0,0,float('nan'),20)])
def test_unreadable_or_invalid_drawing_bounds_keep_full_page(bounds):
    def bboxlog():
        if bounds is None: raise RuntimeError('Synthetic unreadable bounds')
        return [('fill-text',bounds)]
    page=SimpleNamespace(rect=fitz.Rect(0,0,595,842),cropbox=fitz.Rect(0,0,595,842),
        mediabox=fitz.Rect(0,0,595,842),rotation=0,first_annot=None,first_widget=None,get_bboxlog=bboxlog)
    assert legacy.safe_content_bounds(page)==page.rect


def test_shell_makes_original_content_larger_without_losing_qr_or_blank_pages(tmp_path):
    source=tmp_path/'synthetic.pdf';icon=tmp_path/'icon.png'
    Image.new('RGB',(60,60),'#073b55').save(icon)
    qr_value='SYNTHETIC:DIFFTRAVEL-LEGACY-QR-VALIDATION'
    widget=QrCodeWidget(qr_value);x0,y0,x1,y1=widget.getBounds()
    drawing=Drawing(120,120,transform=[120/(x1-x0),0,0,120/(y1-y0),0,0]);drawing.add(widget)
    with fitz.open() as doc,fitz.open(stream=renderPDF.drawToString(drawing),filetype='pdf') as qr:
        page=doc.new_page(width=595,height=842)
        page.insert_text((100,200),'SYNTHETIC TICKET',fontsize=12)
        page.insert_text((100,600),'SYNTHETIC IMPORTANT SMALL PRINT',fontsize=5)
        page.show_pdf_page(fitz.Rect(390,620,510,740),qr,0)
        doc.new_page(width=842,height=595)
        doc.save(source)
    result=legacy.create_pdf_shell_and_insert_content_core(str(source),str(icon),str(icon),'SYNTHETIC')
    with fitz.open(stream=result.getvalue(),filetype='pdf') as output:
        assert len(output)==2 and output[0].rect==fitz.Rect(0,0,595,842)
        assert 'SYNTHETIC IMPORTANT SMALL PRINT' in output[0].get_text()
        sizes=[span['size'] for block in output[0].get_text('dict')['blocks'] if 'lines' in block
            for line in block['lines'] for span in line['spans'] if span['text']=='SYNTHETIC TICKET']
        baseline_size=12*min(515/595,652/842)
        assert sizes and baseline_size*1.05<sizes[0]<=baseline_size*1.3+.02
        raster=output[0].get_pixmap(matrix=fitz.Matrix(3,3))
        image=Image.frombytes('RGB',(raster.width,raster.height),raster.samples)
        assert qr_value in {code.text for code in zxingcpp.read_barcodes(image)}
