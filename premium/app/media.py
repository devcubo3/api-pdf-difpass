"""Contextual photo discovery, private catalog and provenance for HTML documents.

Pages are discovered by a source directory, Brave or opt-in OpenAI web search.
Pexels is an explicit destination-photo alternative.
Downloaded photos are candidates for internal drafts, never proof of usage rights.
"""
from __future__ import annotations

import hashlib
import html
from html.parser import HTMLParser
import io
import json
from pathlib import Path
import re
import threading
import time
import unicodedata
from urllib.parse import urlencode,urljoin,urlsplit,unquote

from PIL import Image,ImageOps,UnidentifiedImageError

from .media_fetch import MediaError,PublicFetcher,public_url
from .storage import Storage
from .media_context import safe_public
from .media_web import WebDiscovery

SEARCH_LOCK=threading.RLock()
SOURCE_DIRECTORY=Path(__file__).resolve().parents[1]/'assets/media/sources.json'


def normalized(text):
    value=unicodedata.normalize('NFKD',str(text)).casefold()
    return re.sub(r'[^a-z0-9]+',' ',''.join(c for c in value if not unicodedata.combining(c))).strip()


def srcset_entries(value):
    """Parse URL tokens before descriptors; commas inside a URL are not separators.

    Cloud image transformations routinely contain commas. A plain split(',')
    creates nonexistent relative URLs and exhausts the bounded download budget.
    """
    position=0
    entries=[]
    whitespace=' \t\n\r\f'
    while position<len(value) and len(entries)<300:
        while position<len(value) and value[position] in whitespace+',':
            position+=1
        start=position
        while position<len(value) and value[position] not in whitespace:
            position+=1
        url=value[start:position]
        if not url:
            break
        descriptors=''
        if url.endswith(','):
            url=url.rstrip(',')
        else:
            start=position
            depth=0
            while position<len(value):
                char=value[position]
                if char==',' and depth==0:
                    break
                depth+=int(char=='(')-int(char==')' and depth>0)
                position+=1
            descriptors=value[start:position].strip()
            position+=1
        width=0
        tokens=descriptors.split()
        if tokens:
            if len(tokens)!=1:
                continue
            match=re.fullmatch(r'([0-9]{1,7})w',tokens[0])
            density=re.fullmatch(r'([0-9]{1,3}(?:\.[0-9]{1,3})?)x',tokens[0])
            if match and int(match[1])>0:
                width=int(match[1])
            elif density and float(density[1])>0:
                width=round(float(density[1])*800)
            else:
                continue
        if url:
            entries.append((url,width))
    return entries


