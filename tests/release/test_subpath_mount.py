"""Independent mount transaction acceptance: temporary files, fake nginx/HTTP only."""
import importlib.util
import io
import json
from pathlib import Path
import shutil
import sys
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture
def mount():
    spec = importlib.util.spec_from_file_location('independent_mount_xunjia', REPO/'deploy/mount_xunjia.py')
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


@pytest.fixture
def layout(tmp_path):
    config = tmp_path/'etc/nginx'; (config/'conf.d').mkdir(parents=True)
    (config/'nginx.conf').write_text(f'events {{}}\nhttp {{\n include {config}/conf.d/*.conf;\n}}\n')
    site = config/'conf.d/njcb.conf'
    original = b'# South Bank original: keep exact bytes\r\nserver {\r\n listen 80;\r\n server_name _;\r\n root /synthetic/njcb;\r\n location / { try_files $uri $uri/ /index.html; }\r\n location /api { proxy_pass http://127.0.0.1:8000; }\r\n location /ws { proxy_pass http://127.0.0.1:8000; }\r\n}\r\n'
    site.write_bytes(original); site.chmod(0o640)
    snippets = tmp_path/'bundle/deploy'; snippets.mkdir(parents=True)
    for name in ('nginx-xunjia-locations.conf','nginx-xunjia-map.conf'):
        shutil.copy2(REPO/'deploy'/name, snippets/name)
    return SimpleNamespace(config=config, site=site, original=original, snippets=snippets, backup=tmp_path/'prefix/state/nginx-mount')


def snapshot(layout):
    return {str(p.relative_to(layout.config)): (p.read_bytes(), p.stat().st_mode & 0o777) for p in layout.config.rglob('*') if p.is_file()}


def test_plan_only_and_apply_preserve_every_original_route_byte_and_repeat_safely(mount, layout):
    before = snapshot(layout)
    _, raw, files = mount.plan(layout.config, layout.snippets)
    assert raw == layout.original and snapshot(layout) == before
    inserted = ('\r\n    include '+str(layout.config/'xunjia-locations.conf')+';').encode()
    assert files[layout.site].replace(inserted, b'', 1) == layout.original
    events=[]
    first=mount.apply(layout.config,layout.snippets,layout.backup,lambda op:events.append(op),lambda phase:events.append(phase))
    assert events == ['test','before','test','reload','after']
    assert first['status']=='XUNJIA_MOUNT_READY' and len(first['changed_files'])==3
    assert layout.site.stat().st_mode & 0o777 == 0o640
    applied=snapshot(layout)
    second=mount.apply(layout.config,layout.snippets,layout.backup,lambda op:None,lambda phase:None)
    assert second['changed_files']==[] and snapshot(layout)==applied
    assert layout.site.read_bytes().count(inserted)==1
    assert (Path(first['backup'])/'njcb.conf').read_bytes()==layout.original
    assert Path(first['backup']).stat().st_mode & 0o777 == 0o700


@pytest.mark.parametrize('target',['conf.d/xunjia-map.conf','xunjia-locations.conf'])
def test_foreign_existing_snippet_rejected_without_write(mount,layout,target):
    path=layout.config/target;path.write_bytes(b'# unrelated prior operator material\n')
    before=snapshot(layout)
    with pytest.raises(mount.MountError,match='EXISTING_XUNJIA_SNIPPET_DIFFERS'):
        mount.apply(layout.config,layout.snippets,layout.backup,lambda op:pytest.fail('runner called'),lambda phase:pytest.fail('HTTP called'))
    assert snapshot(layout)==before and not layout.backup.exists()


@pytest.mark.parametrize('target',['nginx.conf','conf.d/njcb.conf','conf.d/xunjia-map.conf','xunjia-locations.conf'])
def test_config_or_target_symlink_rejected(mount,layout,tmp_path,target):
    path=layout.config/target; external=tmp_path/'external'
    external.write_bytes(path.read_bytes() if path.exists() else b'protected external bytes')
    if path.exists():path.unlink()
    path.symlink_to(external)
    original=external.read_bytes()
    with pytest.raises(mount.MountError,match='SYMLINK'):
        mount.plan(layout.config,layout.snippets)
    assert path.is_symlink() and external.read_bytes()==original


@pytest.mark.parametrize('extra',['\nserver { listen 80; }\n',' server { listen 81; }'])
def test_multiple_server_blocks_rejected_even_same_line(mount,layout,extra):
    layout.site.write_bytes(layout.original.rstrip()+extra.encode())
    with pytest.raises(mount.MountError,match='UNIQUE_EXISTING_PORT80_SITE_REQUIRED'):
        mount.plan(layout.config,layout.snippets)


