"""Prepare only the explicitly authorized isolated verification project."""
import json
import os
from pathlib import Path
import secrets
import subprocess
import yaml

PROJECT='xunjia-dify-verify-20261013'
ROOT=Path('/tmp')/PROJECT
REPO=Path(__file__).resolve().parents[2]


def run(*args):return subprocess.check_output(args,text=True).strip()


def main():
    ROOT.mkdir(exist_ok=True)
    if run('docker','ps','-aq','--filter','label=com.docker.compose.project='+PROJECT):
        raise SystemExit('Project already contains containers; inspect instead of overwriting configuration')
    (ROOT/'tls').mkdir(exist_ok=True)
    secret_path=ROOT/'credentials.json'
    if not secret_path.exists():
        secret_path.write_text(json.dumps({k:secrets.token_urlsafe(30) for k in ('db','redis','app','plugin','inner','sandbox','service','tool','owner_password')},indent=2));secret_path.chmod(0o600)
    secret=json.loads(secret_path.read_text())
    if not (ROOT/'tls/server.key').exists():
        subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-keyout',str(ROOT/'tls/server.key'),'-out',str(ROOT/'tls/server.crt'),'-days','7','-subj','/CN=fixture',
            '-addext','subjectAltName=DNS:fixture','-addext','basicConstraints=critical,CA:TRUE'],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    (ROOT/'tls/trust.pem').write_bytes(Path('/etc/ssl/certs/ca-certificates.crt').read_bytes()+(ROOT/'tls/server.crt').read_bytes())
    (ROOT/'init.sql').write_text('CREATE DATABASE dify_plugin;\n')
    env={
        'DB_TYPE':'postgresql','DB_USERNAME':'postgres','DB_PASSWORD':secret['db'],'DB_HOST':'db_postgres','DB_PORT':'5432','DB_DATABASE':'dify',
        'REDIS_HOST':'redis','REDIS_PORT':'6379','REDIS_PASSWORD':secret['redis'],'REDIS_DB':'0',
        'CELERY_BROKER_URL':'redis://:'+secret['redis']+'@redis:6379/1','CELERY_BACKEND':'redis','CELERY_WORKER_AMOUNT':'1','CELERY_AUTO_SCALE':'false',
        'SECRET_KEY':secret['app'],'INIT_PASSWORD':secret['owner_password'][:24],'MIGRATION_ENABLED':'true','LOG_LEVEL':'INFO',
        'SERVER_WORKER_AMOUNT':'1','SERVER_WORKER_CONNECTIONS':'10','GUNICORN_TIMEOUT':'180','STORAGE_TYPE':'opendal','OPENDAL_SCHEME':'fs','OPENDAL_FS_ROOT':'storage',
        'PLUGIN_DAEMON_URL':'http://plugin_daemon:5002','PLUGIN_DAEMON_KEY':secret['plugin'],'INNER_API_KEY_FOR_PLUGIN':secret['inner'],
        'PLUGIN_DAEMON_TIMEOUT':'120','PLUGIN_MAX_PACKAGE_SIZE':'52428800','CODE_EXECUTION_ENDPOINT':'http://sandbox:8194','CODE_EXECUTION_API_KEY':secret['sandbox'],
        'SSRF_PROXY_HTTP_URL':'','SSRF_PROXY_HTTPS_URL':'','SSRF_DEFAULT_READ_TIME_OUT':'60',
        'SSL_CERT_FILE':'/verify-ca.pem','REQUESTS_CA_BUNDLE':'/verify-ca.pem','CURL_CA_BUNDLE':'/verify-ca.pem',
        'CHECK_UPDATE_URL':'','MARKETPLACE_ENABLED':'true','ENABLE_CHECK_UPGRADABLE_PLUGIN_TASK':'false',
        'ENABLE_EMAIL_CODE_LOGIN':'false','ENABLE_EMAIL_PASSWORD_LOGIN':'true','ENABLE_SOCIAL_OAUTH_LOGIN':'false',
        'ENABLE_OTEL':'false','ENABLE_REQUEST_LOGGING':'false','CONSOLE_CORS_ALLOW_ORIGINS':'*',
        'AGENT_V2_ENABLED':'false','PLUGIN_BASED_TOKEN_COUNTING_ENABLED':'false'}
    trust={'type':'bind','source':str(ROOT/'tls/trust.pem'),'target':'/verify-ca.pem','read_only':True}
    image='langgenius/dify-api:1.17.1'
    api=dict(image=image,pull_policy='never',restart='no',environment=dict(env,MODE='api'),mem_limit='3g',cpus=3,
        volumes=['app_storage:/app/api/storage',trust],ports=['127.0.0.1:0:5001'],
        depends_on={'db_postgres':{'condition':'service_healthy'},'redis':{'condition':'service_healthy'},'init_permissions':{'condition':'service_completed_successfully'}},
        healthcheck={'test':['CMD','curl','-f','http://localhost:5001/health'],'interval':'10s','timeout':'5s','retries':30})
    worker=dict(image=image,pull_policy='never',restart='no',environment=dict(env,MODE='worker',MIGRATION_ENABLED='false'),mem_limit='2g',cpus=2,
        volumes=['app_storage:/app/api/storage',trust],depends_on={'api':{'condition':'service_healthy'}})
    services={
        'init_permissions':dict(image=image,pull_policy='never',user='0:0',entrypoint=['/bin/sh','-c'],command=['chown -R 1001:1001 /app/api/storage'],volumes=['app_storage:/app/api/storage'],restart='no',mem_limit='128m'),
        'db_postgres':dict(image='postgres:15-alpine',pull_policy='never',restart='no',mem_limit='1g',cpus=1,
            environment={'POSTGRES_USER':'postgres','POSTGRES_PASSWORD':secret['db'],'POSTGRES_DB':'dify'},
            volumes=['postgres_data:/var/lib/postgresql/data',str(ROOT/'init.sql')+':/docker-entrypoint-initdb.d/verify.sql:ro'],
            healthcheck={'test':['CMD-SHELL','pg_isready -U postgres -d dify'],'interval':'5s','timeout':'3s','retries':20}),
        'redis':dict(image='redis:6-alpine',pull_policy='never',restart='no',mem_limit='256m',command=['redis-server','--requirepass',secret['redis']],
            environment={'REDISCLI_AUTH':secret['redis']},volumes=['redis_data:/data'],healthcheck={'test':['CMD','redis-cli','ping'],'interval':'5s','timeout':'3s','retries':20}),
        'api':api,'worker':worker,
        'plugin_daemon':dict(image='langgenius/dify-plugin-daemon:0.6.10-local',pull_policy='never',restart='no',mem_limit='2g',cpus=2,
            environment=dict(env,DB_DATABASE='dify_plugin',SERVER_PORT='5002',SERVER_KEY=secret['plugin'],DIFY_INNER_API_URL='http://api:5001',DIFY_INNER_API_KEY=secret['inner'],
                PLUGIN_WORKING_PATH='/app/storage/cwd',PLUGIN_STORAGE_TYPE='local',PLUGIN_STORAGE_LOCAL_ROOT='/app/storage',FORCE_VERIFYING_SIGNATURE='true',
                PYTHON_ENV_INIT_TIMEOUT='600',PLUGIN_MAX_EXECUTION_TIMEOUT='120',PLUGIN_REMOTE_INSTALLING_HOST='0.0.0.0',PLUGIN_REMOTE_INSTALLING_PORT='5003'),
            volumes=['plugin_storage:/app/storage',trust],depends_on={'db_postgres':{'condition':'service_healthy'}}),
        'sandbox':dict(image='langgenius/dify-sandbox:0.2.15',pull_policy='never',restart='no',mem_limit='512m',cpus=1,
            environment={'API_KEY':secret['sandbox'],'GIN_MODE':'release','WORKER_TIMEOUT':'15','ENABLE_NETWORK':'true','SANDBOX_PORT':'8194'},volumes=['sandbox_dependencies:/dependencies']),
        'fixture':dict(image=image,pull_policy='never',restart='no',user='0:0',entrypoint=['/app/api/.venv/bin/python','/source/dify/verify_1171/fixture_server.py'],mem_limit='256m',cpus=1,
            working_dir='/source',environment={'PYTHONPATH':'/source','VERIFY_DATA':'/verify','VERIFY_SERVICE_TOKEN':secret['service'],'VERIFY_TOOL_TOKEN':secret['tool']},
            volumes=[str(REPO)+':/source:ro',str(ROOT)+':/verify:rw'],ports=['127.0.0.1:0:8000'])}
    config={'name':PROJECT,'services':services,'volumes':{name:{} for name in ('app_storage','postgres_data','redis_data','plugin_storage','sandbox_dependencies')}}
    path=ROOT/'compose.yml';path.write_text(yaml.safe_dump(config,sort_keys=False));path.chmod(0o600)
    print(json.dumps({'project':PROJECT,'root':str(ROOT),'services':list(services),'memory_budget':'9.2 GiB','host_ports':'loopback random only','existing_containers_modified':False}))


if __name__=='__main__':main()
