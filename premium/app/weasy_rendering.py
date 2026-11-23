"""Explicit HTML engine for standard containers, with no browser subprocess.

Only application-owned HTML/CSS and embedded assets are accepted. Measurements
use the pinned engine's public Page.anchors API, not its private box tree.
"""
from __future__ import annotations

import re

from .ai_images import AIError

ENGINE_VERSION = '70.0'
PX_PER_MM = 96 / 25.4
PAGED_CSS = '''<style>
.sheet {display:block;break-inside:avoid}
.content {height:auto}
.footer {position:absolute;bottom:0;left:0;right:0;margin-top:0}
.top-grid {display:table;table-layout:fixed;width:100%}
.summary-column {display:table-cell;padding-right:3.5mm;vertical-align:top}
.qr-column {display:table-cell;width:52mm;vertical-align:top}
.no-code>.summary-column {padding-right:0}
.summary {display:block}
.summary>.card+.card {margin-top:3mm}
.summary.paired {display:flex;gap:3mm}
.summary.paired>.card {flex:1.65;min-width:0;margin-top:0}
.summary.paired>.card:last-child {flex:1}
.wide-code {display:block}
.wide-code>.summary-column,.wide-code>.qr-column {display:block;width:100%;padding:0}
.wide-code>.qr-column {margin-top:3mm}
.field-pair,.flight-route,.flight-airports {display:flex}
.field-pair>.field,.flight-terminal,.flight-airports>span {flex:1;min-width:0}
.flight-arrow {flex:0 0 9mm}
</style>'''


def _runtime():
    try:
        import weasyprint
        from weasyprint.text.fonts import FontConfiguration
        from weasyprint.urls import URLFetcher
        if weasyprint.__version__ != ENGINE_VERSION:
            raise RuntimeError('Pinned engine required')
        return weasyprint.HTML,FontConfiguration,URLFetcher
    except (ImportError,OSError,RuntimeError):
        raise AIError('HTML_ENGINE_UNAVAILABLE',
                      'O motor HTML/Pango não está instalado na versão validada.') from None


def check_runtime():
    HTML,FontConfiguration,URLFetcher=_runtime()
    document=HTML(string='<html><body>Diff Travel</body></html>',
        url_fetcher=URLFetcher(allowed_protocols=['data'],fail_on_errors=True)).render(
            font_config=FontConfiguration())
    if len(document.pages)!=1 or not document.write_pdf().startswith(b'%PDF-'):
        raise AIError('HTML_ENGINE_UNAVAILABLE','A validação do motor HTML falhou.')


