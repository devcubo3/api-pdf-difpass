"""Legacy entry adapter for the shared image; original source stays untouched.

Legacy upload filenames become fixed internal names before the original handler
uses them as paths. This prevents a supplier-controlled filename from overwriting
premium storage or the shared process configuration. The pinned legacy FastAPI
dependency object and its schema retain the original field/response contract.
"""
from api_pdf import app

route = next(route for route in app.routes
             if getattr(route, 'path', None) == '/generate-pdf/' and 'POST' in route.methods)
original_handler = route.dependant.call


async def safe_legacy_handler(**values):
    for field, name in (('icon_image', 'icon.png'), ('dif_image', 'logo.png'), ('input_pdf', 'source.pdf')):
        values[field].filename = name
    return await original_handler(**values)


route.dependant.call = safe_legacy_handler
