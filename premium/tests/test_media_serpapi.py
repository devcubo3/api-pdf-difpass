"""Image search credits, source fidelity and secret isolation; no real requests."""
from dataclasses import replace
import json
from urllib.parse import parse_qs,urlsplit

import pytest

from app import media_serpapi
from app.ai_budget import BudgetLedger,SerpApiLedger
from app.config import Settings
from app.media import PhotoSearch
from app.media_fetch import Fetched,MediaError
from app.media_serpapi import SerpApiImages,SerpApiClient
from test_media import photo,source_settings,FakeSource
from test_media_general import discovery_case


def response():
    return {'search_metadata':{'status':'Success','raw_html_file':'https://serpapi.com/?api_key=synthetic-secret'},
        'api_key':'synthetic-secret','images_results':[
        {'title':'Unregistered Lodge Zermatt aerial view','link':'https://lodge.example.test/zermatt/',
         'original':'https://cdn.example.test/lodge-aerial.jpg','original_width':1500,
         'original_height':750,'source':'Lodge Magazine','thumbnail':'https://serpapi.com/thumb.jpg'}]}


class Client:
    def __init__(self): self.calls=[]
    def get_json(self,path,params):
        self.calls.append((path,params));return response()


def test_configuration_never_reveals_serpapi_key(monkeypatch):
    monkeypatch.setenv('SERPAPI_KEY','synthetic-secret')
    settings=Settings.from_env()
    assert settings.serpapi_api_key=='synthetic-secret'
    assert 'synthetic-secret' not in repr(settings)


def test_one_search_is_budgeted_cached_and_does_not_save_debug_or_key(discovery_case,tmp_path):
    settings,db,row=discovery_case
    settings=replace(settings,serpapi_api_key='synthetic-secret')
    client=Client();search=SerpApiImages(settings,db,row,tmp_path,client=client)
    place={'name':'Unregistered Lodge','locality':'Zermatt'}
    results=search.search(place)
    assert results[0]['original'].endswith('lodge-aerial.jpg')
    assert client.calls[0][1]['engine']=='google_images' and client.calls[0][1]['safe']=='active'
    assert search.usage['new_calls']==1 and search.usage['usage']['generative_ai_calls']==0
    assert search.search(place)==results and len(client.calls)==1 and search.usage['cache_hit']
    raw=next(tmp_path.glob('serpapi-*.json')).read_text()
    assert 'synthetic-secret' not in raw and 'thumbnail' not in raw and 'raw_html_file' not in raw
    assert BudgetLedger(db,row).summary()==[]
    assert SerpApiLedger(db,row).summary()[0]['reserved_credits']==1
    next(tmp_path.glob('serpapi-*.json')).write_text('tampered')
    with pytest.raises(MediaError,match='incerta'):
        search.search(place)
    assert len(client.calls)==1


def test_cache_only_and_credit_limit_block_search_but_ai_budget_does_not(discovery_case,tmp_path):
    settings,db,row=discovery_case
    settings=replace(settings,serpapi_api_key='synthetic-secret')
    client=Client();search=SerpApiImages(settings,db,row,tmp_path,client=client,allow_paid=False)
    with pytest.raises(MediaError) as error: search.search({'name':'Unregistered Lodge'})
    assert error.value.code=='MEDIA_SEARCH_CACHE_REQUIRED' and client.calls==[]
    BudgetLedger(db,row).reserve('f'*64,'plan','synthetic',12)
    search=SerpApiImages(settings,db,row,tmp_path,client=client)
    assert search.search({'name':'Unregistered Lodge'}) and len(client.calls)==1
    # Exhaust one independent SaaS credit; the AI balance is irrelevant.
    db.settings=replace(settings,serpapi_daily_search_limit=1)
    with pytest.raises(MediaError) as error:
        SerpApiImages(settings,db,row,tmp_path,client=client).search({'name':'Different Lodge'})
    assert error.value.code=='MEDIA_SERPAPI_CREDIT_LIMIT' and len(client.calls)==1


def test_timeout_is_not_automatically_retried(discovery_case,tmp_path):
    settings,db,row=discovery_case
    calls=[]
    class Failed:
        def get_json(self,*args): calls.append(1);raise TimeoutError('synthetic-secret')
    search=SerpApiImages(replace(settings,serpapi_api_key='synthetic-secret'),db,row,tmp_path,client=Failed())
    for _ in range(2):
        with pytest.raises(MediaError) as error: search.search({'name':'Unregistered Lodge'})
        assert error.value.code=='MEDIA_SEARCH_UNCERTAIN' and 'synthetic-secret' not in str(error.value)
    assert len(calls)==1


