"""Synthetic photo sources and HTTP contracts; no provider keys or real network."""
from dataclasses import replace
import io
import json
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient
from PIL import Image
import pytest
from sqlalchemy import select

from app import ai_images,media,media_fetch
from app.config import Settings
from app.db import ai_calls
from app.main import create_app
from app.media import MediaCatalog,PhotoPage,PhotoSearch,choose_candidate,matches_entity,photo_score
from app.media_fetch import Fetched,MediaError,PublicFetcher,public_addresses,public_url
from app.pipeline import process_next
from test_html_template import case


def photo(color='#ddeeff'):
    target=io.BytesIO()
    Image.new('RGB',(1500,750),color).save(target,format='JPEG')
    return target.getvalue()


class FakeSource:
    def __init__(self):
        self.calls=[]

    def get(self,url,**kwargs):
        self.calls.append((url,kwargs))
        if url=='https://resort.example.test/':
            return Fetched(url,'text/html',b'''<title>Example Resort Coast</title>
                <meta property="og:image" content="/home-praia.jpg">
                <section class="photo-gallery"><img src="/aerial-resort.jpg" alt="Panorama do resort">
                <img src="/favicon.png"><img src="https://127.0.0.1/internal.jpg">
                <img src="/suite-mobile.jpg"></section>''')
        if url.endswith('home-praia.jpg'):
            return Fetched(url,'image/jpeg',photo())
        if url.endswith('aerial-resort.jpg'):
            return Fetched(url,'image/jpeg',photo('#ccddcc'))
        raise MediaError('MEDIA_FETCH_FAILED','Synthetic unavailable photo')


@pytest.fixture
def source_settings(tmp_path):
    directory=tmp_path/'sources.json'
    directory.write_text(json.dumps({'sources':[{'entity':'Example Resort Coast','aliases':['Coast'],
        'page_url':'https://resort.example.test/','publisher':'Example Resort','authority':'official'}]}))
    return Settings(data_dir=tmp_path/'storage',database_url=f'sqlite:///{tmp_path.as_posix()}/state.db',
                    media_sources_file=directory)


@pytest.mark.parametrize('url',[
    'http://example.test/photo.jpg','file:///private.env','https://localhost/a',
    'https://127.0.0.1/a','https://169.254.169.254/a','https://10.0.0.1/a',
    'https://user:fake@example.test/a','https://example.test:444/a',
    'https://example.test/a\r\nX: fake','https://host.internal/a'])
def test_media_rejects_nonpublic_or_credential_bearing_urls(url):
    with pytest.raises(MediaError):
        public_url(url)


def test_media_dns_checks_all_answers_and_pins_validated_address(monkeypatch):
    monkeypatch.setattr(media_fetch.socket,'getaddrinfo',lambda *a,**k:[
        (None,None,None,None,('93.184.216.34',443)),(None,None,None,None,('127.0.0.1',443))])
    with pytest.raises(MediaError) as error:
        public_addresses('example.test')
    assert error.value.code=='MEDIA_URL_BLOCKED'
    monkeypatch.setattr(media_fetch.socket,'getaddrinfo',lambda *a,**k:[
        (None,None,None,None,('93.184.216.34',443))])
    calls=[]
    class Response:
        status=200
        remaining=b'photo'
        def getheader(self,name,default=None):
            return {'Content-Type':'image/jpeg','Content-Length':'5'}.get(name,default)
        def read(self,size):
            chunk,self.remaining=self.remaining[:size],self.remaining[size:]
            return chunk
    class Connection:
        def __init__(self,host,address,timeout):
            calls.append((host,address))
        def request(self,*args,**kwargs):
            pass
        def getresponse(self):
            return Response()
        def close(self):
            pass
    monkeypatch.setattr(media_fetch,'PinnedHTTPS',Connection)
    assert PublicFetcher().get('https://example.test/photo.jpg').content==b'photo'
    assert calls==[('example.test','93.184.216.34')]


def test_media_redirects_revalidate_and_do_not_forward_provider_secret(monkeypatch):
    monkeypatch.setattr(media_fetch,'public_addresses',lambda host:['93.184.216.34'])
    requests=[]
    destination=['https://images.example.test/photo.jpg']
    class Response:
        def __init__(self,first):
            self.status=302 if first else 200
            self.remaining=b'image'
        def getheader(self,name,default=None):
            return {'Location':destination[0],'Content-Type':'image/jpeg'}.get(name,default)
        def read(self,size):
            chunk,self.remaining=self.remaining[:size],self.remaining[size:]
            return chunk
    class Connection:
        def __init__(self,host,address,timeout):
            self.host=host
        def request(self,*args,headers):
            requests.append((self.host,headers))
        def getresponse(self):
            return Response(self.host=='api.example.test')
        def close(self):
            pass
    monkeypatch.setattr(media_fetch,'PinnedHTTPS',Connection)
    PublicFetcher().get('https://api.example.test/photo',headers={'Authorization':'synthetic-secret'})
    assert requests[0][1]['Authorization']=='synthetic-secret'
    assert 'Authorization' not in requests[1][1]
    destination[0]='https://127.0.0.1/internal'
    with pytest.raises(MediaError):
        PublicFetcher().get('https://api.example.test/photo')


