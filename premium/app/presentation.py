"""Compile grounded data to reusable cards. Unrepresented source text is preserved."""
import re

from .ai_images import Plan,Page,Section,page_text
from .document_validation import source_blocks,literal,contains
from .media_context import safe_public
from .flight_cards import flight_card,section_key
from .source_fields import PERSON_KINDS, driver_table_label, field_label

SERVICE_TITLES={'flight':'Seus voos','hotel':'Sua hospedagem','ticket':'Seus ingressos',
    'car_rental':'Locação de veículo','transfer':'Seu traslado','insurance':'Seu seguro',
    'cruise':'Seu cruzeiro','proposal':'Proposta','other':'Informações da viagem'}
CONDITION_TITLES={'general':'Condições gerais','cancellation':'Alteração e cancelamento',
    'baggage':'Bagagem','documentation':'Documentação','fees':'Taxas e cobranças',
    'coverage':'Coberturas','access':'Regras de acesso','checkin':'Check-in','other':'Condições'}
FACT_LABELS={'holder_name':'Titular','booking_reference':'Localizador','rental_reference':'Reserva',
    'ticket_number':'Número do ingresso','flight_number':'Voo','product_name':'Produto','validity':'Validade',
    'departure_date':'Data de saída','arrival_date':'Data de chegada','departure_time':'Horário de saída',
    'arrival_time':'Horário de chegada','origin':'Origem','destination':'Destino',
    'origin_city':'Cidade de saída','destination_city':'Cidade de chegada','origin_code':'Aeroporto de saída',
    'destination_code':'Aeroporto de chegada','baggage':'Bagagem','address':'Endereço','phone':'Telefone',
    'email':'E-mail','amount':'Valor','pickup':'Retirada','dropoff':'Devolução','vehicle':'Veículo',
    'checkin':'Check-in','checkout':'Check-out','policy_number':'Apólice','cabin':'Cabine','duration':'Duração',
    'class':'Classe','fare_class':'Classe tarifária','issue_date':'Data de emissão','age_group':'Faixa etária'}


def display_fact(fact,entities,invalid,service_kind=None,source=None):
    label='Nome' if fact.kind in PERSON_KINDS else FACT_LABELS.get(fact.kind)
    if fact.kind=='ticket_number' and service_kind=='flight':
        label='Número do bilhete'
    if fact.kind=='traveler_count':
        label='Passageiros'
    if source:
        label=source.label
    if not label:
        # A free-form label must not become a new promise, price or restriction.
        label=fact.label if any(contains(fact.label,e.quote) for e in fact.evidence) else 'Informação adicional'
    entity=entities.get(fact.entity_id)
    if (fact.kind in {'phone','email','address'} and entity
            and f'entities.{entity.id}' not in invalid):
        label+=' — '+entity.name
    value=fact.raw_value
    if source and source.qualifier:
        value=f'{value} {source.qualifier}'
    return fact.model_copy(update={'label':label,'raw_value':value})