class PhotoPage(HTMLParser):
    def __init__(self,markup,url):
        super().__init__(convert_charrefs=True)
        self.url=url
        self.photos=[]
        self.links=[]
        self.title=''
        self.text=[]
        self.text_size=0
        self.stack=[]
        self.script_type=None
        self.script_data=[]
        self.in_title=False
        self.feed(markup)
        # WordPress/Elementor background photos may live in JSON/data settings.
        decoded=html.unescape(markup).replace('\\/','/')
        for candidate in re.findall(r'https://[^\s\"\'<>\\]+\.(?:jpe?g|png|webp|avif)(?:\?[^\s\"\'<>\\]*)?',decoded,re.I):
            self.photos.append((candidate,'',0))

    def handle_starttag(self,tag,attributes):
        attributes={key:(value or '') for key,value in attributes}
        marker=attributes.get('id','')+' '+attributes.get('class','')+' '+attributes.get('role','')
        if tag=='script':
            self.script_type=attributes.get('type','')
            self.script_data=[]
        if tag not in {'img','source','meta','link','br','hr','input','area','base','embed','param','track','wbr'}:
            self.stack.append((tag,marker))
        scope=normalized(' '.join(value for _,value in self.stack[-10:]))
        blocked=bool(re.search(r'\b(?:logo|footer|nav|cookie|related|recommend|offer|promo|room|suite)\w*\b',scope))
        gallery=bool(re.search(r'gallery|galeria|photosmosaic|hero|masthead',scope))
        base=-1000 if blocked else (65 if gallery else 5)
        context=' [gallery]' if gallery and not blocked else ''
        if tag=='title':
            self.in_title=True
        if tag=='meta' and attributes.get('property',attributes.get('name','')).lower() in {'og:image','twitter:image','og:image:url'}:
            self.photos.append((urljoin(self.url,attributes.get('content','')),'cover',35))
        if tag in {'img','source'}:
            alt=attributes.get('alt','')+context
            for name in ('src','data-src','data-lazy-src','data-original'):
                if attributes.get(name):
                    self.photos.append((urljoin(self.url,attributes[name]),alt,base))
            for name in ('srcset','data-srcset'):
                for candidate,width in srcset_entries(attributes.get(name,'')):
                    self.photos.append((urljoin(self.url,candidate),alt,
                                        base+(min(12,width/200) if base>=0 else 0)))
        for candidate in re.findall(r'url\([\s\"\']*([^\)\"\']+)',attributes.get('style',''),re.I):
            self.photos.append((urljoin(self.url,candidate.strip()),context,base))
        if tag=='a' and attributes.get('href'):
            self.links.append(urljoin(self.url,attributes['href']))

    def handle_endtag(self,tag):
        if tag=='script':
            raw=''.join(self.script_data)
            try:
                if self.script_type in {'application/json','application/ld+json'}:
                    self.json_photos(json.loads(raw))
                else:
                    # Static JSON assignment only; never evaluate JavaScript.
                    match=re.search(r'(?:window\.)?__(?:INITIAL_STATE|NEXT_DATA|NUXT)__\s*=\s*(?=[\[{])',raw)
                    if match:
                        self.json_photos(json.JSONDecoder().raw_decode(raw[match.end():])[0])
            except (ValueError,RecursionError):
                pass
            self.script_type=None
            self.script_data=[]
        for i in range(len(self.stack)-1,-1,-1):
            if self.stack[i][0]==tag:
                del self.stack[i:]
                break
        if tag=='title':
            self.in_title=False

    def handle_data(self,value):
        if self.script_type is not None:
            self.script_data.append(value[:2_000_000])
        if self.text_size<250000:
            chunk=value[:10000]
            self.text.append(chunk)
            self.text_size+=len(chunk)
        if self.in_title:
            self.title+=value

    def json_photos(self,value):
        remaining=10000
        def walk(node,scope='',identity='',depth=0):
            nonlocal remaining
            remaining-=1
            if remaining<0 or depth>25:
                return
            if isinstance(node,dict):
                module=' '.join(str(node.get(k,''))[:120] for k in ('module','name','title','@type'))
                context=normalized(scope+' '+module)[-500:]
                if re.search(r'promo|offer|banner|accommodation|quarto|room|menu|footer|testimonial|partner|icons|optin',context):
                    return
                types=node.get('@type')
                types=[types] if isinstance(types,str) else (types if isinstance(types,list) else [])
                if any(isinstance(t,str) and t in {'Hotel','Resort','LodgingBusiness','TouristAttraction'} for t in types):
                    identity=str(node.get('name',''))[:150]
                gallery=bool(re.search(r'gallery|galeria|mosaico|amenitiesslider',context))
                description=' '.join(str(node.get(k,''))[:150] for k in ('alt','altText','caption','title'))
                for key,item in node.items():
                    if isinstance(item,str) and re.fullmatch(r'(?:image|imageDesktop|imageMobile|url|contentUrl|src)',key,re.I):
                        if re.match(r'https://',item) and re.search(r'\.(?:jpe?g|png|webp|avif)(?:\?|$)',item,re.I) and (gallery or identity):
                            self.photos.append((item,identity+' '+description+(' [gallery]' if gallery else ''),60))
                    elif isinstance(item,(dict,list)):
                        walk(item,context,identity,depth+1)
            elif isinstance(node,list):
                for item in node[:300]:
                    walk(item,scope,identity,depth+1)
        walk(value)


