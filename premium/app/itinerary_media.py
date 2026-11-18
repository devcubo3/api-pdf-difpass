"""Source-grounded photo subjects for transport, without guessing a final destination."""
import re

from .media_context import plain, safe_public

PHOTO_CONTEXT_POLICY = 'service_roles_v1'


def itinerary_place(document, issues):
    """Use first listed arrival / pickup as an illustrative place, never trip purpose.

    Source position, not arbitrary JSON order or provider names, orders services.
    A missing/contested first place never selects a later return-home arrival.
    """
    invalid={i.get('field') for i in issues if i.get('blocking')}
    entities={e.id:e for e in document.entities}
    private_names={plain(e.name) for e in document.entities if e.role not in {'property','location'}}
    services=[s for s in document.services if s.kind in {'flight','car_rental'}]

    def position(service):
        positions=[(int(m[1]),int(m[2])) for e in service.evidence
                   if (m:=re.fullmatch(r'p(\d+)-b(\d+)',e.block_id))]
        return min(positions) if positions else None

    def city(value):
        value=' '.join(value.split())
        # City/locality fields are public names, not branch IDs, IATA lookups,
        # personal addresses, URLs or reservation references.
        if (not safe_public(value) or plain(value) in private_names
                or re.search(r'[\d@:/\\*#;\[\]{}<>|]',value)):
            return None
        return value

    def candidates(service):
        result=[]
        for fact in document.facts:
            if (fact.service_id!=service.id or fact.status!='supported'
                    or f'facts.{fact.id}' in invalid):
                continue
            direct=(service.kind=='flight' and fact.kind=='destination_city'
                    or service.kind=='car_rental' and fact.kind=='pickup_city')
            pickup=(service.kind=='car_rental' and fact.kind in {'pickup','pickup_location'})
            entity=entities.get(fact.entity_id)
            if fact.entity_id and (not entity or entity.role!='location'
                    or entity.id not in service.entity_ids or f'entities.{entity.id}' in invalid):
                continue
            evidence=list(fact.evidence)
            value=fact.raw_value if direct else None
            if pickup and entity and entity.locality:
                value=entity.locality
                evidence+=entity.evidence
            name=city(value) if value else None
            if name:
                result.append({'kind':'destination','name':name,'locality':None,
                               'source_block_ids':list(dict.fromkeys(e.block_id for e in evidence))})
        return result

    selected=[]
    for kind in ('flight','car_rental'):
        family=[s for s in services if s.kind==kind]
        if not family:
            continue
        positions=[position(s) for s in family]
        if any(p is None for p in positions):
            return None
        first=min(positions)
        first_services=[s for s in family if position(s)==first]
        places=[]
        for service in first_services:
            if f'services.{service.id}' in invalid:
                return None
            found=candidates(service)
            if not found:
                return None
            places+=found
        if len({plain(p['name']) for p in places})!=1:
            return None
        chosen={**places[0],'source_block_ids':list(dict.fromkeys(
            key for p in places for key in p['source_block_ids']))}
        selected.append(chosen)
    # A mixed document can support one common city; competing cities need review.
    if not selected or len({plain(p['name']) for p in selected})!=1:
        return None
    return {**selected[0],'source_block_ids':list(dict.fromkeys(
        key for p in selected for key in p['source_block_ids']))}