def compile_presentation(document,extracted,issues):
    blocks=source_blocks(extracted)
    invalid={i.get('field') for i in issues if i.get('blocking')}
    entities={x.id:x for x in document.entities}
    pages=[]
    all_critical=[]
    fact_ids=[]
    field_sources=[]
    extracted['data']['structured_flights']={}

    def ids(evidence): return list(dict.fromkeys(e.block_id for e in evidence if e.block_id in blocks))
    def safe_name(entity):
        return entity and f'entities.{entity.id}' not in invalid
    def fact_source(fact,service_kind):
        source=field_label(fact.kind,fact.raw_value,fact.evidence,blocks,service_kind=service_kind)
        if not source and fact.kind=='driver_name' and service_kind=='car_rental':
            source=driver_table_label(fact,blocks)
        return source
    def record_source(source,subject_id,service_id):
        if source:
            field_sources.append({'field_id':subject_id,'service_id':service_id,
                'label':source.label,'source_label':source.source_label,
                'source_block_id':source.block_id})
    def condition_section(condition):
        text=' '.join(e.quote for e in condition.evidence)
        negative=bool(re.search(r'\bn[aã]o\b|\bnot\b|\bexclu[ií]|\bnon[- ]?refundable\b|\bpenalidade\b|\bmulta\b',text,re.I))
        included=bool(re.search(r'\binclu[ií]d[oa]s?\b|\bincluded\b|\bincludes\b',text,re.I))
        tone='attention' if negative else ('included' if included else 'neutral')
        return Section(title=CONDITION_TITLES[condition.kind],tone=tone,
            bullets=[e.quote for e in condition.evidence if e.block_id in blocks])
    condition_groups={}
    for condition in document.conditions:
        if f'conditions.{condition.id}' in invalid: continue
        key=(condition.service_id,tuple(sorted((e.block_id,literal(e.quote)) for e in condition.evidence)))
        if key in condition_groups:
            old,section,kinds=condition_groups[key]
            if condition.kind not in kinds:
                section.title+=' / '+CONDITION_TITLES[condition.kind]
                kinds.add(condition.kind)
        else:
            condition_groups[key]=(condition,condition_section(condition),{condition.kind})
    condition_cards=[(condition,section) for condition,section,_ in condition_groups.values()]
    for service in document.services:
        if f'services.{service.id}' in invalid: continue
        bullets=[]; critical=[]; references=ids(service.evidence)
        if service.name:
            bullets.append('Serviço: '+service.name)
            critical.append(service.name)
        source_facts=[(f,fact_source(f,service.kind)) for f in document.facts
            if f.service_id==service.id and f.status=='supported' and f'facts.{f.id}' not in invalid]
        person_labels={}
        for key in dict.fromkeys(service.entity_ids):
            entity=entities.get(key)
            if safe_name(entity):
                source=None
                if entity.role=='person':
                    source=field_label('person_name',entity.name,entity.evidence,blocks,service_kind=service.kind)
                    if source:
                        person_labels[key]=source
                    linked=[fact_label for fact,fact_label in source_facts if
                            fact.kind in PERSON_KINDS and fact.entity_id==key
                            and literal(fact.raw_value)==literal(entity.name)]
                    if linked and (not source or any(
                            not fact_label or fact_label.role==source.role for fact_label in linked)):
                        continue
                # A company found only in an explicit support/contact line has
                # no evidence of being the operating provider of this service.
                if entity.role=='provider' and all(re.match(
                        r'^\s*(?:sac\b|customer service\b|central de atendimento\b)',e.quote,re.I)
                        for e in entity.evidence):
                    continue
                label=source.label if source else {
                    'person':'Nome','provider':'Fornecedor','contact':'Contato',
                }.get(entity.role,'Identificação')
                bullets.append(f'{label}: {entity.name}')
                record_source(source,key,service.id)
                critical.append(entity.name); references+=ids(entity.evidence)
        service_facts=[]
        identity_bullets=list(bullets)
        rendered_people=set()
        for raw,source in source_facts:
            entity=entities.get(raw.entity_id)
            if raw.kind in PERSON_KINDS and safe_name(entity) and entity.role=='person':
                if literal(raw.raw_value)==literal(entity.name):
                    source=source or person_labels.get(entity.id)
                identity=(entity.id,source.role if source else None,literal(raw.raw_value))
                if identity in rendered_people:
                    continue
                rendered_people.add(identity)
            fact=display_fact(raw,entities,invalid,service.kind,source)
            service_facts.append(fact)
            record_source(source,fact.id,service.id)
            bullets.append(f'{fact.label}: {fact.raw_value}')
            if len(fact.raw_value)<=300: critical.append(fact.raw_value)
            fact_ids.append(fact.id); references+=ids(fact.evidence)
            if source:
                references.append(source.block_id)
        headings={'flight':'Dados do voo','hotel':'Reserva e hóspedes','car_rental':'Locadora e veículo','ticket':'Dados do ingresso'}
        sections=[Section(title=headings.get(service.kind,'Dados do serviço'),tone='neutral',bullets=bullets)] if bullets else []
        if service.kind=='flight' and sections:
            structured=flight_card(service_facts,identity_bullets)
            if structured:
                fields,sections[0].bullets=structured
                extracted['data']['structured_flights'][section_key(sections[0])]=fields
        for condition,section in condition_cards:
            if condition.service_id==service.id:
                sections.append(section); references+=ids(condition.evidence)
        if sections:
            pages.append(Page(title=SERVICE_TITLES[service.kind],eyebrow='Sua viagem',introduction='',
                sections=sections,code_id=None,critical_values=list(dict.fromkeys(critical)),
                source_block_ids=list(dict.fromkeys(references))))
            all_critical+=critical

    # Unscoped facts and general clauses still have a visible home.
    general=[]; references=[]; critical=[]
    for fact in document.facts:
        if fact.service_id is None and fact.status=='supported' and f'facts.{fact.id}' not in invalid:
            source=fact_source(fact,None)
            fact=display_fact(fact,entities,invalid,source=source)
            record_source(source,fact.id,None)
            general.append(f'{fact.label}: {fact.raw_value}'); references+=ids(fact.evidence)
            if len(fact.raw_value)<=300: critical.append(fact.raw_value)
            fact_ids.append(fact.id)
    sections=[Section(title='Dados da reserva',tone='neutral',bullets=general)] if general else []
    for condition,section in condition_cards:
        if condition.service_id is None:
            sections.append(section); references+=ids(condition.evidence)
    if sections:
        pages.append(Page(title='Informações e condições',eyebrow='Sua viagem',introduction='',
            sections=sections,code_id=None,critical_values=list(dict.fromkeys(critical)),
            source_block_ids=list(dict.fromkeys(references))))
        all_critical+=critical

    # Each physical code gets its own legible card. Presentation is independent
    # of the entity name; uncertain associations do not claim a holder.
    associations={a.code_id:a for a in document.code_associations}
    services={s.id:s for s in document.services}
    facts={}
    for fact in document.facts:
        service=services.get(fact.service_id)
        kind=service.kind if service else None
        facts[fact.id]=display_fact(fact,entities,invalid,kind,fact_source(fact,kind))
    for code in extracted['codes']:
        a=associations.get(code['id']); bullets=[]; critical=[]
        valid=a and f'codes.{code["id"]}' not in invalid
        references=[key for key,b in blocks.items() if b['page']==code['source_page']]
        if valid:
            code['purpose']=a.purpose
            for key in a.holder_entity_ids:
                entity=entities.get(key)
                if safe_name(entity):
                    page_prefix=f'p{code["source_page"]}-b'
                    page_evidence=[e for e in entity.evidence if e.block_id.startswith(page_prefix)]
                    source=field_label('person_name',entity.name,page_evidence,blocks)
                    label=source.label if source else ('Titular do ingresso' if a.purpose=='ticket' else 'Titular')
                    bullets.append(f'{label}: {entity.name}')
                    critical.append(entity.name)
                    record_source(source,key,None)
            for key in a.number_fact_ids:
                fact=facts[key]
                bullets.append(f'{fact.label}: {fact.raw_value}'); critical.append(fact.raw_value)
        pages.append(Page(title='Seu ingresso' if valid and a.purpose=='ticket' else 'Código do documento',
            eyebrow='Documento de viagem',introduction='',code_id=code['id'],
            sections=[Section(title='Identificação',tone='neutral',bullets=bullets)] if bullets else [],
            critical_values=list(dict.fromkeys(critical)),source_block_ids=references))
        all_critical+=critical

    rendered='\n'.join(page_text(p) for p in pages)
    residual=[]
    residual_groups={}
    omitted=[]
    for key,block in blocks.items():
        text=block['text']
        if (re.fullmatch(r'\s*(?:p[aá]g(?:ina)?\.?|page)\s*\d+\s*(?:/|de|of)\s*\d+\s*',text,re.I)
                and not any(key in p.source_block_ids for p in pages)):
            from .ai_images import Omission
            omitted.append(Omission(block_id=key,reason='source_footer',explanation='Somente paginação da fonte.'))
            continue
        # A cited block is not covered merely because its ID appeared in JSON.
        # Keep the complete original whenever its wording is not already visible.
        if literal(text) not in literal(rendered):
            residual.append(key)
            residual_groups.setdefault(block['page'],[]).append((key,text))
        elif not any(key in p.source_block_ids for p in pages):
            # Repeated text may be rendered once, while preserving evidence links.
            target=next(p for p in pages if literal(text) in literal(page_text(p)))
            target.source_block_ids.append(key)
    for group in residual_groups.values():
        pages.append(Page(title='Informações complementares',eyebrow='Sua viagem',introduction='',
            sections=[Section(title='Detalhes e condições',tone='neutral',bullets=[text for _,text in group])],
            code_id=None,critical_values=[],source_block_ids=[key for key,_ in group]))
    if not pages:
        from .ai_images import AIError
        raise AIError('NO_READABLE_CONTENT','Não há conteúdo legível para apresentar.')
    title=SERVICE_TITLES[document.services[0].kind] if len(document.services)==1 else 'Documentos da sua viagem'
    plan=Plan(title=title,pages=pages,omitted_blocks=omitted)
    coverage={'coverage_passed':True,'source_blocks':len(blocks),'residual_blocks_preserved':len(residual),
        'rendered_fact_ids':fact_ids,'source_field_labels':field_sources,
        'critical_values':list(dict.fromkeys(all_critical)),
        'preservation_policy':'literal_residuals','semantic_completeness':'requires_human_review'}
    return plan,coverage


