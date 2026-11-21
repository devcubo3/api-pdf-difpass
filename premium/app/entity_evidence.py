"""Conservatively supplement public names with literal, service-scoped evidence.

Never change values, repair people, replace invalid quotations or infer localities.
The paid understanding snapshot and the extracted source remain immutable.
"""
from .document_schema import Evidence
from .document_validation import contains, literal, source_blocks, validate_understanding


def repair_public_entity_evidence(document, extracted):
    blocks = source_blocks(extracted)
    _, issues = validate_understanding(document, extracted)
    invalid = {item.get('field') for item in issues if item.get('blocking')}
    condition_blocks = {e.block_id for condition in document.conditions for e in condition.evidence}
    services = {service.id: service for service in document.services}
    codes = {code['id']: code for code in extracted.get('codes', [])}
    page_services = {}

    def valid(evidence):
        return bool(evidence) and all(e.block_id in blocks and e.quote.strip()
            and contains(e.quote, blocks[e.block_id]['text']) for e in evidence)

    def grounded(value, evidence):
        return valid(evidence) and any(contains(value, e.quote)
            and contains(value, blocks[e.block_id]['text']) for e in evidence)

    def own(page, service_id):
        page_services.setdefault(page, set()).add(service_id)

    for service in document.services:
        if f'services.{service.id}' not in invalid and valid(service.evidence):
            for item in service.evidence:
                own(blocks[item.block_id]['page'], service.id)
    for association in document.code_associations:
        if (f'codes.{association.code_id}' not in invalid and association.code_id in codes
                and valid(association.evidence)):
            for service_id in association.service_ids:
                if service_id in services and f'services.{service_id}' not in invalid:
                    own(codes[association.code_id]['source_page'], service_id)

    repairs = []
    for entity in document.entities:
        if (entity.role not in {'property', 'location', 'provider'}
                or not valid(entity.evidence) or grounded(entity.name, entity.evidence)
                or entity.locality and not grounded(entity.locality, entity.evidence)):
            continue
        related = [service for service in document.services if entity.id in service.entity_ids]
        if len(related) != 1 or f'services.{related[0].id}' in invalid:
            continue
        service = related[0]
        # Multiple different entities in this role require human disambiguation.
        names = {literal(other.name).casefold() for other in document.entities
                 if other.role == entity.role and other.id in service.entity_ids}
        if len(names) != 1:
            continue
        exclusive_pages = {page for page, owners in page_services.items() if owners == {service.id}}
        if not all(blocks[e.block_id]['page'] in exclusive_pages for e in entity.evidence):
            continue
        candidates = []
        for other in document.entities:
            if (other.id == entity.id or other.id not in service.entity_ids
                    or other.role not in {'property', 'location', 'provider'}
                    or f'entities.{other.id}' in invalid
                    or literal(other.name).casefold() != literal(entity.name).casefold()
                    or not grounded(other.name, other.evidence)):
                continue
            candidates.extend(e for e in other.evidence
                if blocks[e.block_id]['page'] in exclusive_pages and contains(entity.name, e.quote)
                and e.block_id not in condition_blocks)
        # A standalone name on a uniquely associated service page is also useful;
        # mere occurrences inside conditions or unrelated pages are insufficient.
        candidates.extend(Evidence(block_id=key, quote=block['text'])
            for key, block in blocks.items() if block['page'] in exclusive_pages
            and key not in condition_blocks
            and literal(block['text']).casefold() == literal(entity.name).casefold())
        if not candidates:
            continue
        chosen = min(candidates, key=lambda e: (len(literal(e.quote)), blocks[e.block_id]['page'], e.block_id))
        before = [e.block_id for e in entity.evidence]
        entity.evidence = [*entity.evidence, chosen.model_copy(deep=True)]
        repairs.append({'entity_id': entity.id, 'field': 'name', 'from_block_ids': before,
                        'added_block_id': chosen.block_id, 'policy': 'public_entity_evidence_v1'})
    return repairs
