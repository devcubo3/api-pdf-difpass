"""Premium HTML/CSS -> Chromium PDF, run only in the separate API worker.

AI supplies structured data only. All visual assets and markup are controlled by
the application. The browser has no network, client credentials or source HTML.
"""
from __future__ import annotations

import base64
from collections import Counter
import hashlib
import os
import re
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape
import pymupdf as fitz
from PIL import Image
from playwright.sync_api import sync_playwright, Error as BrowserError

from .ai_images import AIError, Section, _normalized, page_text, validate_plan
from .rendering import _validate_codes
from .customer_presentation import prepare_customer_plan
from .typography import emphasis_segments,bullet_view
from .hotel_preservation import preserve_hotel_data
from .media_context import primary_place

TEMPLATE_VERSION = 'difftravel-html-v14-safety-download'
TEMPLATE_DIR = Path(__file__).parent / 'html_templates'
ICONS = {
    'neutral':'<svg viewBox="0 0 24 24"><rect x="4" y="4" width="16" height="16" rx="3"/><path d="M8 9h8M8 13h8M8 17h5"/></svg>',
    'included':'<svg viewBox="0 0 24 24"><path d="M12 3l8 3v6c0 5-8 9-8 9s-8-4-8-9V6z"/><path d="M8 12l3 3 5-6"/></svg>',
    'attention':'<svg viewBox="0 0 24 24"><path d="M12 3L2 21h20zM12 9v5M12 17h.01"/></svg>',
    'flight':'<svg viewBox="0 0 24 24"><path d="m3 11 7 1 7-8c2-2 4 0 2 2l-5 8 1 7-3-3-2-3-5-1z"/></svg>',
    'stay':'<svg viewBox="0 0 24 24"><path d="M3 18V8m18 10V9M3 14h18M5 10h5v4H5zM10 11h9v3M3 18h18"/></svg>',
    'people':'<svg viewBox="0 0 24 24"><circle cx="9" cy="8" r="3"/><path d="M3 20v-3c0-5 12-5 12 0v3M16 5c4 0 4 6 0 6m2 3c3 0 4 2 4 5"/></svg>',
    'contact':'<svg viewBox="0 0 24 24"><path d="M6 3 3 6c0 7 8 15 15 15l3-3-5-4-2 2c-3-1-5-3-6-6l2-2z"/></svg>',
    'location':'<svg viewBox="0 0 24 24"><path d="M19 10c0 5-7 11-7 11S5 15 5 10a7 7 0 1 1 14 0Z"/><circle cx="12" cy="10" r="2"/></svg>',
}


def card_icon(section,flight):
    if flight:
        return 'flight'
    if section.tone!='neutral':
        return section.tone
    title=_normalized(section.title)
    if any(word in title for word in ('hospede','passageiro')):
        return 'people'
    if any(word in title for word in ('contato','assistencia','fornecedor')):
        return 'contact'
    if any(word in title for word in ('localizacao','endereco')):
        return 'location'
    if any(word in title for word in ('hospedagem','estadia','acomodacao')):
        return 'stay'
    return 'neutral'


def data_uri(path: Path, mime: str) -> str:
    if not path.is_file():
        raise AIError('HTML_BRAND_MISSING', 'O pacote visual do template está incompleto.')
    return f'data:{mime};base64,' + base64.b64encode(path.read_bytes()).decode('ascii')


