"""Check literal evidence and relationships. This is not a semantic accuracy guarantee."""
import re
import unicodedata

from .ai_images import AIError


def literal(value):
    return re.sub(r'\s+',' ',unicodedata.normalize('NFKC',value)).strip()


def traveler_spacing(text):
    """A vertical separator or line wrapping between categories has no value."""
    return literal(text.replace('|', ' '))


def contains(value,text,*,financial=False):
    value,text=literal(value),literal(text)
    if not value: return False
    left=r'(?<![\w])' if value[0].isalnum() else ''
    right=r'(?![\w])' if value[-1].isalnum() else ''
    if value.isdigit():
        left=r'(?<![\w])(?<!\d[.,])'
        right=r'(?![\w]|[.,]\d)'
    elif value[-1].isdigit():
        right=r'(?![\w]|[.,]\d)'
    matches=list(re.finditer(left+re.escape(value)+right,text,re.I))
    if financial:
        matches=[m for m in matches if not re.search(r'[-−+]\s*(?:R\$|US\$|USD|BRL|\$|€|£)?\s*$',text[:m.start()],re.I)]
    return bool(matches)


def source_blocks(extracted):
    return {f'p{p["page"]}-b{i+1}':{**b,'page':p['page']}
            for p in extracted['pages'] for i,b in enumerate(p['blocks'])}


def issue(code,message,*,path=None,blocking=True,evidence=None):
    result={'code':code,'blocking':blocking,'message':message}
    if path: result['field']=path
    if evidence: result['source_block_ids']=[e.block_id for e in evidence]
    return result


