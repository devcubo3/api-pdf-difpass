"""Deterministic customer presentation; immutable paid responses stay unchanged.

Only unambiguous flight fields are rearranged. Unrecognized sentences remain
visible. Editorial remarks about extraction are removed only when not in evidence.
"""
import re
from .ai_images import _normalized


EDITORIAL_NOTE=re.compile(
    r'\((?:o\s+)?(?:nome|texto|trecho)\s+(?:aparece|est[aá])\s+'
    r'(?:truncad[oa]|cortad[oa]|ileg[ií]vel)\s+(?:na|no)\s+'
    r'(?:fonte|pdf|original|documento original)\)',re.I)
AIRPORT_LABEL=re.compile(r'\b(?P<label>Origem(?:\s+indicad[ao])?|Destino(?:\s+indicad[ao])?|'
    r'Aeroporto de sa[ií]da|Aeroporto de chegada)\s*:\s*(?P<value>.+)$',re.I)
FLIGHT_NUMBER=re.compile(r'\bVoo\s*:?\s*([A-Z]{0,3}\s*\d{1,6}[A-Z]?)\b\.?',re.I)
TERMINAL=r'(?P<{prefix}_city>[^;\n→]+?)\s*\((?P<{prefix}_code>[A-Z]{{3}})\)\s*(?P<{prefix}_time>\d{{2}}:\d{{2}})'
ROUTE=re.compile(r'^\s*'+TERMINAL.format(prefix='departure')+r'\s*(?:→|->|\s+[–—]\s+)\s*'
                 +TERMINAL.format(prefix='arrival')+r'\s*(?:;\s*dura[çc][aã]o\s*:\s*'
                 r'(?P<duration>\d{1,2}h\d{2}))?\s*\.?\s*$',re.I)
LABELED_TERMINAL=re.compile(r'^\s*(Sa[ií]da|Chegada)\s*:\s*'
    r'(?P<city>.+?)\s*\((?P<code>[A-Z]{3})\)\s*(?P<time>\d{2}:\d{2})\s*\.?$',re.I)


def _strip_period(value):
    value=value.strip()
    return value[:-1].rstrip() if value.endswith('.') and not value.endswith('...') else value


def _safe_time(value):
    hour,minute=map(int,value.split(':'))
    return hour<24 and minute<60


def _flight(section,evidence):
    route=None
    labeled={}
    consumed=set()
    for index,bullet in enumerate(section.bullets):
        match=ROUTE.fullmatch(bullet)
        if match:
            if route is not None:
                return None
            route=match.groupdict()
            consumed.add(index)
        match=LABELED_TERMINAL.fullmatch(bullet)
        if match:
            side='departure' if match[1].lower().startswith('sa') else 'arrival'
            if side in labeled:
                return None
            labeled[side]=match.groupdict()
            consumed.add(index)
    if route is not None and labeled:
        return None
    if route is None and set(labeled)=={'departure','arrival'}:
        route={f'{side}_{key}':item[key] for side,item in labeled.items() for key in ('city','code','time')}
        route['duration']=None
    if route is None:
        return None
    route={key:value.strip() if isinstance(value,str) else value for key,value in route.items()}
    for side in ('departure','arrival'):
        time,code=route[f'{side}_time'],route[f'{side}_code'].upper()
        if not _safe_time(time):
            return None
        # Require the same airport/time association in the associated source.
        if not any(pair in _normalized(evidence) for pair in (
                _normalized(time+' '+code),_normalized(code+' '+time))):
            return None
        if _normalized(route[f'{side}_city']) not in _normalized(evidence):
            return None
        route[f'{side}_code']=code
    airports={}
    number=None
    duration=route.get('duration')
    extra=[]
    for index,bullet in enumerate(section.bullets):
        if index in consumed:
            continue
        remaining=bullet
        match=FLIGHT_NUMBER.search(remaining)
        if match:
            value=match[1].strip()
            if number is not None or not re.search(r'(?<![a-z0-9])'+re.escape(_normalized(value))+r'(?![a-z0-9])',_normalized(evidence)):
                return None
            number=value
            remaining=(remaining[:match.start()]+remaining[match.end():]).strip()
        match=AIRPORT_LABEL.fullmatch(remaining)
        if match:
            side='departure' if _normalized(match['label']).startswith(('origem','aeroporto de saida')) else 'arrival'
            if side in airports:
                return None
            value=_strip_period(match['value'])
            # The airport is a source fact; never expand truncated names.
            if _normalized(value) not in _normalized(evidence):
                return None
            airports[side]=value
            remaining=''
        match=re.fullmatch(r'Dura[çc][aã]o\s*:\s*(\d{1,2}h\d{2})\.?',remaining,re.I)
        if match:
            if duration is not None and duration!=match[1]:
                return None
            duration=match[1]
            remaining=''
        if remaining:
            extra.append(remaining)
    if duration and _normalized(duration) not in _normalized(evidence):
        return None
    if not number:
        return None
    fields={side:{'city':route[f'{side}_city'],'code':route[f'{side}_code'],
        'time':route[f'{side}_time'],'airport':airports.get(side)} for side in ('departure','arrival')}
    fields.update(number=number,duration=duration,extra=extra)
    canonical=[f'Saída: {fields["departure"]["city"]} ({fields["departure"]["code"]}) {fields["departure"]["time"]}.',
               f'Chegada: {fields["arrival"]["city"]} ({fields["arrival"]["code"]}) {fields["arrival"]["time"]}.',
               f'Voo {number}.']
    if duration:
        canonical.append(f'Duração: {duration}.')
    for side,label in (('departure','Aeroporto de saída'),('arrival','Aeroporto de chegada')):
        if fields[side]['airport']:
            canonical.append(f'{label}: {fields[side]["airport"]}')
    return fields,canonical+extra


def prepare_customer_plan(plan,extracted):
    result=plan.model_copy(deep=True)
    adjustments=[]
    flights={}
    blocks={f'p{p["page"]}-b{i+1}':block['text'] for p in extracted['pages'] for i,block in enumerate(p['blocks'])}
    original_text=_normalized('\n'.join(blocks.values()))
    for page_index,page in enumerate(result.pages):
        evidence='\n'.join(blocks[key] for key in page.source_block_ids if key in blocks)
        def clean(value):
            def replace(match):
                if _normalized(match[0]) in original_text:
                    return match[0]
                adjustments.append({'page':page_index+1,'kind':'unsupported_editorial_note_removed','note':match[0]})
                return ''
            return re.sub(r'[ \t]+',' ',EDITORIAL_NOTE.sub(replace,value)).strip()
        page.title=clean(page.title)
        page.eyebrow=clean(page.eyebrow)
        page.introduction=clean(page.introduction)
        page.critical_values=[fixed for value in page.critical_values if (fixed:=clean(value))]
        for section_index,section in enumerate(page.sections):
            section.title=clean(section.title)
            section.bullets=[fixed for bullet in section.bullets if (fixed:=clean(bullet))]
            flight=_flight(section,evidence)
            if flight:
                fields,canonical=flight
                section.bullets=canonical
                flights[(page_index,section_index)]=fields
                adjustments.append({'page':page_index+1,'section':section_index+1,'kind':'flight_fields_organized',
                                    'source_block_ids':page.source_block_ids})
    return result,flights,adjustments