def matches_entity(subject,title):
    excluded={'hotel','resort','all','inclusive','e','de','do','da','dos','das','the'}
    terms=[word for word in normalized(subject).split() if word not in excluded and len(word)>2]
    actual=set(normalized(title).split())
    return bool(terms) and all(term in actual for term in terms)


def photo_score(url,alt,base=0):
    text=normalized(urlsplit(url).path+' '+(alt or ''))
    if any(word in text for word in ('logo','favicon','icon','placeholder','sprite','youtube','mapa','cardapio','whatsapp','oferta','promoc','cupom')) or re.search(r'\bapp\b|\bdemo\b|aplicativo',text):
        return -1000
    score=base
    for word in ('home','praia','aerea','aerial','panoram','fachada','vista','resort','piscina','capa'):
        if word in text:
            score+=10
    if any(word in text for word in ('fachada','exterior','aerea','aerial','panoram','vista hotel')):
        score+=25
    for word in ('mobile','quarto','suite','apartamento','acomodac','gastronom','restaurante','familia','pessoas'):
        if word in text:
            score-=35
    return score


def photo_identity(subject,locality,url,alt,base,authority):
    """A matching page may contain unrelated properties and global sharing images."""
    if base<0:
        return False
    if '[gallery]' in alt:
        return True
    if authority in {'official','reviewer_supplied'}:
        return True
    generic={'hotel','resort','fazenda','pousada','the','de','do','da','dos','das','all','inclusive'}
    location=set(normalized(locality or '').split())
    terms=[w for w in normalized(subject).split() if w not in generic|location and len(w)>2]
    evidence=normalized(urlsplit(url).path+' '+alt)
    return bool(terms) and all(word in evidence.split() for word in terms)


def image_variants(url):
    """A public CDN thumbnail can expose its original public URL in its path."""
    path=unquote(urlsplit(url).path)
    match=re.search(r'https://[^\s]+\.(?:jpe?g|png|webp|avif)$',path,re.I)
    result=[]
    if match:
        try:
            result.append(public_url(match[0]))
        except MediaError:
            pass
    # Public Cloudinary-compatible delivery URLs expose the original asset ID.
    # Remove only recognized transformation segments, keeping version/asset path.
    # Never alter signed URLs or ask for a larger generated derivative.
    if '/image/upload/' in path and '/s--' not in path:
        prefix,remainder=path.split('/image/upload/',1)
        segments=remainder.split('/')
        removed=False
        while len(segments)>1 and all(re.fullmatch(
                r'(?:a|ar|b|bo|c|co|dpr|e|f|fl|g|h|o|q|r|w|x|y|z)_[^/]+',part)
                for part in segments[0].split(',')):
            segments.pop(0)
            removed=True
        if removed and re.search(r'\.(?:jpe?g|png|webp|avif)$',segments[-1],re.I):
            parts=urlsplit(url)
            original=parts._replace(path=prefix+'/image/upload/'+'/'.join(segments)).geturl()
            result.append(public_url(original))
    return list(dict.fromkeys(result+[url]))