class MeasuredDocument:
    def __init__(self, *, photographic=False, photo_visible=False):
        self.HTML,FontConfiguration,self.URLFetcher=_runtime()
        self.fonts=FontConfiguration()
        self.photographic=photographic
        self.photo_visible=photo_visible
        self.document=None

    def render(self,markup,plan):
        try:
            self.document=self.HTML(string=markup.replace('</head>',PAGED_CSS+'</head>'),
                url_fetcher=self.URLFetcher(allowed_protocols=['data'],fail_on_errors=True)).render(
                    font_config=self.fonts)
        except Exception:
            raise AIError('HTML_ASSET_LOAD',
                          'Não foi possível carregar ou compor os recursos do template.') from None
        layout=[]
        for index,expected in enumerate(plan.pages,1):
            prefix=f'dt-p{index}'
            matches=[(n,page) for n,page in enumerate(self.document.pages,1)
                     if prefix+'-sheet-start' in page.anchors]
            if len(matches)!=1:
                layout.append({'page':index,'passed':False,'codes':[],'compact':False,
                               'header':{'passed':False}})
                continue
            actual_index,page=matches[0]
            anchors=page.anchors
            required={'sheet','content','hero','photo','heading','eyebrow','footer'}
            required.update('card-'+str(n) for n in range(1,len(expected.sections)+1))
            if expected.code_id: required.update({'qr','code'})
            if any(prefix+'-'+name+'-'+point not in anchors
                   for name in required for point in ('start','end')):
                # Fragmentation is a measured overflow. Let the common paginator
                # split this logical page and measure it again before printing.
                layout.append({'page':index,'passed':False,'codes':[],'compact':False,
                               'header':{'passed':False}})
                continue
            def box(name):
                # The documented anchor target is an (x,y) CSS-pixel point.
                start=anchors.get(prefix+'-'+name+'-start')
                end=anchors.get(prefix+'-'+name+'-end')
                if start is None or end is None:
                    raise AIError('HTML_LAYOUT_MEASUREMENT','A medição do template está incompleta.')
                return [float(start[0]),float(start[1]),float(end[0]),float(end[1])]
            sheet=box('sheet'); content=box('content'); hero=box('hero'); photo=box('photo')
            content_limit=box('footer')[1]-4*PX_PER_MM
            names={'heading'}
            if expected.code_id: names.add('qr')
            names.update('card-'+m[1] for key in anchors
                         if (m:=re.fullmatch(re.escape(prefix)+r'-card-(\d+)-start',key)))
            measured=[box(name) for name in names]
            passed=all(rect[0]>=sheet[0]-1 and rect[2]<=sheet[2]+1
                       and rect[3]<=content_limit+1 for rect in measured)
            passed=passed and content[3]>=content[1] and len(measured)>=1+len(expected.sections)
            header_passed=(abs(hero[0])<1 and abs(hero[1])<1
                and abs(hero[2]-210*PX_PER_MM)<1 and abs(hero[3]-62*PX_PER_MM)<1
                and abs(photo[0]-hero[2]*(.36 if self.photographic else 0))<1
                and all(abs(photo[n]-hero[n])<1 for n in (1,2,3)))
            def relative(rect):
                return [round(rect[0]-sheet[0],2),round(rect[1]-sheet[1],2),
                        round(rect[2]-rect[0],2),round(rect[3]-rect[1],2)]
            codes=[]
            if expected.code_id:
                codes=[{'id':expected.code_id,'page':index,'bbox':[x*72/96 for x in box('code')]}]
            layout.append({'page':index,'passed':passed and actual_index==index,
                'split_needed':not passed,'compact':False,'codes':codes,
                'text_tracking_boxes':[[x*72/96 for x in box('eyebrow')]],
                'header':{'passed':header_passed,'box':relative(hero),'photo_frame':relative(photo),
                          'photo_visible':self.photo_visible}})
        if len(self.document.pages)!=len(plan.pages) and all(item['passed'] for item in layout):
            raise AIError('HTML_PAGE_COUNT','A paginação física diverge do plano medido.')
        return layout,markup

    def pdf(self):
        return self.document.write_pdf()


def render_document(settings,plan,render_markup,*,canonical,scope,photographic,photo_visible):
    measured=MeasuredDocument(photographic=photographic,photo_visible=photo_visible)
    if canonical:
        from .pagination import paginate_measured
        plan,layout,markup=paginate_measured(plan,
            lambda candidate:measured.render(render_markup(candidate,'full'),candidate),settings.max_output_pages)
        if scope=='preview':
            preview=plan.model_copy(deep=True); preview.pages=plan.pages[:1]
            layout,_=measured.render(render_markup(plan,'preview'),preview)
    else:
        layout,markup=measured.render(render_markup(plan,scope),plan if scope=='full' else
            plan.model_copy(update={'pages':plan.pages[:1]}))
    if not all(item['passed'] for item in layout):
        raise AIError('HTML_LAYOUT_OVERFLOW','O conteúdo excede a área legível do template.')
    if not all(item['header']['passed'] and item['header']==layout[0]['header'] for item in layout):
        raise AIError('HTML_HEADER_LAYOUT','A geometria do cabeçalho não é uniforme.')
    return plan,layout,markup,measured.pdf()