@pytest.mark.parametrize('status,code',[(302,'MEDIA_SERPAPI_HTTP'),(401,'MEDIA_SERPAPI_AUTH'),(429,'MEDIA_SERPAPI_QUOTA')])
def test_fixed_host_client_does_not_follow_redirects_or_expose_key(monkeypatch,status,code):
    calls=[]
    class Response:
        def __init__(self): self.status=status
        def getheader(self,*args): return 'https://other.example.test/'
    class Connection:
        def __init__(self,host,address,timeout): assert host=='serpapi.com'
        def request(self,method,path,headers): calls.append(path)
        def getresponse(self): return Response()
        def close(self): pass
    monkeypatch.setattr(media_serpapi,'PinnedHTTPS',Connection)
    monkeypatch.setattr(media_serpapi,'public_addresses',lambda host:['93.184.216.34'])
    with pytest.raises(MediaError) as error: SerpApiClient('synthetic-secret').get_json('/search.json',{'q':'Hotel'})
    assert error.value.code==code and 'synthetic-secret' not in str(error.value)
    assert len(calls)==1 and parse_qs(urlsplit(calls[0]).query)['api_key']==['synthetic-secret']


def test_general_photo_search_prefers_serpapi_over_paid_openai_and_filters_candidates(discovery_case,tmp_path,monkeypatch):
    settings,db,row=discovery_case
    settings=replace(settings,serpapi_api_key='synthetic-secret')
    client=Client()
    class Images:
        def get_json(self,path,params):
            payload=client.get_json(path,params)
            payload['images_results']+= [
                {**payload['images_results'][0],'title':'Another Lodge Zermatt'},
                {**payload['images_results'][0],'link':'https://127.0.0.1/private'},
                {**payload['images_results'][0],'original_width':400},
                {**payload['images_results'][0],'original':'https://encrypted-tbn0.gstatic.com/thumbnail.jpg'},
                {**payload['images_results'][0],'original':'https://lh3.googleusercontent.com/maps-photo.jpg'},
                {**payload['images_results'][0],'is_product':True},
                {**payload['images_results'][0],'title':'Unregistered Lodge Paris',
                 'link':'https://lodge.example.test/paris/'}]
            return payload
    monkeypatch.setattr(media_serpapi,'SerpApiClient',lambda key:Images())
    fetches=[]
    class Fetcher:
        def get(self,url,**kwargs):
            assert 'api_key' not in url and not kwargs.get('headers')
            fetches.append(url);return Fetched(url,'image/jpeg',photo())
    search=PhotoSearch(settings,row['tenant_id'],Fetcher(),db=db,row=row)
    monkeypatch.setattr(search.web,'discover',lambda *args:pytest.fail('No second paid provider'))
    result=search.search('Unregistered Lodge',locality='Zermatt',kind='hotel')
    assert len(result['candidates'])==1 and len(fetches)==1 and len(client.calls)==1
    asset=result['candidates'][0]
    assert asset['provider']=='serpapi_google_images' and asset['rights_status']=='permission_pending'
    assert asset['source_page_url']=='https://lodge.example.test/zermatt/'
    assert result['search_usage']['new_calls']==1
    assert search.search('Unregistered Lodge',locality='Zermatt',kind='hotel')['cache_hit']
    assert len(client.calls)==1


def test_registered_official_source_remains_free_first_choice(source_settings,monkeypatch):
    settings=replace(source_settings,serpapi_api_key='synthetic-secret')
    monkeypatch.setattr(SerpApiImages,'search',lambda *args:pytest.fail('Official source already works'))
    result=PhotoSearch(settings,'org',FakeSource()).search('Example Resort Coast')
    assert result['candidates'][0]['provider']=='official_site'
    assert result['search_usage']['new_calls']==0


def test_official_site_without_usable_image_falls_back_to_one_serpapi_search(discovery_case,monkeypatch):
    settings,db,row=discovery_case
    settings=replace(settings,serpapi_api_key='synthetic-secret')
    client=Client()
    class Search:
        def get_json(self,path,params):
            payload=client.get_json(path,params)
            payload['images_results'][0].update(title='Example Resort Coast aerial',
                link='https://resort.example.test/',original='https://cdn.example.test/resort-aerial.jpg')
            return payload
    monkeypatch.setattr(media_serpapi,'SerpApiClient',lambda key:Search())
    class Fetcher:
        def get(self,url,**kwargs):
            if url=='https://resort.example.test/':
                return Fetched(url,'text/html',b'<title>Example Resort Coast</title><p>No public gallery</p>')
            return Fetched(url,'image/jpeg',photo())
    result=PhotoSearch(settings,row['tenant_id'],Fetcher(),db=db,row=row).search('Example Resort Coast')
    assert result['candidates'][0]['authority']=='official' and len(client.calls)==1