@pytest.mark.parametrize('fail_at',['initial-test','before-http','new-test','reload','after-http'])
def test_failures_restore_exact_files_modes_and_do_not_leave_new_fragments(mount,layout,fail_at):
    before=snapshot(layout);events=[];test_calls=0;reload_calls=0
    def runner(op):
        nonlocal test_calls,reload_calls
        events.append(op)
        if op=='test':
            test_calls+=1
            if (fail_at=='initial-test' and test_calls==1) or (fail_at=='new-test' and test_calls==2):raise mount.MountError('SYNTHETIC_TEST_FAILURE')
        if op=='reload':
            reload_calls+=1
            if fail_at=='reload' and reload_calls==1:raise mount.MountError('SYNTHETIC_RELOAD_FAILURE')
    def check(phase):
        events.append(phase)
        if fail_at==phase+'-http':raise mount.MountError('SYNTHETIC_HTTP_FAILURE')
    with pytest.raises(mount.MountError,match='SYNTHETIC'):
        mount.apply(layout.config,layout.snippets,layout.backup,runner,check)
    assert snapshot(layout)==before
    if fail_at in ('initial-test','before-http'):
        assert not layout.backup.exists() and 'reload' not in events
    else:
        assert events[-2:]==['test','reload']
        results=list(layout.backup.glob('mount-*/RESULT.json'))
        assert len(results)==1 and json.loads(results[0].read_text())['status']=='ROLLED_BACK'


@pytest.fixture
def main_env(mount,layout,tmp_path,monkeypatch):
    """Redirect each fixed production path; no real /etc, /proc or HTTP is read."""
    binary=tmp_path/'nginx';binary.write_bytes(b'synthetic executable identity')
    pidfile=tmp_path/'pid';pidfile.write_text('424242')
    proc=tmp_path/'proc';(proc/'424242').mkdir(parents=True)
    (proc/'424242/cmdline').write_bytes((str(binary)+'\0-c\0'+str(layout.config/'nginx.conf')+'\0').encode())
    expected={'version':'synthetic-fixed-version','source_hash':'a'*64}
    installed=tmp_path/'installed.json';installed.write_text(json.dumps(expected))
    mapping={'/etc/nginx':layout.config,'/run/nginx.pid':pidfile,'/proc':proc,
             '/home/xunjia/app/frontend/dist/assets/build-version.json':installed,'/home/xunjia/state/nginx-mount':layout.backup}
    def safe_path(value,*parts):
        return Path(mapping.get(str(value),value),*parts)
    monkeypatch.setattr(mount,'Path',safe_path)
    monkeypatch.setattr(mount,'__file__',str(layout.snippets/'mount_xunjia.py'))
    monkeypatch.setattr(mount.os,'geteuid',lambda:0)
    monkeypatch.setattr(mount.os,'readlink',lambda path:str(binary) if path==proc/'424242/exe' else pytest.fail('unexpected readlink'))
    monkeypatch.setattr(mount.shutil,'which',lambda name:str(binary) if name=='nginx' else None)
    monkeypatch.setitem(sys.modules,'common',SimpleNamespace(verify_bundle=lambda root:{'frontend_build':dict(expected)}))
    monkeypatch.setattr(sys,'argv',['mount_xunjia.py','--apply'])
    calls=[]
    monkeypatch.setattr(mount,'nginx_run',lambda binary,main,op:calls.append(op))
    state={'prefix':dict(expected),'backend':dict(expected),'ticks':0}
    monkeypatch.setattr(mount,'http_read',lambda path,host:(200,b'unchanged synthetic South Bank HTML' if path=='/' else json.dumps(state['prefix']).encode()))
    monkeypatch.setattr(mount.urllib.request,'build_opener',lambda *args:SimpleNamespace(open=lambda *args,**kw:io.BytesIO(json.dumps(state['backend']).encode())))
    monkeypatch.setattr(mount.time,'monotonic',lambda:state['ticks'])
    def sleep(seconds):
        state['ticks']+=seconds
        if state['ticks']>20:raise RuntimeError('INDEPENDENT_BOUND_GUARD')
    monkeypatch.setattr(mount.time,'sleep',sleep)
    return SimpleNamespace(**vars(layout),state=state,calls=calls,proc=proc,binary=binary,expected=expected)


def test_check_only_never_runs_nginx_or_http(mount,main_env,monkeypatch,capsys):
    monkeypatch.setattr(sys,'argv',['mount_xunjia.py'])
    before=snapshot(main_env)
    mount.main()
    assert json.loads(capsys.readouterr().out)['status']=='CHECK_ONLY'
    assert not main_env.calls and snapshot(main_env)==before and not main_env.backup.exists()


def test_normal_main_checks_whole_bundle_installed_build_and_prefix(mount,main_env,capsys):
    mount.main()
    assert json.loads(capsys.readouterr().out)['status']=='XUNJIA_MOUNT_READY'
    assert main_env.calls==['test','test','reload']


def test_nul_separated_running_nginx_custom_config_is_rejected_before_writes(mount,main_env):
    (main_env.proc/'424242/cmdline').write_bytes((str(main_env.binary)+'\0-c\0/foreign/nginx.conf\0').encode())
    before=snapshot(main_env)
    with pytest.raises(mount.MountError,match='RUNNING_NGINX_CONFIG_MISMATCH'):
        mount.main()
    assert snapshot(main_env)==before and not main_env.calls


