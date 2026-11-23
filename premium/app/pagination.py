"""Split measured overflow into continuation pages without reducing the font."""
import re

from .ai_images import AIError,Section,page_text
from .html_rendering import critical_value_present


def coalesce_cards(plan):
    """Fill space across logical sections; keep independent codes on separate pages.

    A code may share cards from its own source context. Source-page evidence is
    not a sufficient association for validation; that was checked independently.
    """
    result=plan.model_copy(deep=True)
    pages=[]
    for item in result.pages:
        if not pages:
            pages.append(item); continue
        previous=pages[-1]
        same_context=(set(item.source_block_ids)<=set(previous.source_block_ids)
                      or set(previous.source_block_ids)<=set(item.source_block_ids))
        if ((not previous.code_id and not item.code_id)
                or bool(previous.code_id)!=bool(item.code_id) and same_context):
            previous.sections.extend(item.sections)
            previous.source_block_ids=list(dict.fromkeys(previous.source_block_ids+item.source_block_ids))
            previous.critical_values=list(dict.fromkeys(previous.critical_values+item.critical_values))
            previous.code_id=previous.code_id or item.code_id
            previous.title=plan.title
        else:
            pages.append(item)
    result.pages=pages
    return result


def split_page(original):
    first=original.model_copy(deep=True)
    second=original.model_copy(deep=True)
    second.code_id=None
    second.introduction=''
    second.title=original.title if original.title.endswith(' — continuação') else original.title+' — continuação'
    sections=first.sections
    if len(sections)>1:
        pivot=max(1,len(sections)//2)
        first.sections=sections[:pivot]
        second.sections=sections[pivot:]
    elif sections and len(sections[0].bullets)>1:
        bullets=list(sections[0].bullets)
        pivot=max(1,len(bullets)//2)
        first.sections[0].bullets=bullets[:pivot]
        second.sections[0].bullets=bullets[pivot:]
    elif sections and sections[0].bullets:
        text=sections[0].bullets[0]
        boundaries=[m.start() for m in re.finditer(r'\s+',text) if 0<m.start()<len(text)-1]
        # Do not cut a critical identity/number/date in half.
        boundaries=[p for p in boundaries if not any(
            m.start()<p<m.end() for value in original.critical_values
            for m in re.finditer(re.escape(value),text))]
        if not boundaries:
            raise AIError('HTML_ATOMIC_CONTENT_OVERFLOW','Um campo indivisível excede a área legível; conferir sua apresentação.')
        pivot=min(boundaries,key=lambda x:abs(x-len(text)/2))
        first.sections[0].bullets=[text[:pivot].rstrip()]
        second.sections[0].bullets=[text[pivot:].lstrip()]
    else:
        raise AIError('HTML_ATOMIC_CONTENT_OVERFLOW','Cabeçalho/código excede a área de impressão.')
    for part in (first,second):
        part.critical_values=[v for v in original.critical_values if critical_value_present(v,page_text(part))]
    if any(not any(critical_value_present(v,page_text(p)) for p in (first,second)) for v in original.critical_values):
        raise AIError('HTML_CRITICAL_PAGINATION','A paginação separou um campo crítico; revisar distribuição.')
    return first,second


def _browser_measure(browser_page,render_markup,measure):
    def measured(plan):
        markup=render_markup(plan)
        browser_page.set_content(markup,wait_until='load')
        browser_page.evaluate('() => document.fonts.ready')
        if not browser_page.evaluate('() => [...document.images].every(i => i.complete && i.naturalWidth > 0)'):
            raise AIError('HTML_ASSET_LOAD','Uma imagem do template não foi carregada.')
        return browser_page.evaluate(measure),markup
    return measured


def paginate_in_browser(browser_page,plan,render_markup,measure,max_pages):
    return paginate_measured(plan,_browser_measure(browser_page,render_markup,measure),max_pages)


def paginate_measured(plan,measure_plan,max_pages):
    """Use actual engine geometry; never guess a character limit or trim data."""
    result=coalesce_cards(plan)
    for _ in range(max_pages*10):
        if len(result.pages)>max_pages:
            raise AIError('HTML_OUTPUT_PAGE_LIMIT','O documento requer mais páginas do que o limite configurado.')
        layout,markup=measure_plan(result)
        failures=[i for i,x in enumerate(layout) if not x['passed']]
        if not failures:
            return compact_measured(result,measure_plan)
        # Geometry already identifies every overflowing logical page. Split
        # independent failures together rather than rendering the entire long
        # document again after each single split. All new pages are remeasured.
        for failure in reversed(failures):
            first,second=split_page(result.pages[failure])
            result.pages[failure:failure+1]=[first,second]
    raise AIError('HTML_PAGINATION_LIMIT','A paginação não convergiu dentro do limite seguro.')


def compact_in_browser(browser_page,plan,render_markup,measure):
    return compact_measured(plan,_browser_measure(browser_page,render_markup,measure))


def compact_measured(plan,measure_plan):
    """Fill spare room with the next complete card, retaining source order.

    Optional, bounded layout optimization. Independent code pages stay atomic.
    Every accepted layout is measured; the last valid layout is restored on exit.
    """
    result=plan
    index=0; attempts=0
    while index<len(result.pages)-1 and attempts<64:
        current,following=result.pages[index:index+2]
        if current.code_id or following.code_id or not following.sections:
            index+=1; continue
        candidate=result.model_copy(deep=True)
        first,second=candidate.pages[index:index+2]
        first.sections.append(second.sections.pop(0))
        pool=list(dict.fromkeys(first.critical_values+second.critical_values))
        first.source_block_ids=list(dict.fromkeys(first.source_block_ids+second.source_block_ids))
        for part in (first,second):
            part.critical_values=[v for v in pool if critical_value_present(v,page_text(part))]
        if any(not any(critical_value_present(v,page_text(p)) for p in (first,second)) for v in pool):
            index+=1; continue
        if not second.sections: candidate.pages.pop(index+1)
        attempts+=1
        layout,_=measure_plan(candidate)
        if all(item['passed'] for item in layout): result=candidate
        else: index+=1
    layout,markup=measure_plan(result)
    if not all(item['passed'] for item in layout):
        raise AIError('HTML_LAYOUT_OVERFLOW','A conferência final da distribuição dos cards falhou.')
    return result,layout,markup
