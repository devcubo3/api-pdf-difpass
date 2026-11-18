"""Keep role, date and reference labels tied to evidence in the composed cards."""
from app.document_schema import Entity, Evidence, Fact, Service, Understanding
from app.document_validation import validate_understanding
from app.presentation import compile_presentation


def compose(lines, claims, *, entity_block=1, service_kind='ticket'):
    def evidence(index):
        return Evidence(block_id=f'p1-b{index}', quote=lines[index-1])

    extracted={'data':{},'pages':[{'page':1,'blocks':[
        {'text':text,'bbox':[0,i*30,490,i*30+20]} for i,text in enumerate(lines)]}],'codes':[]}
    document=Understanding(
        entities=[Entity(id='person',role='person',name='Pessoa Exemplo',evidence=[evidence(entity_block)])],
        services=[Service(id='service',kind=service_kind,name=None,entity_ids=['person'],evidence=[evidence(1)])],
        facts=[Fact(id=f'f{i}',kind=kind,raw_value=value,label='Rótulo genérico',status='supported',
            service_id='service',entity_id=subject,evidence=[evidence(block)])
            for i,(kind,value,subject,block) in enumerate(claims)],
    )
    _,issues=validate_understanding(document,extracted)
    plan,coverage=compile_presentation(document,extracted,issues)
    return plan.pages[0].sections[0].bullets, coverage, issues


def test_ticket_keeps_source_roles_date_format_distinct_references_and_counts():
    labels,coverage,issues=compose([
        'Passageiro titular / Lead Guest Name: Pessoa Exemplo',
        'Data do ingresso / Arrival date: 11/09/2026 (mês/dia/ano)',
        'Nro. da reserva: EXAMPLE1',
        'Park Reservation Number: EXAMPLE2',
        'Passageiros / Travelers:\n | \n2 Adultos (10+ anos)\nCrianças: 1 (3 - 9 anos)',
    ],[
        ('holder_name','Pessoa Exemplo','person',1),
        ('passenger_name','Pessoa Exemplo','person',1),
        ('arrival_date','11/09/2026',None,2),
        ('validity','11/09/2026',None,2),
        ('booking_reference','EXAMPLE1',None,3),
        ('booking_reference','EXAMPLE2',None,4),
        ('traveler_count','2 Adultos (10+ anos) | Crianças: 1 (3 - 9 anos)',None,5),
    ])
    assert not issues
    assert labels.count('Passageiro titular: Pessoa Exemplo')==1
    assert 'Participante: Pessoa Exemplo' not in labels
    assert 'Data do ingresso: 11/09/2026 (mês/dia/ano)' in labels
    assert 'Número da reserva: EXAMPLE1' in labels
    assert 'Número de reserva Park: EXAMPLE2' in labels
    assert 'Passageiros: 2 Adultos (10+ anos) | Crianças: 1 (3 - 9 anos)' in labels
    assert any(x['source_label']=='Passageiro titular / Lead Guest Name'
               and x['source_block_id']=='p1-b1' for x in coverage['source_field_labels'])


def test_different_grounded_roles_for_same_person_are_kept():
    labels,_,issues=compose([
        'Passageiro titular: Pessoa Exemplo',
        'Titular do ingresso: Pessoa Exemplo',
        'Número do ingresso: ABC123',
    ],[
        ('holder_name','Pessoa Exemplo','person',1),
        ('holder_name','Pessoa Exemplo','person',2),
        ('ticket_number','ABC123','person',3),
    ])
    assert not issues
    assert 'Passageiro titular: Pessoa Exemplo' in labels
    assert 'Titular do ingresso: Pessoa Exemplo' in labels


def test_another_person_label_and_changed_quantities_are_not_transferred():
    labels,_,issues=compose([
        'Passageiro titular: Outra Pessoa\nPessoa Exemplo',
        'Validade: 11/09/2026',
        'Passageiros / Travelers:\n2 Adultos | Crianças: 1',
    ],[
        ('holder_name','Pessoa Exemplo','person',1),
        ('validity','11/09/2026',None,2),
        ('traveler_count','20 Adultos | Crianças: 1',None,3),
    ])
    assert 'Nome: Pessoa Exemplo' in labels
    assert not any(line.startswith('Passageiro titular:') for line in labels)
    assert any(i.get('field')=='facts.f2' for i in issues)


def test_driver_and_plain_passenger_roles_follow_different_source_layouts():
    labels,_,issues=compose(['Condutor\nDocumento\nIdade','Pessoa Exemplo\nABC123\n35'],
        [('driver_name','Pessoa Exemplo','person',2),('pickup','ABC123',None,2)],
        entity_block=2,service_kind='car_rental')
    assert not issues and 'Condutor: Pessoa Exemplo' in labels
    labels,_,issues=compose(['Nome do Passageiro\nPessoa Exemplo','Voo AB123'],
        [('flight_number','AB123',None,2)],service_kind='flight')
    assert not issues and 'Passageiro: Pessoa Exemplo' in labels
