"""Entrypoint compatible with Python isolated mode."""
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
if sys.argv[1]=='api':
    import uvicorn
    uvicorn.run('regtrace.api:create_app',factory=True,host='127.0.0.1',port=8000,log_level='info')
else:
    from regtrace.worker import main
    sys.argv=sys.argv[1:]
    main()
