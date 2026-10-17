"""Launch the installed release with shared, literal-only configuration."""
from __future__ import annotations
import os
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent))
from common import DeployError, effective_env, load_json, read_env, validate_config
def main():
    app=Path(__file__).resolve().parents[1]
    meta=load_json(app/'.deployment.json') if (app/'.deployment.json').is_file() else {}
    prefix=Path(meta.get('prefix') or os.getenv('XUNJIA_PREFIX','/home/xunjia'))
    cfg=Path(os.getenv('XUNJIA_CONFIG_FILE') or prefix/'config/xunjia.env')
    if not cfg.is_file() and not meta:cfg=app/'config/xunjia.env'
    if meta and cfg.resolve()!=(prefix/'config/xunjia.env').resolve():
        raise DeployError('CONFIG_PATH_CONFLICT', 'managed releases must use shared configuration')
    values=effective_env(read_env(cfg),prefix,app)
    missing=validate_config(values)
    if 'XUNJIA_DATABASE_URL' in missing:
        raise DeployError('CONFIG_DATABASE_PASSWORD_MISSING')
    if values.get('XUNJIA_RUN_BACKGROUND')=='1':validate_config(values,ready=True)
    static=Path(values['XUNJIA_WEB_DIST'])
    if not (static/'index.html').is_file() or not (static/'assets').is_dir():
        raise DeployError('STATIC_ASSETS_MISSING')
    transport=values.get('XUNJIA_TRANSPORT_CONFIG','')
    if transport and not Path(transport).is_file():raise DeployError('TRANSPORT_CONFIG_MISSING')
    python=app.parent/'venv/bin/python'
    if not python.is_file():python=prefix/'venv/bin/python'
    if not python.is_file():raise DeployError('RELEASE_PYTHON_MISSING')
    env={k:v for k,v in os.environ.items() if not k.startswith(('XUNJIA_','QB_'))}
    env.update(values)
    env.update(PYTHONPATH=str(app),PYTHONNOUSERSITE='1',PYTHONDONTWRITEBYTECODE='1')
    args=[str(python),'-m','uvicorn','server.app:create_app','--factory','--workers','1',
          '--host',values['XUNJIA_HOST'],'--port',values['XUNJIA_PORT']]
    os.chdir(app)
    os.execve(str(python),args,env)
if __name__=='__main__':
    try:main()
    except DeployError as exc:
        print(str(exc),file=sys.stderr);raise SystemExit(2)