class MediaCatalog:
    def __init__(self,settings,tenant_id):
        self.settings=settings
        self.store=Storage(settings.data_dir)
        self.tenant=hashlib.sha256(tenant_id.encode()).hexdigest()
        self.root=self.store._contained(self.store.root/'media'/self.tenant)
        self.root.mkdir(parents=True,exist_ok=True)
        (self.root/'searches').mkdir(exist_ok=True)

    def load(self,asset_id):
        if not isinstance(asset_id,str) or not re.fullmatch(r'[0-9a-f]{64}',asset_id):
            raise MediaError('MEDIA_NOT_FOUND','Foto não encontrada para esta organização.')
        metadata_path=self.store._contained(self.root/f'{asset_id}.json')
        try:
            metadata=json.loads(metadata_path.read_text(encoding='utf-8'))
            if not isinstance(metadata,dict) or any(not isinstance(metadata.get(k),str)
                for k in ('id','sha256','source_page_url','credit','provider','authority','rights_status','publisher')):
                raise ValueError()
            public_url(metadata['source_page_url'])
            path=self.store._contained(self.root/f'{asset_id}.jpg')
            if metadata['id']!=asset_id or hashlib.sha256(path.read_bytes()).hexdigest()!=metadata['sha256']:
                raise ValueError()
        except (OSError,ValueError,KeyError,TypeError):
            raise MediaError('MEDIA_INTEGRITY','A foto está indisponível ou sua integridade mudou.') from None
        return {**metadata,'path':str(path)}

    @staticmethod
    def public(metadata):
        return {key:value for key,value in metadata.items() if key!='path'}

    def save_photo(self,fetched,source,score):
        host=urlsplit(fetched.url).hostname or ''
        if host in {'googleusercontent.com','ggpht.com','googleapis.com'} or host.endswith(('.googleusercontent.com','.ggpht.com','.googleapis.com')):
            raise MediaError('MEDIA_GOOGLE_STORAGE_BLOCKED','Fotos do Google Maps não entram no catálogo permanente de PDFs.')
        if fetched.content_type not in {'image/jpeg','image/png','image/webp','image/avif'}:
            raise MediaError('MEDIA_FORMAT_BLOCKED','Formato de imagem não permitido.')
        try:
            with Image.open(io.BytesIO(fetched.content)) as original:
                if (original.format not in {'JPEG','PNG','WEBP','AVIF'} or getattr(original,'n_frames',1)!=1
                        or original.width*original.height>30_000_000):
                    raise ValueError()
                image=ImageOps.exif_transpose(original).convert('RGB')
                width,height=image.size
                if width<900 or height<350 or not 1.3<=width/height<=3.5:
                    raise MediaError('MEDIA_RESOLUTION','Foto sem proporção ou resolução adequada ao cabeçalho.')
                image.thumbnail((2800,2000),Image.Resampling.LANCZOS)
                output=io.BytesIO()
                image.save(output,format='JPEG',quality=94,optimize=True)
                content=output.getvalue()
        except MediaError:
            raise
        except (UnidentifiedImageError,OSError,ValueError,Image.DecompressionBombError):
            raise MediaError('MEDIA_FORMAT_BLOCKED','A fonte não retornou uma fotografia válida.') from None
        source={**source,'page_url':public_url(source['page_url'])}
        digest=hashlib.sha256(content).hexdigest()
        asset_id=hashlib.sha256((self.tenant+source['page_url']+digest).encode()).hexdigest()
        metadata={'id':asset_id,'sha256':digest,'original_sha256':hashlib.sha256(fetched.content).hexdigest(),
            'image_url':fetched.url,'source_page_url':source['page_url'],'source_title':source['title'][:200],
            'publisher':source['publisher'][:100],'entity':source['entity'],'provider':source['provider'],
            'authority':source['authority'],'rights_status':source.get('rights_status','permission_pending'),
            'credit':source.get('credit',source['publisher'])[:100],
            'photographer':source.get('photographer'), 'license_url':source.get('license_url'),
            'image_context':source.get('image_context','')[:200],
            'width':image.width,'height':image.height,'original_width':width,'original_height':height,
            'score':round(score+min(width,2800)/150,2),'fetched_at':int(time.time())}
        Storage._write_atomic(self.root/f'{asset_id}.jpg',content)
        Storage._write_atomic(self.root/f'{asset_id}.json',json.dumps(metadata,ensure_ascii=False).encode())
        return metadata


