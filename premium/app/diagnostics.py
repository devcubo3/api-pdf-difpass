"""Error locations without exception strings, source lines, locals or credentials."""
from pathlib import Path
import traceback


def safe_error(exc,stage):
    frames=[{'module':Path(frame.filename).name,'line':frame.lineno,'function':frame.name}
            for frame in traceback.extract_tb(exc.__traceback__) if Path(frame.filename).parent.name=='app']
    return {'stage':stage,'kind':type(exc).__name__,'frames':frames[-8:]}
