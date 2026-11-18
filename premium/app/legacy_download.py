"""Validate an externally stored standard PDF URL without fetching or proxying it."""
import json
from urllib.parse import urlsplit

from .media_fetch import public_url,MediaError


def validate_legacy_url(settings,tenant_id,value):
    if not value: return None
    try:
        url=public_url(value)
        allowed=json.loads(settings.legacy_pdf_hosts_json).get(tenant_id,[])
        host=urlsplit(url).hostname
        if host not in {h.lower() for h in allowed}:
            raise ValueError()
        # URLs may contain signed queries; never remove or log their values.
        return url
    except (ValueError,TypeError,MediaError):
        raise ValueError('URL do PDF padrão exige HTTPS e origem autorizada para esta organização.') from None
