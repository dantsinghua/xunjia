"""Add only the synthetic production-app integration service to this project."""
import json
from pathlib import Path
import subprocess
import yaml

ROOT=Path('/tmp/xunjia-dify-verify-20261013')
REPO=Path(__file__).resolve().parents[2]
PROJECT='xunjia-dify-verify-20261013'


def main():
    if not (ROOT/'root-app-key.json').exists():raise ValueError('Import and publish the integration app first')
    config_path=ROOT/'compose.yml';config=yaml.safe_load(config_path.read_text())
    if config['name']!=PROJECT:raise ValueError('project mismatch')
    if not (ROOT/'tls/root-backend.crt').exists():
        subprocess.run(['openssl','req','-newkey','rsa:2048','-nodes','-keyout',str(ROOT/'tls/root-backend.key'),'-out',str(ROOT/'tls/root-backend.csr'),'-subj','/CN=root_backend'],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        (ROOT/'tls/root-backend.ext').write_text('subjectAltName=DNS:root_backend\nbasicConstraints=CA:FALSE\n')
        subprocess.run(['openssl','x509','-req','-in',str(ROOT/'tls/root-backend.csr'),'-CA',str(ROOT/'tls/server.crt'),'-CAkey',str(ROOT/'tls/server.key'),'-CAcreateserial','-out',str(ROOT/'tls/root-backend.crt'),'-days','7','-extfile',str(ROOT/'tls/root-backend.ext')],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    config['services']['root_backend']={'image':'langgenius/dify-api:1.17.1','pull_policy':'never','restart':'no','user':'0:0',
        'entrypoint':['/app/api/.venv/bin/python','/source/dify/verify_1171/root_app_server.py'],'working_dir':'/source','mem_limit':'512m','cpus':1,
        'environment':{'PYTHONPATH':'/source/.venv/lib/python3.12/site-packages:/source'},
        'volumes':[str(REPO)+':/source:ro',str(ROOT)+':/verify:rw'],'ports':['127.0.0.1:0:8000']}
    config_path.write_text(yaml.safe_dump(config,sort_keys=False));config_path.chmod(0o600)
    print('Prepared root_backend with fresh synthetic SQLite and fake transport')


if __name__=='__main__':main()
