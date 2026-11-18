"""Responsive CDN source regressions with synthetic identities and photo bytes."""
import io
from dataclasses import replace

from PIL import Image
import pytest

from app.media import PhotoPage,PhotoSearch,MediaCatalog,srcset_entries,image_variants
from app.media_fetch import Fetched,MediaError
from test_media import source_settings,photo


def test_srcset_preserves_internal_commas_and_prefers_declared_larger_sources():
    small='https://cdn.example.test/image/upload/c_fill,f_avif,w_320/v1/sample.jpg'
    large='https://cdn.example.test/image/upload/c_fill,f_avif,w_1280/v1/sample.jpg'
    assert srcset_entries(small+' 320w, '+large+' 1280w')==[(small,320),(large,1280)]
    page=PhotoPage('<section class="hero"><source srcset="'+small+' 320w, '+large+' 1280w"></section>',
                   'https://attraction.example.test/')
    scores={url:max(b for u,a,b in page.photos if u==url) for url in (small,large)}
    assert scores[large]>scores[small]
    assert not any('/f_avif' in u and not '/image/upload/' in u for u,a,b in page.photos)


@pytest.mark.parametrize('text,expected',[
    ('/one.jpg, /two.jpg',[('/one.jpg',0),('/two.jpg',0)]),
    ('/one.jpg 1x, /two.jpg 2x',[('/one.jpg',800),('/two.jpg',1600)]),
    ('/one.jpg 0w, /two.jpg nope',[]),
    ('/one.jpg 20w 2x',[]),
    ('/one.jpg?q=a,b 1200w',[('/one.jpg?q=a,b',1200)]),
    ('/one.jpg, /two.jpg 2x, /three.jpg',[('/one.jpg',0),('/two.jpg',1600),('/three.jpg',0)])])
def test_srcset_token_boundaries(text,expected):
    assert srcset_entries(text)==expected


def test_original_asset_variant_keeps_host_version_identity_and_url_guards():
    original='https://cdn.example.test/image/upload/v1/client/exterior.jpg'
    cropped='https://cdn.example.test/image/upload/c_fill,f_avif,h_600,w_320/v1/client/exterior.jpg'
    assert image_variants(cropped)==[original,cropped]
    signed='https://cdn.example.test/image/upload/s--synthetic--/c_fill,w_320/v1/exterior.jpg'
    assert image_variants(signed)==[signed]
    arbitrary='https://cdn.example.test/image/upload/some_asset/my_photo.jpg'
    assert image_variants(arbitrary)==[arbitrary]


def test_static_avif_is_normalized_and_animated_avif_is_blocked(source_settings):
    source={'page_url':'https://attraction.example.test/','title':'Example Adventure Park',
        'publisher':'Tourism Board','entity':'Example Adventure Park','provider':'website','authority':'web_candidate'}
    output=io.BytesIO()
    first=Image.new('RGB',(1500,750),'#c4dce5')
    first.save(output,format='AVIF')
    catalog=MediaCatalog(source_settings,'org')
    saved=catalog.save_photo(Fetched('https://cdn.example.test/image.avif','image/avif',output.getvalue()),source,65)
    with Image.open(catalog.load(saved['id'])['path']) as decoded:
        assert decoded.format=='JPEG' and decoded.size==(1500,750)
    output=io.BytesIO()
    first.save(output,format='AVIF',save_all=True,append_images=[Image.new('RGB',first.size,'#fff')],duration=100)
    with pytest.raises(MediaError) as error:
        catalog.save_photo(Fetched('https://cdn.example.test/animated.avif','image/avif',output.getvalue()),source,65)
    assert error.value.code=='MEDIA_FORMAT_BLOCKED'


def test_uncatalogued_attraction_resolves_original_without_requesting_srcset_fragments(source_settings):
    empty=source_settings.data_dir.parent/'empty.json'
    empty.write_text('{"sources":[]}')
    settings=replace(source_settings,media_sources_file=empty,media_openai_search_enabled=True)
    original='https://cdn.example.test/image/upload/v1/example/exterior.jpg'
    cropped='https://cdn.example.test/image/upload/c_fill,f_avif,w_320/v1/example/exterior.jpg'
    larger='https://cdn.example.test/image/upload/c_fill,f_avif,w_1280/v1/example/exterior.jpg'
    class Source:
        def __init__(self):self.calls=[]
        def get(self,url,**kwargs):
            self.calls.append(url)
            if url=='https://tourism.example.test/parks/example/':
                return Fetched(url,'text/html',('<title>Example Adventure Park</title><p>Test City</p>'
                    '<section class="hero"><source srcset="'+cropped+' 320w, '+larger+' 1280w">'
                    '<img src="'+cropped+'" alt="Example Adventure Park exterior"></section>').encode())
            if url==original:
                return Fetched(url,'image/jpeg',photo())
            raise AssertionError('Unexpected or fragmented image request: '+url)
    fetcher=Source()
    search=PhotoSearch(settings,'org',fetcher)
    search.web.discover=lambda place:[{'page_url':'https://tourism.example.test/parks/example/'}]
    result=search.search('Example Adventure Park',locality='Test City',kind='attraction',provider='openai')
    assert fetcher.calls==['https://tourism.example.test/parks/example/',original]
    assert len(result['candidates'])==1 and result['candidates'][0]['image_url']==original
    assert result['candidates'][0]['authority']=='web_candidate'
