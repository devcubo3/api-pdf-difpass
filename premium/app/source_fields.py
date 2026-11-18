"""Read labels attached to values in source blocks used by validated facts."""
import re
from dataclasses import dataclass

from .ai_images import _normalized
from .document_validation import literal, traveler_spacing


PERSON_LABELS = {
    'passageiro titular': ('lead_traveler', 'Passageiro titular'),
    'lead guest name': ('lead_traveler', 'Passageiro titular'),
    'lead passenger': ('lead_traveler', 'Passageiro titular'),
    'hospede titular': ('lead_guest', 'Hóspede titular'),
    'primary guest': ('lead_guest', 'Hóspede titular'),
    'titular da reserva': ('booking_holder', 'Titular da reserva'),
    'titular do ingresso': ('ticket_holder', 'Titular do ingresso'),
    'ticket holder': ('ticket_holder', 'Titular do ingresso'),
    'titular': ('holder', 'Titular'),
    'nome do passageiro': ('traveler', 'Passageiro'),
    'passageiro': ('traveler', 'Passageiro'),
    'passenger name': ('traveler', 'Passageiro'),
    'passenger': ('traveler', 'Passageiro'),
    'hospede': ('guest', 'Hóspede'),
    'nome do hospede': ('guest', 'Hóspede'),
    'guest name': ('guest', 'Hóspede'),
    'condutor principal': ('main_driver', 'Condutor principal'),
    'main driver': ('main_driver', 'Condutor principal'),
    'condutor': ('driver', 'Condutor'),
    'driver': ('driver', 'Condutor'),
    'driver name': ('driver', 'Condutor'),
    'participante': ('participant', 'Participante'),
    'participant': ('participant', 'Participante'),
}
PERSON_KINDS = {'holder_name', 'lead_guest_name', 'passenger_name', 'guest_name',
                'driver_name', 'participant_name', 'traveler_name', 'person_name', 'name'}
DATE_KINDS = {'arrival_date', 'departure_date', 'ticket_date', 'visit_date', 'validity'}
DATE_LABELS = {
    'data do ingresso': 'Data do ingresso', 'ticket date': 'Data do ingresso',
    'data da visita': 'Data da visita', 'visit date': 'Data da visita',
    'data de chegada': 'Data de chegada', 'arrival date': 'Data de chegada',
    'data de saida': 'Data de saída', 'departure date': 'Data de saída',
    'validade': 'Validade', 'validity': 'Validade',
}
TRAVELER_LABELS = {
    'passageiros': 'Passageiros', 'travelers': 'Passageiros', 'adultos': 'Adultos',
    'adults': 'Adultos', 'criancas': 'Crianças', 'children': 'Crianças',
}
REFERENCE_LABELS = {
    'loc': 'Localizador', 'localizador': 'Localizador',
    'loc localizador da reserva': 'Localizador da reserva',
    'localizador da reserva': 'Localizador da reserva', 'reserva': 'Localizador da reserva',
    'booking reference': 'Localizador da reserva', 'booking number': 'Número da reserva',
    'nro da reserva': 'Número da reserva', 'n da reserva': 'Número da reserva',
    'n de reserva': 'Número da reserva', 'no de reserva': 'Número da reserva',
    'numero da reserva': 'Número da reserva', 'voucher': 'Voucher',
    'localizador da companhia': 'Localizador da companhia', 'airline locator': 'Localizador da companhia',
}
DATE_FORMAT = re.compile(r'\((?:m[eê]s/dia/ano|dia/m[eê]s/ano|ano/m[eê]s/dia|[dmy]{2,4}/[dmy]{2,4}/[dmy]{2,4})\)', re.I)


@dataclass(frozen=True)
class FieldLabel:
    label: str
    source_label: str
    block_id: str
    role: str | None = None
    qualifier: str | None = None


