"""Validated runtime configuration. Secrets never appear in dataclass reprs."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Settings:
    data_dir: Path = Path("var")
    database_url: str = field(default="sqlite:///var/state.db", repr=False)
    tokens_json: str = field(default="{}", repr=False)
    max_upload_bytes: int = 20 * 1024 * 1024
    max_pages: int = 50
    render_dpi: int = 240
    worker_timeout_seconds: int = 300
    review_policy: str = "always"
    openai_api_key: str | None = field(default=None, repr=False)
    render_mode: str = 'local'
    pipeline_version: str = 'legacy'
    max_output_pages: int = 100
    legacy_pdf_hosts_json: str = field(default='{}', repr=False)
    require_legacy_pdf_url: bool = False
    html_browser_channel: str | None = None
    html_engine: str = 'chromium'
    public_base_url: str | None = None
    original_link_ttl_days: int = 365
    media_mode: str = 'brand'
    media_sources_file: Path = Path(__file__).resolve().parents[1] / 'assets/media/sources.json'
    brave_search_api_key: str | None = field(default=None,repr=False)
    pexels_api_key: str | None = field(default=None,repr=False)
    media_openai_search_enabled: bool = False
    media_search_reserve_usd: float = .05
    media_search_daily_budget_usd: float = .10
    media_auto_catalog_json: str = field(default='{}', repr=False)
    openai_text_model: str = 'gpt-6-luna'
    openai_image_model: str = 'gpt-image-2'
    openai_image_quality: str = 'high'
    openai_image_size: str = '1536x2176'
    ai_max_pages: int = 16
    ai_request_timeout_seconds: int = 180
    ai_document_budget_usd: float = 12.0
    ai_daily_budget_usd: float = 24.0
    brand_dir: Path = Path(__file__).resolve().parents[1] / "assets" / "brand"

    def __post_init__(self) -> None:
        object.__setattr__(self, "data_dir", Path(self.data_dir))
        object.__setattr__(self, "brand_dir", Path(self.brand_dir))
        object.__setattr__(self, 'media_sources_file', Path(self.media_sources_file))
        if self.public_base_url:
            value=self.public_base_url.rstrip('/')
            parsed=urlsplit(value)
            if (parsed.scheme not in {'http','https'} or not parsed.hostname or parsed.username or parsed.password
                    or parsed.path or parsed.query or parsed.fragment or any(c.isspace() for c in value)
                    or (parsed.scheme=='http' and parsed.hostname not in {'127.0.0.1','localhost','::1'})):
                raise ValueError('PDF_PUBLIC_BASE_URL requires an HTTPS origin (HTTP loopback only for local tests)')
            object.__setattr__(self,'public_base_url',value)
        if not isinstance(self.original_link_ttl_days,int) or not 1<=self.original_link_ttl_days<=1095:
            raise ValueError('Invalid original download link lifetime')
        if self.media_mode not in {'brand','search'}:
            raise ValueError('media_mode must be brand or search')
        if not .02 <= self.media_search_reserve_usd <= .20:
            raise ValueError('Invalid media search reservation')
        if not self.media_search_reserve_usd<=self.media_search_daily_budget_usd<=100:
            raise ValueError('Invalid daily media search budget')
        for name in ("max_upload_bytes", "max_pages", "render_dpi", "worker_timeout_seconds"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if not 120 <= self.render_dpi <= 300:
            raise ValueError("render_dpi must be between 120 and 300")
        if self.review_policy not in {'always','validated_auto'}:
            raise ValueError('review_policy (PDF_REVIEW_POLICY) must be always or validated_auto')
        if self.review_policy=='validated_auto' and (self.render_mode!='html_template' or self.pipeline_version!='canonical_v1'):
            raise ValueError('validated_auto requires html_template and canonical_v1')
        self.media_auto_catalog
        if self.render_mode not in {'local', 'openai_images', 'html_template'}:
            raise ValueError('render_mode must be local, openai_images or html_template')
        if self.pipeline_version not in {'legacy','canonical_v1'}:
            raise ValueError('Unsupported pipeline version')
        if self.pipeline_version=='canonical_v1' and self.render_mode!='html_template':
            raise ValueError('canonical_v1 requires html_template')
        if not 1<=self.max_output_pages<=200:
            raise ValueError('Invalid output page limit')
        try:
            hosts=json.loads(self.legacy_pdf_hosts_json)
            if not isinstance(hosts,dict) or any(not isinstance(k,str) or not isinstance(v,list)
                or any(not isinstance(h,str) or not h or '/' in h or ':' in h or '*' in h for h in v)
                for k,v in hosts.items()):
                raise ValueError()
        except (TypeError,ValueError):
            raise ValueError('PDF_LEGACY_PDF_HOSTS must map tenant IDs to exact HTTPS hostnames') from None
        if self.html_browser_channel not in {None, 'chrome', 'msedge'}:
            raise ValueError('Unsupported HTML browser channel')
        if self.html_engine not in {'chromium', 'weasyprint'}:
            raise ValueError('PDF_HTML_ENGINE must be chromium or weasyprint')
        if not 1 <= self.ai_max_pages <= 40 or not 30 <= self.ai_request_timeout_seconds <= 300:
            raise ValueError('Invalid AI page/timeout limit')
        if not 0.05 <= self.ai_document_budget_usd <= self.ai_daily_budget_usd <= 1000:
            raise ValueError('Invalid AI budget limits')
        profiles={('gpt-image-2','high','1536x2176'),('gpt-image-2','medium','1024x1536'),
                  ('gpt-image-1-mini','medium','1024x1536')}
        if self.openai_text_model != 'gpt-6-luna' or (
                self.openai_image_model,self.openai_image_quality,self.openai_image_size) not in profiles:
            raise ValueError('AI models must match the documented, budgeted configuration')
        if not self.database_url:
            raise ValueError("DATABASE_URL cannot be empty")
        self.api_tokens  # Validate without printing the secret-bearing input.

    @property
    def media_auto_catalog(self):
        try:
            import re
            catalog=json.loads(self.media_auto_catalog_json)
            if not isinstance(catalog,dict): raise ValueError()
            for tenant,entries in catalog.items():
                if not isinstance(tenant,str) or not tenant or not isinstance(entries,list) or len(entries)>200:
                    raise ValueError()
                for entry in entries:
                    if (not isinstance(entry,dict) or set(entry)!={'asset_id','name','locality'}
                        or not isinstance(entry['asset_id'],str) or not re.fullmatch(r'[0-9a-f]{64}',entry['asset_id'])
                        or not isinstance(entry['name'],str) or not 1<=len(entry['name'])<=250
                        or entry['locality'] is not None and (not isinstance(entry['locality'],str) or not 1<=len(entry['locality'])<=250)):
                        raise ValueError()
            return catalog
        except (ValueError,TypeError):
            raise ValueError('PDF_MEDIA_AUTO_CATALOG requires tenant lists of asset_id, name and locality') from None

    @property
    def api_tokens(self) -> dict[str, dict[str, Any]]:
        try:
            tokens = json.loads(self.tokens_json)
        except (TypeError, ValueError):
            raise ValueError("PDF_API_TOKENS must contain a valid JSON object") from None
        if not isinstance(tokens, dict):
            raise ValueError("PDF_API_TOKENS must be an object")
        allowed_roles = {"upload", "read", "review"}
        for token, identity in tokens.items():
            if not isinstance(token, str) or not token or not isinstance(identity, dict):
                raise ValueError("PDF_API_TOKENS contains an invalid identity")
            if set(identity) != {"tenant_id", "subject", "roles"}:
                raise ValueError("Each identity requires tenant_id, subject and roles")
            for name in ("tenant_id", "subject"):
                value = identity[name]
                if not isinstance(value, str) or not value.strip() or len(value) > 200:
                    raise ValueError("Identity tenant_id and subject must be nonempty strings")
            roles = identity["roles"]
            if not isinstance(roles, list) or not roles or any(
                not isinstance(role, str) or role not in allowed_roles for role in roles
            ):
                raise ValueError("Identity roles must contain upload, read and/or review")
        return tokens

    @classmethod
    def from_env(cls) -> "Settings":
        data_dir = Path(os.getenv("PDF_DATA_DIR", "var"))
        return cls(
            data_dir=data_dir,
            database_url=os.getenv("DATABASE_URL", f"sqlite:///{data_dir.as_posix()}/state.db"),
            tokens_json=os.getenv("PDF_API_TOKENS", "{}"),
            max_upload_bytes=int(os.getenv("PDF_MAX_UPLOAD_BYTES", str(20 * 1024 * 1024))),
            max_pages=int(os.getenv("PDF_MAX_PAGES", "50")),
            render_dpi=int(os.getenv("PDF_RENDER_DPI", "240")),
            worker_timeout_seconds=int(os.getenv("PDF_WORKER_TIMEOUT_SECONDS", "300")),
            review_policy=os.getenv("PDF_REVIEW_POLICY", "always"),
            openai_api_key=os.getenv("OPENAI_API_KEY") or None,
            render_mode=os.getenv('PDF_RENDER_MODE', 'openai_images'),
            pipeline_version=os.getenv('PDF_PIPELINE_VERSION','legacy'),
            max_output_pages=int(os.getenv('PDF_MAX_OUTPUT_PAGES','100')),
            legacy_pdf_hosts_json=os.getenv('PDF_LEGACY_PDF_HOSTS') or '{}',
            require_legacy_pdf_url=os.getenv('PDF_REQUIRE_LEGACY_PDF_URL','false').lower()=='true',
            html_browser_channel=os.getenv('PDF_HTML_BROWSER_CHANNEL') or None,
            html_engine=os.getenv('PDF_HTML_ENGINE', 'chromium'),
            public_base_url=os.getenv('PDF_PUBLIC_BASE_URL') or None,
            original_link_ttl_days=int(os.getenv('PDF_ORIGINAL_LINK_TTL_DAYS','365')),
            media_mode=os.getenv('PDF_MEDIA_MODE','brand'),
            media_sources_file=Path(os.getenv('PDF_MEDIA_SOURCES_FILE',str(cls.__dataclass_fields__['media_sources_file'].default))),
            brave_search_api_key=os.getenv('BRAVE_SEARCH_API_KEY') or None,
            pexels_api_key=os.getenv('PEXELS_API_KEY') or None,
            media_openai_search_enabled=os.getenv('PDF_MEDIA_OPENAI_SEARCH_ENABLED','false').lower()=='true',
            media_search_reserve_usd=float(os.getenv('PDF_MEDIA_SEARCH_RESERVE_USD','.05')),
            media_search_daily_budget_usd=float(os.getenv('PDF_MEDIA_SEARCH_DAILY_BUDGET_USD','.10')),
            media_auto_catalog_json=os.getenv('PDF_MEDIA_AUTO_CATALOG') or '{}',
            openai_text_model=os.getenv('OPENAI_TEXT_MODEL', 'gpt-6-luna'),
            openai_image_model=os.getenv('OPENAI_IMAGE_MODEL', 'gpt-image-2'),
            openai_image_quality=os.getenv('OPENAI_IMAGE_QUALITY','high'),
            openai_image_size=os.getenv('OPENAI_IMAGE_SIZE','1536x2176'),
            ai_max_pages=int(os.getenv('PDF_AI_MAX_PAGES', '16')),
            ai_request_timeout_seconds=int(os.getenv('PDF_AI_REQUEST_TIMEOUT_SECONDS', '180')),
            ai_document_budget_usd=float(os.getenv('PDF_AI_DOCUMENT_BUDGET_USD', '12')),
            ai_daily_budget_usd=float(os.getenv('PDF_AI_DAILY_BUDGET_USD', '24')),
            brand_dir=Path(os.getenv("PDF_BRAND_DIR", str(cls.__dataclass_fields__["brand_dir"].default))),
        )