def build_html(plan, codes, data, brand_dir: Path, scope='full',media_asset=None,flight_fields=None,original_download=None, *, measurement=False) -> str:
    code_map = {c['id']: c for c in codes}
    selected = plan.pages[:1] if scope == 'preview' else plan.pages
    pages = []
    for index,page in enumerate(selected):
        code = code_map.get(page.code_id, {})
        wide_code=False
        if code.get('path'):
            with Image.open(code['path']) as original:
                # Linear and other landscape codes need their own wide area.
                # A square QR slot can make narrow source bars unscannable.
                wide_code=original.width >= 2.5 * original.height
        cards=[]
        for j,section in enumerate(page.sections):
            flight=(flight_fields or {}).get((index,j))
            if not flight and data.get('pipeline_version')=='canonical_v1':
                from .flight_cards import section_key
                flight=data.get('structured_flights',{}).get(section_key(section))
            cards.append({'section':section,'flight':flight,'icon':card_icon(section,flight),
                'bullets':[bullet_view(text,page.critical_values) for text in section.bullets],
                'extra':[bullet_view(text,page.critical_values) for text in flight['extra']] if flight else []})
        paired=(not page.code_id and len(cards)>=2 and all(not item['flight'] and
                item['section'].tone=='neutral' for item in cards[:2]) and
                sum(len(' '.join(item['section'].bullets)) for item in cards[:2])<440)
        pages.append({'page':page, 'code_label':('Ingresso individual' if code.get('purpose') == 'ticket'
                                                else ('Código do documento' if data.get('pipeline_version')=='canonical_v1'
                                                      and code.get('purpose')!='booking' else 'Referência da reserva')),
                      'cards':cards,'paired':paired,'wide_code':wide_code,
                      'introduction':emphasis_segments(page.introduction,page.critical_values)})
    # Never load a path or URL supplied in document text or a model response.
    hero_name = data.get('hero_asset')
    allowed = {'premium-orlando.png', 'premium-campos.png', 'premium-comandatuba.png', 'premium-dif-pass.png','premium-institutional.svg'}
    hero = data_uri(brand_dir/hero_name, 'image/svg+xml' if hero_name.endswith('.svg') else 'image/png') if hero_name in allowed else None
    if media_asset:
        path=Path(media_asset['path'])
        if hashlib.sha256(path.read_bytes()).hexdigest()!=media_asset['sha256']:
            raise AIError('MEDIA_INTEGRITY','A fotografia selecionada mudou desde a conferência.')
        hero=data_uri(path,'image/jpeg')
    environment = Environment(loader=FileSystemLoader(TEMPLATE_DIR),
        autoescape=select_autoescape(['html']), undefined=StrictUndefined)
    icons={name:svg.replace('<svg ', '<svg fill="none" stroke="'+(
        '#16734a' if name=='included' else '#a3442c' if name=='attention' else '#073b55')+
        '" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" ')
        for name,svg in ICONS.items()}
    return environment.get_template('premium.html').render(title=plan.title, pages=pages,
        total_pages=len(plan.pages), icons=icons, hero=hero, photographic_hero=bool(media_asset),
        measurement=measurement,
        complete=scope=='full',original_download=original_download,
        photo_credit=((('Imagem de apoio: ' if (media_asset.get('authority')=='destination_stock'
                        or (primary_place(data) or {}).get('kind')=='destination') else 'Foto: ')
                        +media_asset['credit']) if media_asset else None),
        photo_source=media_asset['source_page_url'] if media_asset else None,
        wordmark=data_uri(brand_dir/'wordmark-premium.png','image/png'),
        mascot=data_uri(brand_dir/'mascot-premium.png','image/png'),
        font_regular=data_uri(brand_dir/'fonts/Vera.ttf','font/ttf'),
        font_bold=data_uri(brand_dir/'fonts/VeraBd.ttf','font/ttf'))


MEASURE_LAYOUT = """() => [...document.querySelectorAll('.sheet')].map((sheet, index) => {
 const bounds=sheet.getBoundingClientRect(), content=sheet.querySelector('.content');
 const limit=content.getBoundingClientRect();
 const header=sheet.querySelector('.hero').getBoundingClientRect();
 const frameElement=sheet.querySelector('.photo-frame');
 const frame=frameElement.getBoundingClientRect();
 const photograph=frameElement.classList.contains('photograph');
 const scene=sheet.querySelector('.scene');
 const relative=box => [box.left-bounds.left,box.top-bounds.top,box.width,box.height]
   .map(value => Math.round(value*100)/100);
 const photo=scene ? getComputedStyle(scene) : null;
 const headerPassed=Math.abs(frame.left-header.left-header.width*(photograph ? .36 : 0))<=.5
   && Math.abs(frame.right-header.right)<=.5 && Math.abs(frame.top-header.top)<=.5
   && Math.abs(frame.bottom-header.bottom)<=.5
   && (!scene || (photo.objectFit==='cover' && photo.objectPosition===(photograph ? '50% 42%' : '50% 61%')
       && scene.naturalWidth>0 && scene.naturalHeight>0));
 const elements=[...sheet.querySelectorAll('.card,.qr-card,.heading')];
 const overflow=elements.some(el => {
   const b=el.getBoundingClientRect();
   return b.left < bounds.left-.5 || b.right > bounds.right+.5 || b.bottom > limit.bottom+.5;
 }) || sheet.querySelector('.heading').getBoundingClientRect().bottom > limit.bottom;
 const codes=[...sheet.querySelectorAll('[data-code-id]')].map(el => {
   const b=el.getBoundingClientRect();
   return {id:el.dataset.codeId,page:index+1,
     bbox:[b.left-bounds.left,b.top-bounds.top,b.right-bounds.left,b.bottom-bounds.top].map(v=>v*72/96)};
 });
 return {page:index+1,passed:!overflow,compact:sheet.classList.contains('compact'),codes,
   header:{passed:headerPassed,box:relative(header),photo_frame:relative(frame),photo_visible:!!scene}};
})"""