def test_valid_json_wrong_prefix_version_is_bounded_and_rolled_back(mount,main_env):
    main_env.state['prefix']['version']='synthetic-other-release'
    before=snapshot(main_env)
    with pytest.raises(mount.MountError,match='PREFIX_HTTP_CHECK_FAILED'):
        mount.main()
    assert snapshot(main_env)==before
    assert main_env.calls[-2:]==['test','reload']
    assert main_env.state['ticks']<=15


@pytest.mark.parametrize('phase',['backend','prefix'])
def test_same_version_wrong_source_hash_is_not_ready(mount,main_env,phase):
    main_env.state[phase]['source_hash']='b'*64
    before=snapshot(main_env)
    with pytest.raises(mount.MountError,match='VERSION_MISMATCH|PREFIX_HTTP_CHECK_FAILED'):
        mount.main()
    assert snapshot(main_env)==before


def test_bundle_verification_failure_prevents_any_runtime_or_write(mount,main_env,monkeypatch):
    def reject(root): raise mount.MountError('SYNTHETIC_BUNDLE_INVALID')
    monkeypatch.setitem(sys.modules,'common',SimpleNamespace(verify_bundle=reject))
    before=snapshot(main_env)
    with pytest.raises(mount.MountError,match='SYNTHETIC_BUNDLE_INVALID'):
        mount.main()
    assert snapshot(main_env)==before and not main_env.calls and not main_env.backup.exists()


def test_installed_frontend_source_mismatch_prevents_any_runtime_or_write(mount,main_env,tmp_path):
    installed=tmp_path/'installed.json'
    installed.write_text(json.dumps({**main_env.expected,'source_hash':'c'*64}))
    before=snapshot(main_env)
    with pytest.raises(mount.MountError,match='INSTALL_THIS_FRONTEND_BEFORE_MOUNTING'):
        mount.main()
    assert snapshot(main_env)==before and not main_env.calls and not main_env.backup.exists()


@pytest.mark.parametrize('option',['-p\0/synthetic/foreign-prefix','-p/synthetic/foreign-prefix'])
def test_running_nginx_custom_prefix_is_rejected_before_any_write(mount,main_env,option):
    (main_env.proc/'424242/cmdline').write_bytes((str(main_env.binary)+'\0'+option+'\0-c\0'+str(main_env.config/'nginx.conf')+'\0').encode())
    before=snapshot(main_env)
    with pytest.raises(mount.MountError,match='RUNNING_NGINX_PREFIX_UNREVIEWED'):
        mount.main()
    assert snapshot(main_env)==before and not main_env.calls and not main_env.backup.exists()


@pytest.mark.parametrize('role',['worker process','cache manager process','helper process'])
def test_worker_cache_or_helper_pid_never_authorizes_reload(mount,main_env,role):
    (main_env.proc/'424242/cmdline').write_bytes(('nginx: '+role+'\0').encode())
    before=snapshot(main_env)
    with pytest.raises(mount.MountError,match='NGINX_MASTER_REQUIRED'):
        mount.main()
    assert snapshot(main_env)==before and not main_env.calls and not main_env.backup.exists()


@pytest.mark.parametrize('compiled',['expected','foreign','missing'])
def test_no_c_requires_confirmed_compiled_config_path(mount,main_env,monkeypatch,capsys,compiled):
    (main_env.proc/'424242/cmdline').write_bytes(('nginx: master process '+str(main_env.binary)+'\0').encode())
    probe=[]
    def run(argv,**kwargs):
        probe.append(argv)
        assert argv==[str(main_env.binary),'-V'] and kwargs['timeout']==15
        value=str(main_env.config/'nginx.conf') if compiled=='expected' else '/synthetic/foreign/nginx.conf'
        return SimpleNamespace(returncode=0,stdout='',stderr='nginx version: synthetic\nconfigure arguments: '+('' if compiled=='missing' else '--conf-path='+value))
    monkeypatch.setattr(mount.subprocess,'run',run)
    before=snapshot(main_env)
    if compiled=='expected':
        mount.main()
        assert json.loads(capsys.readouterr().out)['status']=='XUNJIA_MOUNT_READY'
    else:
        with pytest.raises(mount.MountError,match='RUNNING_NGINX_CONFIG_MISMATCH|RUNNING_NGINX_CONFIG_UNVERIFIED'):
            mount.main()
        assert snapshot(main_env)==before and not main_env.calls and not main_env.backup.exists()
    assert len(probe)==1


def test_attached_c_correct_config_retains_safe_supported_startup(mount,main_env,capsys):
    (main_env.proc/'424242/cmdline').write_bytes((str(main_env.binary)+'\0-c'+str(main_env.config/'nginx.conf')+'\0').encode())
    mount.main()
    assert json.loads(capsys.readouterr().out)['status']=='XUNJIA_MOUNT_READY'
