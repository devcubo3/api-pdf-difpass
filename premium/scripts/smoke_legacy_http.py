"""Verify the legacy endpoint using synthetic data through HTTP only."""
import argparse
import io
import time

import httpx
import fitz as pymupdf
from PIL import Image
from reportlab.pdfgen import canvas


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', required=True)
    args = parser.parse_args()
    source = io.BytesIO()
    document = canvas.Canvas(source, pagesize=(595, 842), invariant=True)
    cases=[(595,842),(612,792),(842,595),(595,842)]
    for number,(width,height) in enumerate(cases,1):
        document.setPageSize((width,height))
        document.drawString(50,height-60,f'SYNTHETIC LEGACY CONTRACT PAGE {number}')
        document.setFont('Helvetica',5)
        document.drawString(12,12,f'SYNTHETIC FOOTER {number}')
        document.setFont('Helvetica',12)
        for x,y,color in [(2,2,(1,0,0)),(width-14,2,(0,1,0)),
                          (2,height-14,(0,0,1)),(width-14,height-14,(1,0,1))]:
            document.setFillColorRGB(*color)
            document.rect(x,y,12,12,fill=1,stroke=0)
        document.setFillColorRGB(0,0,0)
        document.showPage()
    # An empty source page must keep its position without breaking the request.
    document.showPage()
    document.save()
    picture = io.BytesIO()
    Image.new('RGB', (60, 60), '#073b55').save(picture, format='PNG')
    with httpx.Client(base_url=args.base_url, timeout=60, trust_env=False) as client:
        for attempt in range(30):
            try:
                response = client.get('/openapi.json')
                if response.status_code == 200:
                    break
            except httpx.TransportError:
                pass
            time.sleep(1)
        else:
            raise RuntimeError('Legacy API did not become available')
        assert response.json()['info']['title'] == 'PDF Shell Generator API'
        assert '/generate-pdf/' in response.json()['paths']
        response = client.post('/generate-pdf/', data={'title': 'SYNTHETIC TEST'}, files={
            'icon_image': ('icon.png', picture.getvalue(), 'image/png'),
            'dif_image': ('logo.png', picture.getvalue(), 'image/png'),
            'input_pdf': ('synthetic.pdf', source.getvalue(), 'application/pdf'),
        })
        assert response.status_code == 200, f'Legacy request failed: HTTP {response.status_code}'
        assert response.headers['content-type'].startswith('application/pdf')
        with pymupdf.open(stream=response.content, filetype='pdf') as result:
            assert len(result) == 5
            assert result.embfile_count() == 0
            for number, page in enumerate(result, 1):
                if number<=len(cases):
                    assert f'SYNTHETIC LEGACY CONTRACT PAGE {number}' in page.get_text()
                    assert f'SYNTHETIC FOOTER {number}' in page.get_text()
                    assert tuple(page.rect)[2:]==cases[number-1]
                    raster=page.get_pixmap()
                    # All four edge markers must survive in the rendered PDF,
                    # not merely remain in an invisible/clipped text object.
                    for color in (b'\xff\x00\x00',b'\x00\xff\x00',b'\x00\x00\xff',b'\xff\x00\xff'):
                        assert raster.samples.count(color)>10
    print('Legacy contract passed: five pages, portrait/landscape, four edge markers, fine print and blank page.')


if __name__ == '__main__':
    main()