def critical_value_present(value: str, text: str) -> bool:
    normalized=_normalized(text)
    def contains(fact):
        characters=_normalized(fact).replace(' ','')
        if not characters:
            return False
        # Whitespace may come from glyph tracking or line wrapping. Boundaries
        # still reject a longer/different booking code, price or ticket number.
        pattern=r'(?<![a-z0-9])'+r'\s*'.join(map(re.escape,characters))+r'(?![a-z0-9])'
        return re.search(pattern,normalized) is not None
    if contains(value):
        return True
    # Equivalent, bounded presentations of the same facts. Never fuzzy-match
    # names, booking codes, ticket numbers, dates, prices or standalone numbers.
    pair=re.fullmatch(r'(\d{2}:\d{2}) ([A-Z]{3})',value.strip())
    if pair and contains(pair[2]+' '+pair[1]):
        return True
    hours=re.fullmatch(r'(\d{2}:\d{2})[-–—](\d{2}:\d{2})',value.strip())
    if hours:
        return any(contains(hours[1]+' '+separator+' '+hours[2]) for separator in ('às','até','a','to'))
    distance=re.fullmatch(r'(\d+) a (\d+) metros',value.strip())
    if distance:
        equivalent=f'entre {distance[1]} e {distance[2]} metros'
        return contains(equivalent)
    return False


def reconcile_source_locators(plan, extracted):
    """Recover a missing critical locator only from its associated source page.

    The provider response/cache remains immutable. The rendered plan records the
    recovered field and provenance; unsupported/made-up critical facts still fail.
    """
    result=plan.model_copy(deep=True)
    adjustments=[]
    for number,page in enumerate(result.pages,1):
        allowed_pages={int(match[1]) for block_id in page.source_block_ids
                       if (match:=re.fullmatch(r'p(\d+)-b\d+',block_id))}
        locators={}
        for source in extracted['pages']:
            if source['page'] not in allowed_pages:
                continue
            source_text='\n'.join(block['text'] for block in source['blocks'])
            for match in re.finditer(r'\bLocalizador\s*:\s*([A-Za-z0-9][A-Za-z0-9/-]{4,79})',
                                     source_text,re.IGNORECASE):
                locators[match[1]] = source['page']
        recovered=[]
        for value in page.critical_values:
            if value in locators and not critical_value_present(value,page_text(page)):
                recovered.append('Localizador: '+value)
                adjustments.append({'page':number,'kind':'source_locator_added',
                                    'source_page':locators[value]})
        if recovered:
            page.sections.append(Section(title='Referências da reserva',tone='neutral',bullets=recovered))
    return result,adjustments


def check_pdf_text(text: str, page) -> dict:
    expected = Counter(_normalized(page_text(page)).split())
    actual = Counter(_normalized(text).split())
    coverage = sum((expected & actual).values()) / max(1, sum(expected.values()))
    critical = all(critical_value_present(v,text) for v in page.critical_values)
    return {'passed':coverage == 1 and critical, 'word_coverage':round(coverage,4),
            'critical_values_match':critical, 'verification':'pdf_text_layer'}