def validate_understanding(document,extracted):
    blocks=source_blocks(extracted)
    issues=[]
    if (len(document.facts)>2000 or len(document.entities)>500 or len(document.services)>200
            or len(document.conditions)>2000 or len(document.code_associations)>500):
        raise AIError('DOCUMENT_SCHEMA_LIMIT','A estrutura excede os limites de revisão deste documento.')
    groups=(document.entities,document.services,document.facts,document.conditions)
    for group in groups:
        ids=[x.id for x in group]
        if len(ids)!=len(set(ids)) or any(not re.fullmatch(r'[a-zA-Z0-9_-]{1,80}',x) for x in ids):
            raise AIError('DOCUMENT_IDS_INVALID','Identificadores estruturados inválidos ou duplicados.')
    entities={x.id:x for x in document.entities}
    services={x.id:x for x in document.services}
    facts={x.id:x for x in document.facts}

    def evidence_ok(evidence):
        return bool(evidence) and all(e.block_id in blocks and e.quote.strip()
            and contains(e.quote,blocks[e.block_id]['text']) for e in evidence)

    def grounded(value,evidence,*,financial=False,spacing=literal):
        return evidence_ok(evidence) and any(contains(spacing(value),spacing(e.quote),financial=financial)
            and contains(spacing(value),spacing(blocks[e.block_id]['text']),financial=financial) for e in evidence)

    for entity in document.entities:
        if not grounded(entity.name,entity.evidence) or entity.locality and not grounded(entity.locality,entity.evidence):
            issues.append(issue('ENTITY_EVIDENCE_REVIEW','Identidade sem trecho correspondente na fonte.',
                path=f'entities.{entity.id}',evidence=entity.evidence))
    for service in document.services:
        if (not evidence_ok(service.evidence) or any(x not in entities for x in service.entity_ids)
                or (service.name and not grounded(service.name,service.evidence))):
            issues.append(issue('SERVICE_EVIDENCE_REVIEW','Serviço ou seus vínculos exigem conferência.',
                path=f'services.{service.id}',evidence=service.evidence))
        service_facts={f.kind for f in document.facts if f.service_id==service.id and f.status=='supported'}
        anchors={'flight':{'flight_number','origin','destination','departure_time','departure_date'},
            'hotel':{'hotel_name','checkin','checkout','checkin_date','checkout_date','room','stay_period'},
            'ticket':{'ticket_number','product_name','validity'},
            'car_rental':{'vehicle','pickup','dropoff','pickup_date','dropoff_date','rental_reference'}}
        property_present=service.kind=='hotel' and any(entities[x].role=='property'
            for x in service.entity_ids if x in entities)
        if service.kind in anchors and not service_facts.intersection(anchors[service.kind]) and not property_present:
            issues.append(issue('SERVICE_FIELDS_REVIEW','Conferir se há contratação deste serviço além das condições gerais.',
                path=f'services.{service.id}',evidence=service.evidence))
    for fact in document.facts:
        path=f'facts.{fact.id}'
        if (fact.service_id is not None and fact.service_id not in services
                or fact.entity_id is not None and fact.entity_id not in entities):
            issues.append(issue('FACT_RELATION_REVIEW','Campo aponta para entidade/serviço inexistente.',path=path))
        if fact.status=='supported':
            numeric_value=bool(re.fullmatch(r'(?:[A-Z]{3}\s*)?[-+−]?\s*(?:R\$|US\$|[$€£])?\s*\d[\d.,\s]*',fact.raw_value))
            if not grounded(fact.raw_value,fact.evidence,
                    financial=numeric_value or fact.kind in {'amount','price','fare','fee','tax','total','deposit'},
                    spacing=traveler_spacing if fact.kind=='traveler_count' else literal):
                issues.append(issue('FACT_EVIDENCE_REVIEW','Valor sem correspondência literal na evidência.',
                    path=path,evidence=fact.evidence))
        elif fact.status in {'ambiguous','unreadable'} or fact.raw_value.strip():
            issues.append(issue('FACT_UNCERTAIN','Campo ausente, ilegível ou ambíguo requer revisão.',
                path=path,evidence=fact.evidence))
    for condition in document.conditions:
        if (not evidence_ok(condition.evidence)
                or condition.service_id is not None and condition.service_id not in services):
            issues.append(issue('CONDITION_EVIDENCE_REVIEW','Condição sem escopo/evidência válida.',
                path=f'conditions.{condition.id}',evidence=condition.evidence))

    codes={c['id']:c for c in extracted['codes']}
    associations={}
    for page in document.unreadable_code_pages:
        if page not in {p['page'] for p in extracted['pages']}:
            raise AIError('DOCUMENT_CODE_PAGE_INVALID','Página de código ilegível inexistente.')
        issues.append(issue('CODE_UNREADABLE','A leitura visual indicou código sem identificação segura.',path=f'pages.{page}'))
    for association in document.code_associations:
        key=association.code_id
        if key in associations or key not in codes:
            raise AIError('DOCUMENT_CODES_INVALID','Associação de código desconhecida ou duplicada.')
        associations[key]=association
        holders=[entities.get(x) for x in association.holder_entity_ids]
        numbers=[facts.get(x) for x in association.number_fact_ids]
        valid=(evidence_ok(association.evidence) and association.service_ids
            and all(x in services for x in association.service_ids)
            and all(x is not None and x.role=='person' and grounded(x.name,x.evidence) for x in holders)
            and all(x is not None and x.status=='supported' and grounded(x.raw_value,x.evidence) for x in numbers))
        if association.purpose=='ticket':
            # Titles have no role in this check. Bind holder and printed number
            # to the source page and the actual decoded code, not just the PDF.
            valid=valid and len(holders)==1 and len(numbers)==1
            if valid:
                holder,number=holders[0],numbers[0]
                valid=(evidence_ok(holder.evidence) and evidence_ok(number.evidence)
                    and any(blocks[e.block_id]['page']==codes[key]['source_page'] for e in holder.evidence)
                    and any(blocks[e.block_id]['page']==codes[key]['source_page'] for e in number.evidence)
                    and number.kind=='ticket_number' and number.entity_id==holder.id
                    and number.service_id in association.service_ids
                    and any(contains(number.raw_value,payload) for payload in codes[key]['payloads']))
                people=[p for p in document.entities if p.role=='person' and evidence_ok(p.evidence)
                    and any(blocks[e.block_id]['page']==codes[key]['source_page'] for e in p.evidence)]
                if valid and len(people)>1:
                    # Presence on the same page is insufficient when several
                    # travellers appear there. Require an unambiguous joint block;
                    # other arrangements stay reviewable instead of guessing.
                    shared={e.block_id for e in holder.evidence}&{e.block_id for e in number.evidence}
                    valid=any(contains(holder.name,blocks[b]['text']) and contains(number.raw_value,blocks[b]['text'])
                        and not any(p.id!=holder.id and contains(p.name,blocks[b]['text']) for p in people)
                        for b in shared)
        if association.purpose=='unknown' or not valid:
            issues.append(issue('CODE_ASSOCIATION_REVIEW','Conferir serviço, titular e identificador deste código.',
                path=f'codes.{key}',evidence=association.evidence))
    for key in codes.keys()-associations.keys():
        issues.append(issue('CODE_ASSOCIATION_REVIEW','Código original preservado sem associação comprovada.',path=f'codes.{key}'))
    # Same kind/subject may legitimately repeat (flight legs); only exact evidence
    # overlap with different values proves a local contradiction.
    by_slot={}
    for fact in document.facts:
        if fact.status!='supported': continue
        if fact.kind not in {'departure_date','arrival_date','departure_time','arrival_time','ticket_number','birth_date'}:
            continue
        slot=(fact.service_id,fact.entity_id,fact.kind,tuple(sorted(e.block_id for e in fact.evidence)))
        if slot in by_slot and literal(by_slot[slot].raw_value)!=literal(fact.raw_value):
            issues.append(issue('FACT_CONFLICT_REVIEW','Valores distintos para o mesmo campo/evidência.',path=f'facts.{fact.id}'))
        by_slot[slot]=fact
    if not document.services:
        issues.append(issue('DOCUMENT_SCOPE_REVIEW','Conferir o escopo do documento e os serviços apresentados.'))
    return {'schema_version':'canonical_v1','evidence_validation_passed':not any(i['blocking'] for i in issues),
            'services':len(document.services),'facts':len(document.facts),'codes':len(codes),
            'semantic_completeness':'requires_human_review'},issues
