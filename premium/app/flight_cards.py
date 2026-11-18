"""Build the flight card directly from grounded field kinds, without sentence parsing."""
import hashlib
import re


def section_key(section):
    return hashlib.sha256(section.model_dump_json().encode()).hexdigest()


def flight_card(facts,other_bullets):
    by_kind={}
    singletons={'origin','destination','origin_city','destination_city','departure_city','arrival_city',
        'origin_code','destination_code','departure_airport_code','arrival_airport_code','departure_time',
        'arrival_time','flight_number','duration','departure_airport_name','arrival_airport_name'}
    for fact in facts:
        if fact.kind in by_kind and fact.kind in singletons: return None
        by_kind[fact.kind]=fact
    used=set()
    def pick(*names):
        for key in names:
            if key in by_kind:
                used.add(key)
                return by_kind[key].raw_value
        return None
    route={}
    for side,prefix in (('departure','origin'),('arrival','destination')):
        value=pick(prefix)
        city=pick(prefix+'_city',side+'_city')
        code=pick(prefix+'_code',side+'_airport_code')
        if value and not code:
            match=re.fullmatch(r'(.*?)\s*\(([A-Z]{3})\)',value)
            if match: city,code=match[1].strip(),match[2]
            elif re.fullmatch(r'[A-Z]{3}',value): code=value
            else: return None
        time=pick(side+'_time')
        if not code or not re.fullmatch(r'[A-Z]{3}',code) or not time or not re.fullmatch(r'\d{2}:\d{2}',time): return None
        if int(time[:2])>23 or int(time[3:])>59: return None
        route[side]={'city':city or '', 'code':code,'time':time,'airport':pick(side+'_airport_name')}
    number=pick('flight_number')
    if not number: return None
    duration=pick('duration')
    extra=list(other_bullets)+[f'{f.label}: {f.raw_value}' for f in facts if f.kind not in used]
    bullets=[f'Saída: {route["departure"]["city"]} ({route["departure"]["code"]}) {route["departure"]["time"]}.',
             f'Chegada: {route["arrival"]["city"]} ({route["arrival"]["code"]}) {route["arrival"]["time"]}.',
             f'Voo {number}.']
    if duration: bullets.append('Duração: '+duration+'.')
    for side,label in (('departure','Aeroporto de saída'),('arrival','Aeroporto de chegada')):
        if route[side]['airport']: bullets.append(label+': '+route[side]['airport'])
    return {**route,'number':number,'duration':duration,'extra':extra},bullets+extra
