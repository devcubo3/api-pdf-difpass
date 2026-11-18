"""Restore explicitly labelled hotel facts from evidence, without editing paid caches."""
import re

from .ai_images import AIError, Section, _normalized, page_text

HOTEL_CONTACT=re.compile(r'Contato\s+(?:do\s+)?Hotel\s*:',re.I)
CENTRAL=re.compile(r'Contato\s+(?:da\s+)?Central\s+(?:de\s+)?Reservas\s*:',re.I)
EMAIL=re.compile(r'[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}',re.I)
PHONE=re.compile(r'\(\d{2}\)\s*\d{4,5}[-\s]?\d{4}|0800\s*\d{3}\s*\d{4}')


def preserve_hotel_data(plan,extracted):
    result=plan.model_copy(deep=True)
    adjustments=[]
    for source in extracted['pages']:
        for index,block in enumerate(source['blocks'],1):
            text=re.sub(r'\s+',' ',block['text']).strip()
            contact=HOTEL_CONTACT.search(text)
            if not contact:
                continue
            if len(HOTEL_CONTACT.findall(text))!=1:
                raise AIError('HOTEL_DATA_REVIEW','Múltiplos hotéis/contatos no mesmo bloco exigem associação explícita.')
            identifier=f'p{source["page"]}-b{index}'
            # Anchor the exact hotel identity/address before its explicit contact label.
            header=re.split(r'Pol[ií]ticas Gerais\s*:',text[:contact.start()],flags=re.I)[-1].strip()
            candidates=[p for p in result.pages if _normalized(p.title) in _normalized(header)]
            if not candidates:
                candidates=[p for p in result.pages if identifier in p.source_block_ids
                            and 'hospedagem' in _normalized(p.eyebrow)]
            if not candidates:
                raise AIError('HOTEL_DATA_REVIEW','Não foi possível associar o contato ao hotel do documento.')
            page=candidates[0]
            details=re.split(r'Sua reserva inclui\s*:',text[contact.start():],maxsplit=1,flags=re.I)[0].strip()
            central=CENTRAL.search(details)
            if len(details)>800:
                raise AIError('HOTEL_DATA_REVIEW','Os contatos do hotel exigem separação e revisão.')
            hotel_phone=details[HOTEL_CONTACT.search(details).end():central.start() if central else len(details)].strip()
            central_data=details[central.end():].strip() if central else ''
            central_emails=EMAIL.findall(central_data)
            hotel_emails=EMAIL.findall(hotel_phone)
            emails=central_emails+hotel_emails
            hotel_phone=EMAIL.sub('',hotel_phone).strip(' ;,')
            phones=PHONE.findall(details)
            if not phones:
                raise AIError('HOTEL_DATA_REVIEW','Contato do hotel sem telefone verificável.')
            central_phone=EMAIL.sub('',central_data).strip(' ;,')
            bullets=['Contato do hotel: '+hotel_phone]
            cep=re.search(r'\bCEP\s*:\s*(\d{5}-?\d{3})',header,re.I)
            if cep and len(header)<=600:
                bullets.insert(0,'Identificação e endereço: '+header)
            elif cep:
                raise AIError('HOTEL_DATA_REVIEW','Endereço do hotel exige separação e revisão.')
            if central_phone:
                bullets.append('Central de reservas: '+central_phone)
            bullets+=['E-mail de reservas: '+email for email in central_emails]
            bullets+=['E-mail do hotel: '+email for email in hotel_emails]
            for bullet in bullets:
                if _normalized(bullet.split(': ',1)[1]) not in _normalized(page_text(page)):
                    existing=next((s for s in page.sections if s.title=='Dados e contatos do hotel'),None)
                    if existing is None:
                        existing=Section(title='Dados e contatos do hotel',tone='neutral',bullets=[])
                        page.sections.append(existing)
                    existing.bullets.append(bullet)
            if identifier not in page.source_block_ids:
                page.source_block_ids.append(identifier)
            for fact in [*phones,*emails,*([cep[1]] if cep else [])]:
                if fact not in page.critical_values:
                    page.critical_values.append(fact)
            adjustments.append({'kind':'hotel_source_data_preserved','page':result.pages.index(page)+1,
                                'source_block_ids':[identifier],'contact_facts':len(phones)+len(emails),'address':bool(cep)})
            # Preserve full original minor-document clauses when these appear in hotel policies.
            clauses=re.split(r'\s+-\s+',text)
            anchors=[r'^Menores de 18',r'^Se o menor estiver acompanhado',r'^Caso o menor n[aã]o esteja acompanhado']
            rules=[part.strip() for part in clauses if any(re.search(a,part,re.I) for a in anchors)]
            sections=[(p,s) for p in result.pages if identifier in p.source_block_ids
                      for s in p.sections if 'menores' in _normalized(s.title) and 'hospedagem' in _normalized(p.eyebrow)]
            if rules and len(sections)==1:
                target,section=sections[0]
                categories=[r'^(?:Menores de 18)',r'^(?:Se o menor estiver acompanhado|Com apenas um dos pais)',
                            r'^(?:Caso o menor n[aã]o esteja acompanhado|Sem os pais ou respons[aá]vel)']
                replacement={i:next((r for r in rules if re.search(anchor,r,re.I)),None)
                             for i,anchor in enumerate(anchors)}
                used=set()
                rebuilt=[]
                for bullet in section.bullets:
                    category=next((i for i,pattern in enumerate(categories) if re.search(pattern,bullet,re.I)),None)
                    if category is not None and replacement[category]:
                        if category not in used:
                            rebuilt.append(replacement[category]);used.add(category)
                    else:
                        rebuilt.append(bullet)
                rebuilt+=[value for key,value in replacement.items() if value and key not in used]
                section.bullets=rebuilt
                # Keep the source's exact numeric presentation, e.g. "18 (dezoito) anos".
                # The age stays critical; only its displayed representation changes.
                for rule in rules:
                    for match in re.finditer(r'\b(\d+)\s+\([^)]{1,30}\)\s+anos\b',rule,re.I):
                        target.critical_values=[match[0] if value==match[1]+' anos' else value
                                                for value in target.critical_values]
                adjustments.append({'kind':'hotel_minor_clauses_preserved','page':result.pages.index(target)+1,
                                    'source_block_ids':[identifier]})
    return result,adjustments
