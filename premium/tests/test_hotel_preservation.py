import pytest

from app.ai_images import AIError,Page,Plan,Section,page_text
from app.hotel_preservation import preserve_hotel_data


SOURCE=('Políticas Gerais: HOTEL EXEMPLO Rua Alfa, 10 - Cidade - CEP: 12345-678 '
    'Contato Hotel: (12) 3456-7890 Contato Central Reservas: (11) 4321-8765 / 0800 111 2222 '
    'reservas@hotel.example Sua reserva inclui: - Internet Wi-Fi. '
    '- Menores de 18 (dezoito) anos: documento original (certidão de nascimento / RG / Passaporte). '
    '- Se o menor estiver acompanhado por apenas um dos pais: autorização assinada pelo pai ausente '
    'e cópia autenticada do documento de identidade do mesmo. '
    '- Caso o menor não esteja acompanhado dos pais: autorização assinada por ambos e cópias dos documentos dos pais.')


def fixture():
    return (Plan(title='Viagem',omitted_blocks=[],pages=[Page(title='Hotel Exemplo',eyebrow='Hospedagem',
        introduction='Reserva de teste.',sections=[Section(title='Regras para menores',tone='attention',
            bullets=['Menores de 18 anos: documento original.','Com apenas um dos pais: autorização.',
                     'Sem os pais ou responsável legal: autorização.','Outro serviço permanece disponível.'])],
        code_id=None,critical_values=['18 anos'],source_block_ids=['p1-b1'])]),
        {'pages':[{'page':1,'blocks':[{'text':SOURCE}]}]})


def test_hotel_restoration_preserves_identity_contacts_and_full_clauses_without_mutating_cache():
    plan,extracted=fixture()
    before=plan.model_dump_json()
    fixed,adjustments=preserve_hotel_data(plan,extracted)
    text=page_text(fixed.pages[0])
    for fact in ['12345-678','(12) 3456-7890','(11) 4321-8765','0800 111 2222','reservas@hotel.example',
                 'certidão de nascimento / RG / Passaporte','assinada pelo pai ausente',
                 'documento de identidade do mesmo','documentos dos pais','Outro serviço permanece disponível.']:
        assert fact in text
    assert len(fixed.pages[0].critical_values)==6
    assert '18 (dezoito) anos' in fixed.pages[0].critical_values
    assert plan.model_dump_json()==before
    assert len(adjustments)==2
    again,_=preserve_hotel_data(fixed,extracted)
    assert again==fixed


def test_hotel_contacts_without_cep_or_central_are_not_lost_or_given_an_invented_address():
    plan,extracted=fixture()
    extracted['pages'][0]['blocks'][0]['text']='Hotel Exemplo Contato Hotel: (12) 3456-7890 ajuda@hotel.example'
    fixed,_=preserve_hotel_data(plan,extracted)
    text=page_text(fixed.pages[0])
    assert '(12) 3456-7890' in text and 'ajuda@hotel.example' in text
    assert 'Identificação e endereço:' not in text


def test_multiple_hotels_in_one_block_require_review_instead_of_misassigning_contacts():
    plan,extracted=fixture()
    extracted['pages'][0]['blocks'][0]['text']+=' Hotel Outro Contato Hotel: (21) 5555-0000'
    with pytest.raises(AIError,match='Múltiplos'):
        preserve_hotel_data(plan,extracted)


def test_unknown_hotel_identity_is_not_assigned_to_an_unrelated_page():
    plan,extracted=fixture()
    plan.pages[0].title='Aéreo'
    plan.pages[0].eyebrow='Voos'
    with pytest.raises(AIError) as error:
        preserve_hotel_data(plan,extracted)
    assert error.value.code=='HOTEL_DATA_REVIEW'
