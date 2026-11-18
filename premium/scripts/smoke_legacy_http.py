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
    for number in (1, 2):
        document.drawString(50, 500, f'SYNTHETIC LEGACY CONTRACT PAGE {number}')
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
            assert len(result) == 2
            assert result.embfile_count() == 0
            for number, page in enumerate(result, 1):
                assert f'SYNTHETIC LEGACY CONTRACT PAGE {number}' in page.get_text()
    print('Legacy contract passed: two source pages, PDF response and original endpoint.')


if __name__ == '__main__':
    main()
