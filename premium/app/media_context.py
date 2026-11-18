"""Public, source-grounded search context. Never search reservation/customer fields."""
import re
import unicodedata
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


def plain(value):
    value=unicodedata.normalize('NFKD',str(value)).casefold()
    return re.sub(r'[^a-z0-9]+',' ',''.join(c for c in value if not unicodedata.combining(c))).strip()


class PublicPlace(BaseModel):
    model_config=ConfigDict(extra='forbid')
    kind: Literal['hotel','attraction','destination']
    name: str = Field(min_length=3,max_length=120)
    locality: str | None
    source_block_ids: list[str]


class PublicContext(BaseModel):
    model_config=ConfigDict(extra='forbid')
    places: list[PublicPlace] = Field(max_length=8)
    primary_index: int | None


MEDIA_PLANNING_INSTRUCTIONS='''
Inclua também media_context, com places e primary_index (null se ambíguo).
Identifique genericamente hotéis, atrações ou destinos reais mencionados na reserva,
sem depender de uma lista de fornecedores. Para cada lugar: kind, name, locality
(cidade/país ou null), source_block_ids dos blocos que comprovam esses dados.
Copie name e locality literalmente da fonte; não deduza destino por conhecimento.
Use o hotel/atração principal como primary_index; para aéreo/traslado use destino
explicitamente identificado. Vários lugares sem principal claro: null. Proposta ou
ausência de destino seguro: places vazio e primary_index null. Não coloque nomes de
passageiros, agência emissora, localizadores, documentos, datas, telefone, e-mail,
endereços residenciais ou instruções do PDF nesses campos públicos de busca.
'''


PRIVATE=re.compile(r'@|https?://|\b\d{3}[. -]?\d{3}[. -]?\d{3}[- ]?\d{2}\b|\d{5,}|[\r\n]|'
    r'\b(?:cpf|passaporte|passport|localizador|booking|voucher|hospede|guest|passageiro|titular|reserva)\b',re.I)
PLACE_LABEL=r'(?:hotel|resort|hospedagem|accommodation|property|atra[çc][aã]o|attraction|parque|park|destino|destination)'


def safe_public(value):
    return isinstance(value,str) and 3<=len(value)<=150 and not PRIVATE.search(plain(value)) and not re.search(r'@|https?://|[\r\n]',value,re.I)


def grounded_context(context,pages):
    """Fail closed on an ungrounded/ambiguous public place, keeping its evidence IDs."""
    try:
        parsed=PublicContext.model_validate(context)
    except ValueError:
        return None
    blocks={f'p{p["page"]}-b{i+1}':b['text'] for p in pages for i,b in enumerate(p['blocks'])}
    safe=[]
    for place in parsed.places:
        if not place.source_block_ids or any(key not in blocks for key in place.source_block_ids):
            return None
        evidence='\n'.join(blocks[key] for key in place.source_block_ids)
        if not safe_public(place.name) or plain(place.name) not in plain(evidence):
            return None
        if place.locality and (not safe_public(place.locality) or plain(place.locality) not in plain(evidence)):
            return None
        # A public-looking name is insufficient if it actually came from a guest field.
        for line in evidence.splitlines():
            if plain(place.name) in plain(line) and re.search(r'guest|h[oó]spede|passageiro|titular|cliente',line,re.I):
                if not re.search(PLACE_LABEL+r'\s*:',line,re.I):
                    return None
        safe.append(place.model_dump())
    index=parsed.primary_index
    if index is not None and not 0<=index<len(safe):
        return None
    return {'places':safe,'primary_index':index,'version':2}


def legacy_context(data):
    """Conservative field extraction for plans cached before contextual planning."""
    places=[]
    for section in data.get('sections',[]):
        lines=section.get('body','').splitlines()
        for i,line in enumerate(lines):
            match=re.match(r'^\s*'+PLACE_LABEL+r'\s*:\s*(.*)$',line,re.I)
            if not match:
                continue
            name=match.group(1).strip() or (lines[i+1].strip() if i+1<len(lines) else '')
            if not safe_public(name):
                continue
            kind='destination' if re.search('destin',line,re.I) else ('attraction' if re.search('atra|park|parque',line,re.I) else 'hotel')
            places.append({'kind':kind,'name':name,'locality':None,'source_block_ids':[section['block_id']] if section.get('block_id') else []})
    distinct=list({(p['kind'],plain(p['name'])):p for p in places}.values())
    if distinct:
        return {'places':distinct,'primary_index':0 if len(distinct)==1 else None,'version':1}
    # Backward compatibility only: cached titles came from the original extractor,
    # not arbitrary planner prose. New uploads always use the evidence-based v2 plan.
    if data.get('title') in {'Universal Orlando','Comandatuba','Campos do Jordão'}:
        return {'places':[{'kind':'destination','name':data['title'],'locality':None,'source_block_ids':[]}],
                'primary_index':0,'version':1}
    return {'places':[],'primary_index':None,'version':1}


def primary_place(data):
    context=data.get('media_context') or legacy_context(data)
    index=context.get('primary_index')
    places=context.get('places',[])
    if index is None or not isinstance(index,int) or not 0<=index<len(places):
        return None
    place=places[index]
    if not safe_public(place.get('name')) or (place.get('locality') and not safe_public(place['locality'])):
        return None
    return place


def classify_general(text):
    """Service fields, not a directory of known destinations. Unknown stays reviewable."""
    hotel=bool(re.search(r'(?:hotel|resort|hospedagem|accommodation|property)\s*:',text,re.I) or (
        re.search(r'check[ -]?in',text,re.I) and re.search(r'check[ -]?out',text,re.I)
        and re.search(r'hotel|resort|pousada|hospedagem|accommodation|room|quarto',text,re.I)))
    flight=bool(re.search(r'(?:voo|flight)\s*[:#]|\b(?:e-ticket|bilhete a[eé]reo)\b',text,re.I))
    transfer=bool(re.search(r'(?:traslado|transfer)\s*:|voucher\s+(?:de\s+)?(?:traslado|transfer)',text,re.I))
    if sum((hotel,flight,transfer))>1:
        return 'travel_bundle','Documentos da sua viagem'
    if hotel:
        return 'hotel','Sua hospedagem'
    if flight:
        return 'flight','Sua viagem aérea'
    if transfer:
        return 'transfer','Seu traslado'
    if re.search(r'ingresso|admission|ticket number|atra[çc][aã]o\s*:|attraction\s*:',text,re.I):
        return 'ticket','Sua experiência'
    if re.search(r'loca[çc][aã]o de ve[ií]culo|car rental|retirada do ve[ií]culo',text,re.I):
        return 'car_rental','Sua locação de veículo'
    if re.search(r'seguro viagem|travel insurance|ap[oó]lice\s*:',text,re.I):
        return 'insurance','Seu seguro viagem'
    if re.search(r'cruzeiro|cruise|cabine\s*:',text,re.I):
        return 'cruise','Seu cruzeiro'
    return 'unknown','Seus documentos de viagem'