def public_context(document,issues):
    """Derive only source-supported public entities, never a guest or reference."""
    invalid={i.get('field') for i in issues if i.get('blocking')}
    places=[]
    for service in document.services:
        if (service.kind not in {'hotel','ticket','flight','transfer','car_rental','cruise'}
                or f'services.{service.id}' in invalid): continue
        for entity in document.entities:
            if (entity.id not in service.entity_ids or entity.role not in {'property','location'}
                    or f'entities.{entity.id}' in invalid or not safe_public(entity.name)
                    or entity.locality and not safe_public(entity.locality)):
                continue
            kind='hotel' if service.kind=='hotel' and entity.role=='property' else (
                'attraction' if service.kind=='ticket' else 'destination')
            places.append({'kind':kind,'name':entity.name,'locality':entity.locality,
                'source_block_ids':[e.block_id for e in entity.evidence],'entity_id':entity.id})
    distinct=list({(p['kind'],literal(p['name'])):p for p in places}.values())
    primary=next((i for i,p in enumerate(distinct) if p['entity_id']==document.primary_place_id),None)
    focal=[i for i,p in enumerate(distinct) if p['kind'] in {'hotel','attraction'}]
    if primary is None and len(focal)==1:
        primary=focal[0]
    if not focal and any(s.kind in {'flight','car_rental'} for s in document.services):
        from .itinerary_media import itinerary_place
        route=itinerary_place(document,issues)
        primary=None
        if route:
            from .media_context import plain
            primary=next((i for i,p in enumerate(distinct)
                          if p['kind']=='destination' and plain(p['name'])==plain(route['name'])),None)
            if primary is None:
                primary=len(distinct)
                distinct.append(route)
    if len(distinct)>8:
        if primary is not None:
            distinct=[distinct[primary]]+[p for i,p in enumerate(distinct) if i!=primary][:7]
            primary=0
        else:
            distinct=distinct[:8]
    return {'places':[{k:v for k,v in p.items() if k!='entity_id'} for p in distinct],
            'primary_index':primary}
