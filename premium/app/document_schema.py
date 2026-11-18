"""Provider-independent understanding. Source/layout metadata is supplied by the server."""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field


class Record(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)


class Evidence(Record):
    block_id: str
    quote: str


class Entity(Record):
    id: str
    role: Literal['person','provider','property','location','contact','other']
    name: str
    evidence: list[Evidence]
    locality: str | None = None


class Service(Record):
    id: str
    kind: Literal['flight','hotel','ticket','car_rental','transfer','insurance','cruise','proposal','other']
    name: str | None
    entity_ids: list[str]
    evidence: list[Evidence]


class Fact(Record):
    id: str
    service_id: str | None
    entity_id: str | None
    kind: str
    label: str
    raw_value: str
    status: Literal['supported','ambiguous','unreadable','absent']
    evidence: list[Evidence]


class Condition(Record):
    id: str
    service_id: str | None
    kind: Literal['general','cancellation','baggage','documentation','fees','coverage','access','checkin','other']
    evidence: list[Evidence]


class CodeAssociation(Record):
    code_id: str
    purpose: Literal['ticket','booking','other','unknown']
    service_ids: list[str]
    holder_entity_ids: list[str]
    number_fact_ids: list[str]
    evidence: list[Evidence]


class Understanding(Record):
    schema_version: Literal['canonical_v1']='canonical_v1'
    entities: list[Entity]=Field(default_factory=list)
    services: list[Service]=Field(default_factory=list)
    facts: list[Fact]=Field(default_factory=list)
    conditions: list[Condition]=Field(default_factory=list)
    code_associations: list[CodeAssociation]=Field(default_factory=list)
    unreadable_code_pages: list[int]=Field(default_factory=list)
    primary_place_id: str | None = None


def strict_schema(model):
    """One schema derived from runtime types; strict outputs require all keys."""
    schema=model.model_json_schema()
    def visit(node):
        if isinstance(node,dict):
            node.pop('default',None)
            if 'properties' in node:
                node['required']=list(node['properties'])
                node['additionalProperties']=False
            for value in node.values(): visit(value)
        elif isinstance(node,list):
            for value in node: visit(value)
    visit(schema)
    return schema
