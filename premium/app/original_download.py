"""Narrow, revocable source capability embedded in a PDF, never a backend API key."""
import hashlib
import secrets
import time
from datetime import datetime,timezone


def issue_original_download(settings,db,row):
    if row['data_json'].get('legacy_pdf_url'):
        from .legacy_download import validate_legacy_url
        url=validate_legacy_url(settings,row['tenant_id'],row['data_json']['legacy_pdf_url'])
        return {'url':url,'expires_at':None,'expires_label':None,'kind':'legacy_pdf'}
    if settings.require_legacy_pdf_url:
        return None
    if not settings.public_base_url:
        return None
    token=secrets.token_urlsafe(32)
    expires=time.time()+settings.original_link_ttl_days*86400
    db.create_access_link(row,token_sha256=hashlib.sha256(token.encode()).hexdigest(),
        artifact='original_download',page_number=None,artifact_sha256=row['source_sha'],
        disposition='attachment',expires_at=expires,subject='premium-worker')
    return {'url':settings.public_base_url+'/v2/access/'+token,
            'expires_at':datetime.fromtimestamp(expires,timezone.utc).isoformat(),
            'expires_label':datetime.fromtimestamp(expires,timezone.utc).strftime('%d/%m/%Y')}