class PhotoSearch:
    def __init__(self,settings,tenant_id,fetcher=None,*,db=None,row=None,allow_paid=True):
        self.settings=settings
        self.catalog=MediaCatalog(settings,tenant_id)
        self._fetcher=fetcher
        self.web=WebDiscovery(settings,db,row,self.catalog.root/'searches',allow_paid=allow_paid)

    @property
    def fetcher(self):
        # The bounded download clock starts after an optional paid web lookup.
        if self._fetcher is None:
            self._fetcher=PublicFetcher()
        return self._fetcher

    def source_for(self,subject):
        path=self.settings.media_sources_file
        try:
            sources=json.loads(Path(path).read_text(encoding='utf-8'))['sources']
        except (OSError,ValueError,KeyError):
            raise MediaError('MEDIA_SOURCES_CONFIG','Catálogo de fontes de imagens inválido.') from None
        for source in sources:
            names=source.get('aliases',[])+[source['entity']]
            if normalized(subject) in {normalized(alias) for alias in names if len(normalized(alias))>2}:
                return source
        return None

    def discover_pages(self,subject,source_url,provider,locality=None,kind=None):
        known=self.source_for(subject)
        if source_url:
            url=public_url(source_url)
            official=known and urlsplit(url).hostname==urlsplit(known['page_url']).hostname
            return [{'page_url':url,'publisher':known['publisher'] if official else urlsplit(url).hostname,
                     'entity':known['entity'] if official else subject,
                     'authority':'official' if official else 'reviewer_supplied','provider':'website'}]
        if known and provider in {'auto','official'}:
            return [{**known,'provider':'official_site'}]
        if provider=='official':
            raise MediaError('MEDIA_SOURCE_REQUIRED','Cadastre a fonte oficial deste destino ou informe sua página pública.')
        if provider in {'auto','brave'} and self.settings.brave_search_api_key:
            query=urlencode({'q':' '.join(filter(None,[subject,locality,'site oficial fotos'])),'count':5})
            response=self.fetcher.get('https://api.search.brave.com/res/v1/web/search?'+query,
                headers={'X-Subscription-Token':self.settings.brave_search_api_key})
            try:
                results=json.loads(response.content).get('web',{}).get('results',[])
            except ValueError:
                raise MediaError('MEDIA_PROVIDER_RESPONSE','A busca retornou uma resposta inválida.') from None
            return [{'page_url':public_url(item['url']),'publisher':urlsplit(item['url']).hostname,
                     'entity':subject,'authority':'web_candidate','provider':'brave'} for item in results
                    if matches_entity(subject,item.get('title',''))][:3]
        if provider in {'auto','openai'} and self.settings.media_openai_search_enabled:
            pages=self.web.discover({'name':subject,'locality':locality,'kind':kind or 'place'})
            return [{**item,'publisher':urlsplit(item['page_url']).hostname,
                     'entity':subject,'authority':'web_candidate','provider':'openai_web_search'}
                    for item in pages]
        if provider=='pexels' and self.settings.pexels_api_key:
            return []  # Pexels candidates are retrieved by its image endpoint below.
        raise MediaError('MEDIA_SEARCH_NOT_CONFIGURED','Informe uma fonte oficial ou configure Brave Search/Pexels no ambiente privado.')

    def search(self,subject,source_url=None,provider='auto',*,locality=None,kind=None):
        subject=' '.join(subject.split())
        if not safe_public(subject) or (locality and not safe_public(locality)):
            raise MediaError('MEDIA_CONTEXT_INVALID','Informe apenas o nome público do destino, hotel ou atração.')
        if provider not in {'auto','official','brave','pexels','openai'}:
            raise MediaError('MEDIA_PROVIDER_INVALID','Provedor de busca inválido.')
        source_url=public_url(source_url) if source_url else None
        source_digest=hashlib.sha256(Path(self.settings.media_sources_file).read_bytes()).hexdigest()
        key=hashlib.sha256(json.dumps([normalized(subject),source_url,provider,source_digest,
            normalized(locality or ''),kind,'context-v6-srcset-avif-originals'],ensure_ascii=False).encode()).hexdigest()
        cache=self.catalog.root/'searches'/f'{key}.json'
        with SEARCH_LOCK:
            if cache.is_file():
                saved=json.loads(cache.read_text(encoding='utf-8'))
                if time.time()-saved['created_at']<86400:
                    for candidate in saved['candidates']:
                        self.catalog.load(candidate['id'])
                    return {**saved,'cache_hit':True,'search_usage':{'cache_hit':True,'new_calls':0,'reserved_estimate_usd':0}}
            pages=self.discover_pages(subject,source_url,provider,locality,kind)
            candidates=[]
            errors=[]
            if not pages and self.settings.pexels_api_key and provider=='pexels':
                query=urlencode({'query':subject,'orientation':'landscape','per_page':4})
                response=self.fetcher.get('https://api.pexels.com/v1/search?'+query,
                                         headers={'Authorization':self.settings.pexels_api_key})
                for item in json.loads(response.content).get('photos',[])[:4]:
                    source={'page_url':item['url'],'publisher':'Pexels','title':item.get('alt',subject),
                        'entity':subject,'provider':'pexels','authority':'destination_stock',
                        'rights_status':'provider_license_review','credit':item['photographer']+' / Pexels',
                        'photographer':item['photographer'],'license_url':'https://www.pexels.com/license/'}
                    try:
                        photo=self.fetcher.get(item['src'].get('large2x',item['src']['original']),max_bytes=8_000_000)
                        candidates.append(self.catalog.save_photo(photo,source,0))
                    except MediaError as exc:
                        errors.append(exc.code)
            for source in pages[:5]:
                try:
                    response=self.fetcher.get(source['page_url'])
                    if response.content_type not in {'text/html','application/xhtml+xml'}:
                        continue
                    parsed=PhotoPage(response.content.decode('utf-8',errors='replace'),response.url)
                    if not matches_entity(source['entity'],parsed.title):
                        errors.append('MEDIA_ENTITY_MISMATCH')
                        continue
                    if locality and source['authority']!='official' and not matches_entity(locality,parsed.title+' '+' '.join(parsed.text)):
                        errors.append('MEDIA_LOCALITY_MISMATCH')
                        continue
                    redirected=(urlsplit(response.url).hostname.removeprefix('www.')
                                !=urlsplit(source['page_url']).hostname.removeprefix('www.'))
                    source={**source,'page_url':response.url,'title':parsed.title}
                    if redirected:
                        source.update(authority='redirected_candidate',publisher=urlsplit(response.url).hostname)
                    ranked={}
                    for url,alt,base in parsed.photos:
                        try:
                            url=public_url(url)
                        except MediaError:
                            continue
                        score=photo_score(url,alt,base)
                        if not photo_identity(subject,locality,url,alt,base,source['authority']):
                            continue
                        for variant in image_variants(url):
                            priority=score+(3 if variant!=url else 0)
                            if score>=0 and priority>ranked.get(variant,(-1000,''))[0]:
                                ranked[variant]=(priority,alt)
                    families=set()
                    attempts=0
                    for url,(score,alt) in sorted(ranked.items(),key=lambda item:item[1][0],reverse=True):
                        family=image_variants(url)[0]
                        if family in families:
                            continue
                        if attempts>=8:
                            break
                        attempts+=1
                        try:
                            photo=self.fetcher.get(url,max_bytes=8_000_000)
                            candidates.append(self.catalog.save_photo(photo,{**source,'image_context':alt},score))
                            families.add(family)
                        except MediaError as exc:
                            errors.append(exc.code)
                        if len(candidates)>=4:
                            break
                except MediaError as exc:
                    errors.append(exc.code)
                if len(candidates)>=4:
                    break
            candidates=list({c['id']:c for c in candidates}.values())
            candidates.sort(key=lambda item:item['score'],reverse=True)
            if not candidates:
                raise MediaError('MEDIA_NO_MATCH','Nenhuma foto correspondente com resolução adequada foi encontrada.')
            result={'subject':subject,'locality':locality,'kind':kind,'entity':candidates[0]['entity'],'provider':provider,'created_at':int(time.time()),
                    'google_maps_url':'https://www.google.com/maps/search/?'+urlencode({'api':1,'query':candidates[0]['entity']}),
                    'candidates':candidates[:4],'cache_hit':False,'diagnostics':sorted(set(errors)),
                    'search_usage':self.web.usage or {'new_calls':0,'reserved_estimate_usd':0}}
            Storage._write_atomic(cache,json.dumps(result,ensure_ascii=False).encode())
            return result


def choose_candidate(result,document_id):
    candidates=result['candidates']
    eligible=[c for c in candidates if c['score']>=candidates[0]['score']-8]
    index=int(hashlib.sha256(document_id.encode()).hexdigest()[:8],16)%len(eligible)
    return eligible[index]