def _rows(text):
    lines = [line.strip() for line in text.splitlines() if line.strip() and line.strip() != '|']
    for index, line in enumerate(lines):
        label, separator, value = line.partition(':')
        if separator:
            if value.strip():
                yield label.strip(), value.strip()
            elif index + 1 < len(lines):
                yield label.strip(), lines[index + 1]
            # Multiline traveler counts remain tied to their column heading.
            if _normalized(label.split('/')[0]) in {'passageiros', 'travelers'}:
                yield label.strip(), ' '.join([value, *lines[index + 1:]]).strip()
        elif index + 1 < len(lines):
            yield line, lines[index + 1]


def _matching_rows(value, evidence, blocks, *, date=False, travelers=False):
    clean = traveler_spacing if travelers else literal
    wanted = clean(value)
    if not wanted:
        return
    for entry in evidence:
        block = blocks.get(entry.block_id)
        if not block:
            continue
        for label, raw in _rows(block['text']):
            actual = clean(raw)
            if actual.casefold() == wanted.casefold():
                yield label, entry.block_id, None
            elif date and actual.casefold().startswith(wanted.casefold()):
                suffix = actual[len(wanted):].strip()
                if DATE_FORMAT.fullmatch(suffix):
                    yield label, entry.block_id, suffix


def _reference_label(label):
    plain_label = _normalized(label)
    if plain_label in REFERENCE_LABELS:
        return REFERENCE_LABELS[plain_label]
    match = re.fullmatch(r"([\w .’'-]{2,40}) (reservation|confirmation) number", label, re.I)
    if match:
        kind = 'reserva' if match[2].casefold() == 'reservation' else 'confirmação'
        return f'Número de {kind} {match[1].strip()}'
    if re.fullmatch(r'n[º°o]?\.? de confirma[cç][aã]o [\w .’-]{2,40}', label, re.I):
        return label
    return None


def field_label(kind, value, evidence, blocks, *, service_kind=None):
    matches = []
    for source, block_id, qualifier in _matching_rows(
            value, evidence, blocks, date=kind in DATE_KINDS, travelers=kind == 'traveler_count'):
        # The first half of an explicitly bilingual source label takes priority.
        key = _normalized(source.split('/')[0])
        if kind in PERSON_KINDS:
            role, label = PERSON_LABELS.get(key, (None, None))
            if key == 'lead guest name' and service_kind == 'hotel':
                role, label = 'lead_guest', 'Hóspede titular'
            if label:
                matches.append(FieldLabel(label, source, block_id, role))
        else:
            label = None
            if kind in DATE_KINDS:
                label = DATE_LABELS.get(key)
                if key == 'arrival date' and service_kind == 'ticket':
                    label = 'Data do ingresso'
            elif kind in {'booking_reference', 'rental_reference'}:
                label = _reference_label(source)
            elif kind == 'traveler_count':
                label = TRAVELER_LABELS.get(key)
            if label:
                matches.append(FieldLabel(label, source, block_id, qualifier=qualifier))
    meanings = {(m.label, m.role, m.qualifier) for m in matches}
    return matches[0] if len(meanings) == 1 else None


def driver_table_label(fact, blocks):
    """Read a driver identity from an adjacent header/row with matching columns."""
    for entry in fact.evidence:
        position = re.fullmatch(r'(p\d+)-b(\d+)', entry.block_id)
        if not position:
            continue
        page, index = position.groups()
        row = blocks.get(entry.block_id)
        header_id = f'{page}-b{int(index)-1}'
        header = blocks.get(header_id)
        if not row or not header:
            continue
        labels = header['text'].splitlines()
        values = row['text'].splitlines()
        if len(labels) != len(values) or len(values) < 2 or literal(values[0]) != literal(fact.raw_value):
            continue
        role, label = PERSON_LABELS.get(_normalized(labels[0]), (None, None))
        if (role in {'driver','main_driver'} and abs(header['bbox'][0]-row['bbox'][0]) <= 5
                and 0 <= row['bbox'][1]-header['bbox'][3] <= 30):
            return FieldLabel(label, labels[0], header_id, role)
    return None