def test_media_search_discovers_photos_caches_and_separates_tenants(source_settings):
    fetcher=FakeSource()
    search=PhotoSearch(source_settings,'org-a',fetcher)
    result=search.search('Coast')
    assert len(result['candidates'])==2
    assert all(c['authority']=='official' and c['rights_status']=='permission_pending' for c in result['candidates'])
    assert not any('favicon' in url or '127.0.0.1' in url for url,_ in fetcher.calls)
    assert result['google_maps_url'].startswith('https://www.google.com/maps/search/')
    count=len(fetcher.calls)
    again=search.search('Coast')
    assert again['cache_hit'] and len(fetcher.calls)==count
    assert search.search('COAST')['cache_hit'] and len(fetcher.calls)==count
    assert choose_candidate(result,'synthetic-document')==choose_candidate(again,'synthetic-document')
    asset=result['candidates'][0]
    with pytest.raises(MediaError):
        MediaCatalog(source_settings,'org-b').load(asset['id'])
    path=Path(search.catalog.load(asset['id'])['path'])
    path.write_bytes(b'tampered')
    with pytest.raises(MediaError) as error:
        search.search('Coast')
    assert error.value.code=='MEDIA_INTEGRITY'


def test_media_identity_resolution_and_photo_filters(source_settings):
    assert matches_entity('Example Resort Coast','Welcome to Example Resort Coast')
    assert not matches_entity('Example Resort Coast','Example Resort Mountains')
    search=PhotoSearch(source_settings,'a',FakeSource())
    assert search.source_for('Other hotel in Coast') is None
    assert photo_score('https://example.test/logo.png','')<0
    assert photo_score('https://example.test/home-praia.jpg','')>photo_score('https://example.test/suite-mobile.jpg','')
    parsed=PhotoPage('<title>Coast</title><img srcset="/small.jpg 200w, /large.jpg 1600w">','https://example.test/')
    assert any(item[0]=='https://example.test/large.jpg' for item in parsed.photos)


def test_media_blocks_google_photo_storage_and_invalid_formats(source_settings):
    catalog=MediaCatalog(source_settings,'a')
    with pytest.raises(MediaError) as error:
        catalog.save_photo(Fetched('https://lh3.googleusercontent.com/photo','image/jpeg',photo()),{},0)
    assert error.value.code=='MEDIA_GOOGLE_STORAGE_BLOCKED'
    with pytest.raises(MediaError):
        catalog.save_photo(Fetched('https://example.test/photo.svg','image/svg+xml',b'<svg/>'),{},0)
    with pytest.raises(MediaError):
        catalog.save_photo(Fetched('https://example.test/photo.jpg','image/jpeg',b'not an image'),{},0)
    with pytest.raises(MediaError):
        catalog.save_photo(Fetched('https://example.test/photo.jpg','image/jpeg',photo()),
                           {'page_url':'javascript:alert(1)'},0)


def test_media_http_search_selection_requires_review_and_rebuild(case,source_settings,monkeypatch):
    settings,source,_,plan=case
    settings=replace(settings,media_sources_file=source_settings.media_sources_file)
    fake=SimpleNamespace(responses=SimpleNamespace(create=lambda **k:SimpleNamespace(
        status='completed',output_text=plan.model_dump_json(),usage=None)))
    monkeypatch.setattr(ai_images,'OpenAI',lambda **k:fake)
    fetcher=FakeSource()
    monkeypatch.setattr(media,'PublicFetcher',lambda:fetcher)
    auth={'Authorization':'Bearer reviewer'}
    app=create_app(settings)
    with TestClient(app) as client:
        uploaded=client.post('/v2/documents',headers={**auth,'Idempotency-Key':'photo-contract'},
            files={'input_pdf':('fixture.pdf',source.read_bytes(),'application/pdf')})
        route='/v2/documents/'+uploaded.json()['document_id']
        assert process_next(settings)
        etag=client.get(route,headers=auth).json()['etag']
        body={'reason':'Synthetic photo search','subject':'Coast','auto_select':True}
        assert client.post(route+'/media/search',json=body).status_code==401
        assert client.post(route+'/media/search',headers={'Authorization':'Bearer reader','If-Match':etag},json=body).status_code==403
        assert client.post(route+'/media/search',headers={'Authorization':'Bearer other','If-Match':etag},json=body).status_code==404
        assert client.post(route+'/media/search',headers=auth,json=body).status_code==428
        assert client.post(route+'/media/search',headers={**auth,'If-Match':'stale'},json=body).status_code==409
        response=client.post(route+'/media/search',headers={**auth,'If-Match':etag},json=body)
        assert response.status_code==200,response.text
        result=response.json()
        asset_id=result['selected_asset_id']
        assert 'path' not in json.dumps(result)
        assert client.get(route+f'/media/{asset_id}.jpg').status_code==401
        assert client.get(route+f'/media/{asset_id}.jpg',headers=auth).status_code==200
        assert client.get(route+f'/media/{asset_id}.jpg',headers={'Authorization':'Bearer other'}).status_code==404
        before=len(fetcher.calls)
        maps=client.get(route+'/media/maps',headers=auth).json()
        assert not maps['photo_import_enabled'] and 'google.com/maps/search/' in maps['url']
        assert len(fetcher.calls)==before
        review=client.get(route+'/review',headers=auth).json()
        assert not review['checks']['validations_passed']
        assert any(i['code']=='MEDIA_REBUILD_REQUIRED' for i in review['issues'])
        etag=client.get(route,headers=auth).json()['etag']
        assert client.post(route+'/template',headers={**auth,'If-Match':etag},json={
            'reason':'Cache-only photo preview','generation_scope':'full'}).status_code==202
        assert process_next(replace(settings,openai_api_key=None))
        review=client.get(route+'/review',headers=auth).json()
        assert review['checks']['photo']['id']==asset_id
        assert review['checks']['codes_valid'] and all(c['passed'] for c in review['checks']['template_text'])
        assert any(i['code']=='PHOTO_USAGE_REVIEW' and i['blocking'] for i in review['issues'])
        etag=client.get(route,headers=auth).json()['etag']
        decision={'action':'approve','revision':review['revision'],'artifact_sha256':review['artifact_sha256']}
        assert client.post(route+'/review/decision',headers={**auth,'If-Match':etag},json=decision).status_code==409
        assert client.get(route+'/review/pdf',headers=auth).status_code==200
        with app.state.db.engine.connect() as connection:
            assert len(connection.execute(select(ai_calls)).all())==1


