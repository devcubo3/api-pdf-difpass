"""Text-preserving emphasis and field views. Never interpret source text as HTML."""
import re


PATTERNS={
    'money':r'R\$\s*\d[\d.,]*\d|R\$\s*\d',
    'restriction':r'\bn[aã]o\s+(?:reembols[aá]ve(?:l|is)|perm[ií]tid[ao]s?|aceita(?:m)?|'
        r's[aã]o\s+(?:aceitas|disponibilizados)|podem\s+ser\s+alterados)|'
        r'\b(?:[eé]\s+proibido|taxa obrigat[oó]ria|cobrados?\s+[aà]\s+parte|'
        r'0\s+malas?\s+despachadas?|documento original|autenticad[ao]s?\s+em\s+cart[oó]rio)\b',
    'date':r'\b\d{1,2}\s+(?:jan|fev|mar|abr|mai|jun|jul|ago|set|out|nov|dez)(?:\s+\d{4})?\b|'
        r'\b\d{1,2}/\d{1,2}/\d{2,4}\b',
    'time':r'\b\d{1,2}:\d{2}(?:\s*(?:AM|PM))?\b|\b\d{1,2}h\d{0,2}\b',
    'value':r'\b\d+(?:[.,]\d+)?\s*(?:kg|minutos?|horas?|noites?|adultos?|anos|metros|malas?|item pessoal)\b|'
        r'\b(?:All Inclusive|gratuita|gratuito|RG|CNH|passaporte|24h)\b',
}
FIELD=re.compile(r'^(?P<label>Identifica[çc][aã]o e endere[çc]o|Contato do hotel|Central de reservas|E-mail (?:de reservas|do hotel)|Localizador|Voucher|Check-in|Check-out|Endere[çc]o(?:\s+informado)?|'
    r'Passageiros|H[oó]spedes|Plant[aã]o|Telefone|Contato(?:\s+(?:de emerg[eê]ncia))?(?:\s+informado)?|'
    r'Per[ií]odo(?:\s+de\s+utiliza[çc][aã]o)?|Ida|Volta|Descri[çc][aã]o do servi[çc]o|'
    r'Atividades citadas|Validade(?:\s+impressa)?|N[uú]mero do ingresso|Confirma[çc][aã]o(?:\s+UO)?)\s*:\s*(?P<value>.+)$',re.I)
STAY_DATES=re.compile(r'^(Check-in:)\s*(.+?)\s+(Check-out:)\s*(.+)$',re.I)


def emphasis_segments(text,critical_values=()):
    spans=[]
    for kind,pattern in PATTERNS.items():
        for match in re.finditer(pattern,text,re.I):
            spans.append((match.start(),match.end(),kind))
    for value in critical_values:
        if isinstance(value,str) and 4<=len(value)<=70:
            for match in re.finditer(re.escape(value),text,re.I):
                if (match.start()==0 or not text[match.start()-1].isalnum()) and (
                        match.end()==len(text) or not text[match.end()].isalnum()):
                    spans.append((match.start(),match.end(),'critical'))
    # Longest match wins at a shared start, without creating overlapping markup.
    spans.sort(key=lambda span:(span[0],-(span[1]-span[0])))
    result=[]
    end=0
    for start,stop,kind in spans:
        if start<end:
            continue
        if start>end:
            result.append({'text':text[end:start],'emphasis':None})
        result.append({'text':text[start:stop],'emphasis':kind})
        end=stop
    if end<len(text):
        result.append({'text':text[end:],'emphasis':None})
    return result


def _field(label,value,critical_values):
    return {'label':label.rstrip(':'),'value':value,'segments':emphasis_segments(value,critical_values),
            'reference':label.lower().rstrip(':') in {'localizador','voucher','número do ingresso','confirmação uo'}
                        and len(value)<70 and ':' not in value}


def bullet_view(text,critical_values=()):
    dates=STAY_DATES.fullmatch(text)
    if dates:
        return {'kind':'paired','fields':[_field(dates[1],dates[2],critical_values),
                                         _field(dates[3],dates[4],critical_values)]}
    match=FIELD.fullmatch(text)
    if match:
        return {'kind':'field','field':_field(match['label'],match['value'],critical_values)}
    return {'kind':'text','segments':emphasis_segments(text,critical_values)}
