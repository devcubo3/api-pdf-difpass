"""Transport photo subjects from synthetic facts; no providers or customer PDFs."""
from copy import deepcopy

import pytest

from app.document_schema import Understanding, Entity, Service, Fact, Evidence
from app.document_validation import validate_understanding
from app.media_context import grounded_context, primary_place
from app.presentation import public_context


def flights(routes):
    blocks=[]
    document=Understanding(schema_version='canonical_v1', entities=[], services=[], facts=[],
                           conditions=[], code_associations=[], unreadable_code_pages=[], primary_place_id=None)
    for i,(origin,destination) in enumerate(routes,1):
        text=f'Flight TEST{i}. From {origin}. To {destination}.'
        blocks.append({'text':text,'bbox':[0,i*30,500,i*30+20]})
        ev=Evidence(block_id=f'p1-b{i}',quote=text)
        sid=f's{i}'
        document.services.append(Service(id=sid,kind='flight',name=None,entity_ids=[],evidence=[ev]))
        for kind,value in (('flight_number',f'TEST{i}'),('origin_city',origin),('destination_city',destination)):
            document.facts.append(Fact(id=f'{kind}_{i}',service_id=sid,entity_id=None,kind=kind,
                label='Campo',raw_value=value,status='supported',evidence=[ev]))
    return document,{'pages':[{'page':1,'blocks':blocks}],'codes':[]}


def selected(document,extracted,extra_issues=()):
    _,issues=validate_understanding(document,extracted)
    context=public_context(document,issues+list(extra_issues))
    grounded=grounded_context(context,extracted['pages'])
    return primary_place({'media_context':grounded}) if grounded else None


def test_first_listed_arrival_has_a_photo_context_without_location_entities():
    document,extracted=flights([('Alta Vista','Costa Clara'),('Costa Clara','Alta Vista')])
    place=selected(document,extracted)
    assert place['name']=='Costa Clara' and place['kind']=='destination'
    assert place['source_block_ids']==['p1-b1']
    document.services.reverse()
    document.facts.reverse()
    assert selected(document,extracted)==place  # Source order, not model list order.


@pytest.mark.parametrize('status', ['ambiguous','unreadable','absent'])
def test_unknown_first_arrival_never_silently_selects_the_return_home(status):
    document,extracted=flights([('Alta Vista','Costa Clara'),('Costa Clara','Alta Vista')])
    document.facts[2].status=status
    assert selected(document,extracted) is None


def test_contested_or_conflicting_first_arrival_requires_review():
    document,extracted=flights([('Alta Vista','Costa Clara'),('Costa Clara','Alta Vista')])
    assert selected(document,extracted,[{'field':'facts.destination_city_1','blocking':True}]) is None
    assert selected(document,extracted,[{'field':'services.s1','blocking':True}]) is None
    other=document.facts[2].model_copy(deep=True)
    other.id='other';other.raw_value='Alta Vista'
    document.facts.append(other)
    assert selected(document,extracted) is None


def test_same_source_position_with_competing_arrivals_is_ambiguous():
    document,extracted=flights([('Alta Vista','Costa Clara'),('Costa Clara','Alta Vista')])
    document.services[1].evidence=deepcopy(document.services[0].evidence)
    assert selected(document,extracted) is None


def rental():
    text=['Vehicle rental: sedan.','Pickup: BRANCH-A*ALTA VISTA AIRPORT.',
          'Dropoff: BRANCH-B*COSTA CLARA AIRPORT.']
    ev=[Evidence(block_id=f'p1-b{i}',quote=t) for i,t in enumerate(text,1)]
    entities=[Entity(id='pickup_place',role='location',name='ALTA VISTA AIRPORT',locality='ALTA VISTA',evidence=[ev[1]]),
              Entity(id='dropoff_place',role='location',name='COSTA CLARA AIRPORT',locality='COSTA CLARA',evidence=[ev[2]])]
    doc=Understanding(schema_version='canonical_v1',entities=entities,
        services=[Service(id='rental',kind='car_rental',name=None,
                          entity_ids=['pickup_place','dropoff_place'],evidence=[ev[0]])],
        facts=[Fact(id='pickup',kind='pickup',service_id='rental',entity_id='pickup_place',
                    raw_value='ALTA VISTA AIRPORT',label='Retirada',status='supported',evidence=[ev[1]]),
               Fact(id='dropoff',kind='dropoff',service_id='rental',entity_id='dropoff_place',
                    raw_value='COSTA CLARA AIRPORT',label='Devolução',status='supported',evidence=[ev[2]])],
        conditions=[],code_associations=[],unreadable_code_pages=[],primary_place_id=None)
    return doc,{'pages':[{'page':1,'blocks':[{'text':t,'bbox':[0,0,500,30]} for t in text]}],'codes':[]}


def test_rental_uses_grounded_pickup_city_not_dropoff_or_branch_code():
    document,extracted=rental()
    place=selected(document,extracted)
    assert place['name']=='ALTA VISTA' and place['locality'] is None
    assert place['source_block_ids']==['p1-b2']
    document.primary_place_id='dropoff_place'
    assert selected(document,extracted)==place
    document.facts[0].raw_value='31 out. 19:00'  # Pickup time is not a place.
    document.facts[0].entity_id=None
    assert selected(document,extracted) is None


def test_rental_never_uses_a_person_or_an_unsupported_locality():
    document,extracted=rental()
    document.entities[0].role='person'
    assert selected(document,extracted) is None
    document.entities[0].role='location'
    document.entities[0].locality='LUGAR INVENTADO'
    assert selected(document,extracted) is None


@pytest.mark.parametrize('value',['Guest EXAMPLE','ABC123','123.456.789-00','person@example.test'])
def test_route_city_does_not_leak_private_or_reference_values(value):
    document,extracted=flights([('Alta Vista',value)])
    assert selected(document,extracted) is None


def test_person_name_is_not_used_as_an_illustrative_city():
    document,extracted=flights([('Alta Vista','Costa Clara')])
    document.entities.append(Entity(id='person',role='person',name='Costa Clara',
                                    locality=None,evidence=document.services[0].evidence))
    assert selected(document,extracted) is None


def test_a_unique_hotel_keeps_priority_in_a_document_with_flights():
    document,extracted=flights([('Alta Vista','Costa Clara')])
    text='Hotel: Estalagem Exemplo'
    extracted['pages'][0]['blocks'].append({'text':text,'bbox':[0,50,500,75]})
    ev=Evidence(block_id='p1-b2',quote=text)
    document.entities.append(Entity(id='hotel',role='property',name='Estalagem Exemplo',locality=None,evidence=[ev]))
    document.services.append(Service(id='stay',kind='hotel',name=None,entity_ids=['hotel'],evidence=[ev]))
    assert selected(document,extracted)['name']=='Estalagem Exemplo'
