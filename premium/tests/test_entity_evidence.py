"""Synthetic evidence repairs must never loosen the existing validation."""
from copy import deepcopy

import pytest

from app.document_schema import Understanding, Entity, Evidence, Service, Fact, CodeAssociation, Condition
from app.document_validation import validate_understanding
from app.entity_evidence import repair_public_entity_evidence


def case():
    source = {'pages': [
        {'page': 1, 'blocks': [{'text': 'Admission ticket 0001'},
            {'text': 'Alpine Waters is located in Mountain City.'}]},
        {'page': 2, 'blocks': [{'text': 'Alpine Waters Resort'}]}],
        'codes': [{'id': 'code', 'source_page': 2, 'payloads': ['0001']}]}
    ticket = Evidence(block_id='p1-b1', quote='Admission ticket 0001')
    address = Evidence(block_id='p1-b2', quote='Alpine Waters is located in Mountain City.')
    document = Understanding(
        entities=[Entity(id='place', role='property', name='Alpine Waters Resort',
                         locality='Mountain City', evidence=[address])],
        services=[Service(id='admission', kind='ticket', name=None,
                          entity_ids=['place'], evidence=[ticket])],
        facts=[Fact(id='number', service_id='admission', entity_id=None, kind='ticket_number',
                    label='Ingresso', raw_value='0001', status='supported', evidence=[ticket])],
        code_associations=[CodeAssociation(code_id='code', purpose='booking', service_ids=['admission'],
                            holder_entity_ids=[], number_fact_ids=[], evidence=[ticket])])
    return document, source


def test_supplements_name_in_same_service_without_changing_values_or_source():
    document, source = case()
    original_source = deepcopy(source)
    original = document.model_dump()
    assert validate_understanding(document, source)[1][0]['code'] == 'ENTITY_EVIDENCE_REVIEW'
    repairs = repair_public_entity_evidence(document, source)
    assert repairs[0]['added_block_id'] == 'p2-b1'
    assert validate_understanding(document, source)[0]['evidence_validation_passed']
    assert source == original_source
    updated = document.model_dump()
    updated['entities'][0]['evidence'] = original['entities'][0]['evidence']
    assert updated == original
    assert repair_public_entity_evidence(document, source) == []


@pytest.mark.parametrize('change', ['invented', 'person', 'invalid_quote', 'locality',
                                   'conditions', 'unrelated_page', 'shared_page', 'ambiguous_role'])
def test_ambiguous_or_unverified_context_remains_blocked(change):
    document, source = case()
    entity = document.entities[0]
    if change == 'invented':
        entity.name = 'Invented Resort'
    elif change == 'person':
        entity.role = 'person'
    elif change == 'invalid_quote':
        entity.evidence[0].quote = 'Invented quotation'
    elif change == 'locality':
        entity.locality = 'Another City'
    elif change == 'conditions':
        source['pages'][1]['blocks'][0]['text'] = 'Access to Alpine Waters Resort is not included.'
    elif change == 'unrelated_page':
        document.code_associations = []
        source['codes'] = []
    elif change == 'shared_page':
        document.services.append(Service(id='second', kind='other', name=None, entity_ids=[],
            evidence=[Evidence(block_id='p2-b1', quote='Alpine Waters Resort')]))
    elif change == 'ambiguous_role':
        document.entities.append(Entity(id='second-place', role='property', name='Mountain City',
            evidence=[entity.evidence[0].model_copy(deep=True)]))
        document.services[0].entity_ids.append('second-place')
    before = document.model_dump()
    assert repair_public_entity_evidence(document, source) == []
    assert document.model_dump() == before
    assert any(item.get('field') == 'entities.place' for item in validate_understanding(document, source)[1])


def test_existing_grounded_entity_can_supply_literal_name_in_service_context():
    document, source = case()
    source['pages'][1]['blocks'][0]['text'] = 'Provider: Alpine Waters Resort'
    document.entities.append(Entity(id='provider', role='provider', name='Alpine Waters Resort',
        evidence=[Evidence(block_id='p2-b1', quote='Provider: Alpine Waters Resort')]))
    document.services[0].entity_ids.append('provider')
    assert repair_public_entity_evidence(document, source)
    assert validate_understanding(document, source)[0]['evidence_validation_passed']


def test_condition_occurrence_cannot_supply_a_property_identity():
    document, source = case()
    source['pages'][1]['blocks'][0]['text'] = 'Access to Alpine Waters Resort is not included.'
    clause = Evidence(block_id='p2-b1', quote=source['pages'][1]['blocks'][0]['text'])
    document.entities.append(Entity(id='provider', role='provider', name='Alpine Waters Resort',
        evidence=[clause]))
    document.services[0].entity_ids.append('provider')
    document.conditions.append(Condition(id='restriction',service_id='admission',kind='access',evidence=[clause]))
    assert repair_public_entity_evidence(document, source) == []