def render_html_document(settings, plan, coverage, extracted, output_dir, planner, scope='full',media_asset=None,original_download=None):
    if scope not in {'full','preview'}:
        raise AIError('HTML_SCOPE_INVALID','Escopo do template inválido.')
    _validate_codes(extracted['codes'])
    canonical=extracted['data'].get('pipeline_version')=='canonical_v1'
    if canonical:
        flight_fields={}; adjustments=[]
    else:
        plan,hotel_adjustments=preserve_hotel_data(plan,extracted)
        plan,flight_fields,adjustments=prepare_customer_plan(plan,extracted)
        adjustments=hotel_adjustments+adjustments
        plan,locator_adjustments=reconcile_source_locators(plan,extracted)
        adjustments+=locator_adjustments
        coverage=validate_plan(plan,extracted,settings.ai_max_pages)
    output_dir=Path(output_dir)
    output_dir.mkdir(parents=True,exist_ok=True)
    markup=build_html(plan,extracted['codes'],extracted['data'],settings.brand_dir,scope,media_asset,flight_fields,original_download)
    (output_dir/'document.html').write_text(markup,encoding='utf-8')
    if settings.html_engine=='weasyprint':
        from .weasy_rendering import render_document
        plan,layout,markup,raw=render_document(settings,plan,
            lambda candidate,selected_scope:build_html(candidate,extracted['codes'],extracted['data'],
                settings.brand_dir,selected_scope,media_asset,flight_fields,
                original_download if selected_scope=='full' else None,measurement=True),
            canonical=canonical,scope=scope,photographic=bool(media_asset),
            photo_visible=bool(media_asset or extracted['data'].get('hero_asset')))
        (output_dir/'document.html').write_text(markup,encoding='utf-8')
    else:
        try:
            with sync_playwright() as playwright:
                browser_env={key:os.environ[key] for key in ('PATH','SystemRoot','WINDIR','TEMP','TMP',
                    'HOME','USERPROFILE','LOCALAPPDATA','LANG','LC_ALL','DISPLAY','XDG_RUNTIME_DIR',
                    'FONTCONFIG_PATH','PLAYWRIGHT_BROWSERS_PATH') if key in os.environ}
                browser=playwright.chromium.launch(headless=True,channel=settings.html_browser_channel,
                                                   chromium_sandbox=True,env=browser_env)
                try:
                    context=browser.new_context(java_script_enabled=False,service_workers='block')
                    context.route('**/*',lambda route:route.abort())
                    page=context.new_page()
                    page.set_default_timeout(30000)
                    page.emulate_media(media='print')
                    if canonical:
                        from .pagination import paginate_in_browser
                        # Measure the complete document even for preview so continuation
                        # later reuses the same content and deterministic pagination.
                        plan,layout,markup=paginate_in_browser(page,plan,
                            lambda p:build_html(p,extracted['codes'],extracted['data'],settings.brand_dir,
                                'full',media_asset,flight_fields,original_download),MEASURE_LAYOUT,settings.max_output_pages)
                        (output_dir/'document.html').write_text(markup,encoding='utf-8')
                        if scope=='preview':
                            page.set_content(build_html(plan,extracted['codes'],extracted['data'],settings.brand_dir,
                                scope,media_asset,flight_fields,None),wait_until='load')
                            page.evaluate('() => document.fonts.ready')
                            layout=page.evaluate(MEASURE_LAYOUT)
                    else:
                        page.set_content(markup,wait_until='load')
                        page.evaluate('() => document.fonts.ready')
                        loaded=page.evaluate('() => [...document.images].every(i => i.complete && i.naturalWidth > 0)')
                        if not loaded:
                            raise AIError('HTML_ASSET_LOAD','Uma imagem do template não foi carregada.')
                        layout=page.evaluate(MEASURE_LAYOUT)
                        for item in layout:
                            if not item['passed']:
                                page.locator('.sheet').nth(item['page']-1).evaluate("el => el.classList.add('compact')")
                        layout=page.evaluate(MEASURE_LAYOUT)
                        if not all(item['passed'] for item in layout):
                            raise AIError('HTML_LAYOUT_OVERFLOW','O conteúdo excede a área legível do template. Revisar a distribuição das páginas.')
                    if not all(item['header']['passed'] and item['header']==layout[0]['header'] for item in layout):
                        raise AIError('HTML_HEADER_LAYOUT', 'A geometria do cabeçalho não é uniforme.')
                    raw=page.pdf(format='A4',print_background=True,prefer_css_page_size=True,
                                 display_header_footer=False)
                finally:
                    browser.close()
        except BrowserError:
            raise AIError('HTML_BROWSER_UNAVAILABLE','Não foi possível renderizar o template. Verifique a instalação do Chromium no worker.') from None
    selected=plan.pages[:1] if scope=='preview' else plan.pages
    (output_dir/'browser.pdf').write_bytes(raw)
    code_map={c['id']:c for c in extracted['codes']}
    placements=[]
    paths=[]
    text_checks=[]
    pdf_path=output_dir/'document.pdf'
    with fitz.open(stream=raw,filetype='pdf') as pdf:
        if len(pdf)!=len(selected):
            raise AIError('HTML_PAGE_COUNT','A paginação do navegador diverge do plano. Não liberar o documento.')
        for index,pdf_page in enumerate(pdf):
            for placement in layout[index]['codes']:
                code=code_map[placement['id']]
                box=fitz.Rect(placement['bbox'])
                with Image.open(code['path']) as original:
                    scale=min(box.width/original.width,box.height/original.height)
                    width,height=original.width*scale,original.height*scale
                target=fitz.Rect(box.x0+(box.width-width)/2,box.y0+(box.height-height)/2,
                                 box.x0+(box.width+width)/2,box.y0+(box.height+height)/2)
                pdf_page.insert_image(target,filename=code['path'],overlay=True)
                placements.append({'id':code['id'],'page':index+1,'bbox':list(target),
                                   'path':str(Path(code['path']).resolve())})
            # Chromium positions tracked headings glyph by glyph. Inhibit only
            # MuPDF's inferred spaces; genuine word-space glyphs remain intact.
            text=pdf_page.get_text(flags=fitz.TEXTFLAGS_TEXT | fitz.TEXT_INHIBIT_SPACES)
            text_checks.append({'page':index+1,**check_pdf_text(text,selected[index])})
        if not all(item['passed'] for item in text_checks):
            raise AIError('HTML_TEXT_MISMATCH','O PDF não preservou todo o texto planejado. Não liberar.')
        pdf.set_metadata({'title':plan.title,'author':'Diff Travel','subject':'Documento premium'})
        if original_download:
            links=[link for link in pdf[-1].get_links() if link.get('uri')==original_download['url']]
            if len(links)!=1:
                raise AIError('ORIGINAL_DOWNLOAD_LINK_MISSING','O botão de download do original não foi preservado no PDF.')
        pdf.save(pdf_path,garbage=3,deflate=True)
    with fitz.open(pdf_path) as pdf:
        for index,pdf_page in enumerate(pdf,1):
            path=output_dir/f'page-{index:03d}.png'
            pdf_page.get_pixmap(dpi=settings.render_dpi,alpha=False).save(path)
            paths.append(str(path.resolve()))
    return {'pdf_path':str(pdf_path.resolve()),'page_paths':paths,'page_count':len(paths),
        'code_placements':placements,'render_mode':'html_template','html_engine':settings.html_engine,'content_plan':plan.model_dump(),
        'generation_scope':scope,'planned_page_count':len(plan.pages),'content_plan_checks':coverage,
        'template_version':TEMPLATE_VERSION,'template_sha256':hashlib.sha256(
            (TEMPLATE_DIR/'premium.html').read_bytes()).hexdigest(),
        'template_text_checks':text_checks,'template_layout_checks':[
            {k:v for k,v in item.items() if k!='codes'} for item in layout],
        'template_adjustments':adjustments,
        'original_download':{'included':bool(original_download),'expires_at':original_download.get('expires_at') if original_download else None,
                             'kind':original_download.get('kind','source_capability') if original_download else None},
        'image_generation_performed':False,'ai_usage':planner.ledger.summary(),
        'ai_cache_hits':planner.cache_hits}
