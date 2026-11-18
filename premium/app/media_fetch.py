"""Bounded public-HTTPS fetching with DNS pinning, no proxies or inherited secrets."""
from __future__ import annotations

from dataclasses import dataclass
import http.client
import ipaddress
import socket
import ssl
import time
from urllib.parse import quote,urljoin, urlsplit, urlunsplit


class MediaError(ValueError):
    def __init__(self,code,message):
        super().__init__(message)
        self.code,self.message=code,message


def public_url(url):
    if not isinstance(url,str) or len(url)>2500 or any(ord(c)<32 for c in url) or '\\' in url:
        raise MediaError('MEDIA_URL_INVALID','Endereço da fonte inválido.')
    try:
        parsed=urlsplit(url)
    except ValueError:
        raise MediaError('MEDIA_URL_INVALID','Endereço da fonte inválido.') from None
    try:
        port=parsed.port
    except ValueError:
        raise MediaError('MEDIA_URL_INVALID','Porta de rede inválida.') from None
    if (parsed.scheme!='https' or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or port not in {None,443}):
        raise MediaError('MEDIA_URL_BLOCKED','A fonte precisa usar HTTPS público sem credenciais.')
    host=parsed.hostname.encode('idna').decode('ascii').lower()
    if host=='localhost' or host.endswith(('.localhost','.local','.internal')) or '.' not in host:
        raise MediaError('MEDIA_URL_BLOCKED','Endereço de rede privado não permitido.')
    try:
        address=ipaddress.ip_address(host)
    except ValueError:
        address=None
    if address and not address.is_global:
        raise MediaError('MEDIA_URL_BLOCKED','Endereço de rede privado não permitido.')
    return urlunsplit(('https',host,quote(parsed.path or '/',safe="/%:@-._~!$&'()*+,;="),
                       quote(parsed.query,safe="%=&?/:@-._~!$'()*+,;"),''))


def public_addresses(host):
    try:
        answers=socket.getaddrinfo(host,443,type=socket.SOCK_STREAM)
        addresses=list(dict.fromkeys(item[4][0] for item in answers))
    except OSError:
        raise MediaError('MEDIA_DNS_FAILED','Não foi possível localizar o servidor de imagens.') from None
    if not addresses or any(not ipaddress.ip_address(ip).is_global for ip in addresses):
        raise MediaError('MEDIA_URL_BLOCKED','A fonte resolveu para um endereço de rede não permitido.')
    return addresses


class PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self,host,address,timeout):
        self.pinned_address=address
        self.tls_context=ssl.create_default_context()
        super().__init__(host,port=443,timeout=timeout,context=self.tls_context)

    def connect(self):
        raw=socket.create_connection((self.pinned_address,443),self.timeout)
        try:
            self.sock=self.tls_context.wrap_socket(raw,server_hostname=self.host)
        except Exception:
            raw.close()
            raise


@dataclass(frozen=True)
class Fetched:
    url: str
    content_type: str
    content: bytes


class PublicFetcher:
    def __init__(self,seconds=45):
        self.deadline=time.monotonic()+seconds
        self.requests=0

    def get(self,url,max_bytes=2_000_000,headers=None):
        url=public_url(url)
        original_host=urlsplit(url).hostname
        for _ in range(4):
            remaining=self.deadline-time.monotonic()
            if remaining<=0 or self.requests>=18:
                raise MediaError('MEDIA_SEARCH_LIMIT','Limite de tempo/requisições da busca atingido.')
            parsed=urlsplit(url)
            address=public_addresses(parsed.hostname)[0]
            connection=PinnedHTTPS(parsed.hostname,address,min(8,remaining))
            self.requests+=1
            request_headers={'User-Agent':'DiffTravelPremiumPDF/2.0 (photo metadata discovery)',
                             'Accept-Encoding':'identity','Accept':'*/*'}
            # Provider credentials are sent only to the fixed provider host,
            # never to an image host or to a redirect on a different origin.
            if headers and parsed.hostname==original_host:
                request_headers.update(headers)
            try:
                connection.request('GET',urlunsplit(('', '',parsed.path,parsed.query,'')),
                                   headers=request_headers)
                response=connection.getresponse()
                if response.status in {301,302,303,307,308}:
                    location=response.getheader('Location')
                    if not location:
                        raise MediaError('MEDIA_FETCH_FAILED','Redirecionamento sem destino.')
                    url=public_url(urljoin(url,location))
                    continue
                if response.status!=200:
                    raise MediaError('MEDIA_FETCH_FAILED','A fonte não disponibilizou o conteúdo solicitado.')
                length=response.getheader('Content-Length')
                if length and int(length)>max_bytes:
                    raise MediaError('MEDIA_TOO_LARGE','Arquivo da fonte acima do limite.')
                if response.getheader('Content-Encoding','identity').lower() not in {'','identity'}:
                    raise MediaError('MEDIA_ENCODING_BLOCKED','Conteúdo comprimido de rede não permitido.')
                chunks=[]
                total=0
                while True:
                    if time.monotonic()>self.deadline:
                        raise MediaError('MEDIA_SEARCH_LIMIT','Prazo de download excedido.')
                    chunk=response.read(min(65536,max_bytes+1-total))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    total+=len(chunk)
                    if total>max_bytes:
                        raise MediaError('MEDIA_TOO_LARGE','Arquivo da fonte acima do limite.')
                return Fetched(url,response.getheader('Content-Type','').split(';')[0].lower(),b''.join(chunks))
            except MediaError:
                raise
            except (OSError,ValueError,http.client.HTTPException):
                raise MediaError('MEDIA_FETCH_FAILED','Falha na consulta da fonte de imagens.') from None
            finally:
                connection.close()
        raise MediaError('MEDIA_REDIRECT_LIMIT','A fonte excedeu o limite de redirecionamentos.')
