"""Organization-scoped automatic photo selection, with explicit use policy.

Matching proves relevance, not copyright ownership. The organization opts in to
using credited matching-source photos; this never invents a provider licence.
"""
from urllib.parse import urlsplit

from .media import MediaCatalog,PhotoSearch,choose_candidate
from .media_context import primary_place
from .media_fetch import MediaError


def select_automatic_photo(settings,row,data,issues,db):
    policy=settings.media_auto_use_policy
    allow_paid=(bool(data.pop('media_refresh_allow_paid',False))
                or not bool(row['data_json'].get('ai_cache_only')))
    place=primary_place(data)
    decision={'policy':policy,'status':'not_selected'}
    data['media_decision']=decision
    data.pop('media_selection',None)
    data.pop('photo_provenance',None)
    reason=None
    try:
        approved=MediaCatalog(settings,row['tenant_id']).preapproved(place)
        if approved:
            decision.update(status='selected',basis='configured_catalog',asset_id=approved['id'])
            return {'asset_id':approved['id'],'permission_confirmed':True,'approval_scope':'configured_catalog'}
    except MediaError as exc:
        decision['catalog_error']=exc.code
    if settings.media_mode!='search':
        reason='MEDIA_SEARCH_DISABLED'
    elif policy!='matched_sources':
        reason='MEDIA_AUTO_USE_NOT_ENABLED'
    elif not place or data.get('document_type')=='proposal':
        reason='MEDIA_CONTEXT_REVIEW'
    else:
        try:
            result=PhotoSearch(settings,row['tenant_id'],db=db,row=row,
                allow_paid=allow_paid).search(
                    place['name'],locality=place.get('locality'),kind=place.get('kind'))
            data['media_search']=result
            eligible=[candidate for candidate in result['candidates']
                if candidate.get('authority') in {'official','web_candidate'}
                or candidate.get('authority')=='destination_stock' and place.get('kind')=='destination']
            if not eligible:
                raise MediaError('MEDIA_AUTO_SOURCE_REVIEW','A fonte encontrada exige conferência da equipe.')
            candidate=choose_candidate({**result,'candidates':eligible},row['id'])
            decision.update(status='selected',basis='organization_matched_sources',
                asset_id=candidate['id'],provider=candidate['provider'],
                source_host=urlsplit(candidate['source_page_url']).hostname,
                search_usage=result.get('search_usage',{}))
            return {'asset_id':candidate['id'],'permission_confirmed':True,
                    'approval_scope':'organization_matched_sources','use_policy':policy}
        except MediaError as exc:
            reason=exc.code
    decision['reason']=reason
    data['hero_asset']='premium-institutional.svg'
    issues.append({'code':'MEDIA_INSTITUTIONAL','blocking':False,
        'message':f'Foto não aplicada ({reason}); arte institucional utilizada.'})
    return None
