"""Formatting may emphasize source values, never rewrite them or accept HTML."""
import pytest

from app.typography import bullet_view, emphasis_segments


@pytest.mark.parametrize('text,critical',[
    ('  R$ 60,00; 0 malas despachadas. Não reembolsável.\n', ['R$ 60,00','60,00']),
    ('Check-in: 22 nov 2026, às 03:00 PM. Check-out: 27 nov 2026, às 12:00 PM.', []),
    ('ABC1234 não é ABC123; RG, CNH e passaporte. https://exemplo.test/a?x=1&y=2', ['ABC123']),
    ('<script>"22/11/2026"</script><img src="https://invalid.test">', ['22/11/2026']),
    ('', []),
])
def test_emphasis_retains_every_source_character(text,critical):
    segments=emphasis_segments(text,critical)
    assert ''.join(part['text'] for part in segments)==text


def test_reference_emphasis_does_not_match_a_different_longer_code():
    parts=emphasis_segments('ABC1234; ABC123.', ['ABC123'])
    assert [part['text'] for part in parts if part['emphasis']]=='ABC123'.split()


def test_field_views_keep_values_and_original_labels():
    view=bullet_view('Check-in: 22 nov 2026, às 03:00 PM. Check-out: 27 nov 2026, às 12:00 PM.')
    assert view['kind']=='paired'
    assert [(part['label'],part['value']) for part in view['fields']]==[
        ('Check-in','22 nov 2026, às 03:00 PM.'),('Check-out','27 nov 2026, às 12:00 PM.')]
    view=bullet_view('Localizador: <img src="https://invalid.test">')
    assert view['field']['value']=='<img src="https://invalid.test">'
    assert ''.join(part['text'] for part in view['field']['segments'])==view['field']['value']

