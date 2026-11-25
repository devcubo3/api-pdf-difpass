"""One credited image lookup; no model calls, redirects or secret-bearing logs."""
import hashlib
import json
from urllib.parse import urlencode

from .ai_budget import SerpApiLedger,AIBudgetError
from .media_fetch import MediaError,PinnedHTTPS,public_addresses
from .storage import Storage


class SerpApiClient:
    def __init__(self,key):
        self._key=key

    def get_json(self,path,params=None):
        # The provider requires a query-string key. Use a fixed TLS host and no
        # HTTP logging/redirect following; never return or persist the request URL.
        if path not in {'/search.json','/account.json'} or not self._key:
            raise MediaError('MEDIA_SERPAPI_CONFIG','Configure a chave privada da SerpApi.')
        connection=None
        try:
            connection=PinnedHTTPS('serpapi.com',public_addresses('serpapi.com')[0],25)
            query=urlencode({**(params or {}),'api_key':self._key})
            connection.request('GET',path+'?'+query,headers={
                'Accept':'application/json','Accept-Encoding':'identity',
                'User-Agent':'DiffTravelPremiumPDF/2.0'})
            response=connection.getresponse()
            if response.status!=200:
                code={401:'MEDIA_SERPAPI_AUTH',403:'MEDIA_SERPAPI_AUTH',
                      429:'MEDIA_SERPAPI_QUOTA'}.get(response.status,'MEDIA_SERPAPI_HTTP')
                raise MediaError(code,'A SerpApi não concluiu a consulta. Verifique a chave, a cota e o status do provedor.')
            if response.getheader('Content-Encoding','identity').lower() not in {'','identity'}:
                raise ValueError()
            content=response.read(2_000_001)
            if len(content)>2_000_000:
                raise ValueError()
            result=json.loads(content)
            if not isinstance(result,dict) or result.get('error'):
                raise ValueError()
            return result
        except MediaError:
            raise
        except Exception:
            raise MediaError('MEDIA_SERPAPI_RESPONSE','A consulta de imagens não retornou uma resposta válida.') from None
        finally:
            if connection is not None:
                connection.close()


def sanitized_results(payload):
    """Keep only public photo metadata; discard key, debug and pagination URLs."""
    if payload.get('search_metadata',{}).get('status')!='Success':
        raise MediaError('MEDIA_SERPAPI_INCOMPLETE','A busca de imagens não foi concluída.')
    results=payload.get('images_results',[])
    if not isinstance(results,list):
        raise MediaError('MEDIA_SERPAPI_RESPONSE','Lista de imagens inválida.')
    keys=('title','link','original','source','original_width','original_height','is_product')
    return [{key:item[key] for key in keys if key in item}
            for item in results[:60] if isinstance(item,dict)]


class SerpApiImages:
    def __init__(self,settings,db,row,cache_root,*,client=None,allow_paid=True):
        self.settings,self.db,self.row=settings,db,row
        self.root=cache_root
        self.client=client
        self.allow_paid=allow_paid
        self.usage={}

    def search(self,place):
        if not self.settings.serpapi_api_key or not self.db or not self.row:
            raise MediaError('MEDIA_SERPAPI_CONFIG','Configure a SerpApi no ambiente privado da API.')
        # Caller supplies only grounded public place/locality, never PDF/customer data.
        query=' '.join(filter(None,[place['name'],place.get('locality'),'photos']))
        params={'engine':'google_images','q':query,'hl':'pt','safe':'active',
                'imgar':'w','imgsz':'l','image_type':'photo','ijn':0,'no_cache':'false'}
        fingerprint=hashlib.sha256(json.dumps(params,sort_keys=True).encode()).hexdigest()
        key=hashlib.sha256(f'{self.row["id"]}:{self.row["revision"]}:serpapi-images-v1:{fingerprint}'.encode()).hexdigest()
        target=self.root/f'serpapi-{key}.json'
        ledger=SerpApiLedger(self.db,self.row)
        existing=ledger.cached(key)
        if not existing:
            if not self.allow_paid:
                raise MediaError('MEDIA_SEARCH_CACHE_REQUIRED','Esta reconstrução não permite consumir um novo crédito de busca.')
            try:
                existing=ledger.reserve(key)
            except AIBudgetError:
                raise MediaError('MEDIA_SERPAPI_CREDIT_LIMIT','Cota de buscas SerpApi atingida; o orçamento de IA não foi consumido.') from None
        if existing:
            if (existing['status']!='completed' or not target.is_file()
                    or hashlib.sha256(target.read_bytes()).hexdigest()!=existing['response_sha256']):
                raise MediaError('MEDIA_SEARCH_UNCERTAIN','Consulta anterior incerta; não repetir o consumo de créditos automaticamente.')
            self.usage={'provider':'serpapi','cache_hit':True,'new_calls':0,
                        'reserved_estimate_usd':0,'reserved_credits':0,'usage':existing['usage_json']}
            return json.loads(target.read_bytes())
        try:
            payload=(self.client or SerpApiClient(self.settings.serpapi_api_key)).get_json('/search.json',params)
            results=sanitized_results(payload)
            raw=json.dumps(results,ensure_ascii=False).encode()
            Storage._write_atomic(target,raw)
            usage={'provider':'serpapi','engine':'google_images','search_requests':1,
                   'maximum_search_credits':1,'generative_ai_calls':0}
            ledger.finish(key,hashlib.sha256(raw).hexdigest(),usage)
            self.usage={'provider':'serpapi','cache_hit':False,'new_calls':1,
                        'reserved_estimate_usd':0,'reserved_credits':1,'usage':usage}
            return results
        except Exception as exc:
            ledger.uncertain(key)
            if isinstance(exc,MediaError):
                raise
            raise MediaError('MEDIA_SEARCH_UNCERTAIN','A busca não concluiu com segurança; não repetir automaticamente.') from None