def test_media_brave_and_pexels_adapters_keep_credentials_off_photo_hosts(source_settings):
    settings=replace(source_settings,brave_search_api_key='fake-brave',pexels_api_key='fake-pexels')
    class Providers(FakeSource):
        def get(self,url,**kwargs):
            if url.startswith('https://api.search.brave.com/'):
                self.calls.append((url,kwargs))
                assert kwargs['headers']=={'X-Subscription-Token':'fake-brave'}
                return Fetched(url,'application/json',json.dumps({'web':{'results':[
                    {'title':'Example Resort Coast','url':'https://resort.example.test/'}]}}).encode())
            if url.startswith('https://api.pexels.com/'):
                self.calls.append((url,kwargs))
                assert kwargs['headers']=={'Authorization':'fake-pexels'}
                return Fetched(url,'application/json',json.dumps({'photos':[{
                    'url':'https://www.pexels.com/photo/fictional-1','alt':'Synthetic coast',
                    'photographer':'Synthetic Author','src':{'original':'https://resort.example.test/home-praia.jpg'}}]}).encode())
            assert 'headers' not in kwargs
            return super().get(url,**kwargs)
    fetcher=Providers()
    result=PhotoSearch(settings,'brave-org',fetcher).search('Example Resort Coast',provider='brave')
    assert result['candidates'][0]['provider']=='brave'
    assert result['candidates'][0]['authority']=='web_candidate'
    result=PhotoSearch(settings,'stock-org',fetcher).search('Coast',provider='pexels')
    assert result['candidates'][0]['credit']=='Synthetic Author / Pexels'
    assert result['candidates'][0]['authority']=='destination_stock'
    assert result['candidates'][0]['license_url']=='https://www.pexels.com/license/'


def test_worker_auto_photo_search_uses_only_public_destination_and_preserves_approval_gate(case,source_settings,monkeypatch):
    from app import pipeline
    from app.ai_images import ContextualPlan
    settings,source,_,plan=case
    plan=ContextualPlan(**plan.model_dump(),media_context={'places':[{'kind':'attraction',
        'name':'Universal Orlando','locality':None,'source_block_ids':['p1-b1']}],'primary_index':0})
    settings=replace(settings,media_mode='search',media_sources_file=source_settings.media_sources_file)
    result=PhotoSearch(settings,'a',FakeSource()).search('Coast')
    queries=[]
    class Discovery:
        def __init__(self,*args,**kwargs):
            pass
        def search(self,subject,**kwargs):
            queries.append(subject)
            return result
    monkeypatch.setattr(pipeline,'PhotoSearch',Discovery)
    monkeypatch.setattr(ai_images,'OpenAI',lambda **k:SimpleNamespace(responses=SimpleNamespace(
        create=lambda **kw:SimpleNamespace(status='completed',output_text=plan.model_dump_json(),usage=None))))
    with TestClient(create_app(settings)) as client:
        auth={'Authorization':'Bearer reviewer'}
        uploaded=client.post('/v2/documents',headers={**auth,'Idempotency-Key':'auto-photo'},
            files={'input_pdf':('fixture.pdf',source.read_bytes(),'application/pdf')})
        assert uploaded.status_code==202
        assert process_next(settings)
        route='/v2/documents/'+uploaded.json()['document_id']
        review=client.get(route+'/review',headers=auth).json()
        assert queries==['Universal Orlando']
        assert review['checks']['photo']['authority']=='official'
        assert not review['checks']['photo']['permission_confirmed']
        assert all(c['passed'] for c in review['checks']['template_text'])
        assert client.get(route+'/pdf',headers=auth).status_code==409
