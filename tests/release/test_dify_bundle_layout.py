"""Generated A/B contracts work from shipped and installed directory layouts.

Only payload files are copied to owned temporary directories; no archive,
installation, model, service or ARM runtime is executed.
"""
from functools import lru_cache
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from deploy.build_offline import input_files
from dify.negotiation_v22.build import build


ROOT = Path(__file__).resolve().parents[2]
PROVIDER = 'CONFIGURE_MODEL_PROVIDER'
MODEL = 'CONFIGURE_MODEL_NAME'


@pytest.fixture(scope='module')
def layouts(tmp_path_factory):
    parent = tmp_path_factory.mktemp('dify-real-layouts')
    source_root = ROOT
    if (ROOT/'app/server').is_dir() and not (ROOT/'server').is_dir():
        # This release test itself also runs inside an extracted package.
        # Recover the source-shaped view from its actual shipped files, then
        # exercise the same real input_files mapping and all eight assertions.
        source_root = parent/'source-view'
        source_root.mkdir()
        shutil.copytree(ROOT/'app/server', source_root/'server')
        shutil.copytree(ROOT/'dify', source_root/'dify')
    payload = {relative: source for relative, source in input_files(source_root).items()
               if relative.startswith('app/server/') or relative.startswith('dify/')}
    assert all('app/server/negotiation/'+name in payload
               for name in ('text_roles.py', 'slots.py', 'reply_render.py'))
    result = {}
    for name in ('bundle', 'installed'):
        root = parent/name
        root.mkdir()
        for relative, source in payload.items():
            if name == 'installed' and relative.startswith('app/'):
                relative = relative[len('app/'):]
            target = root/relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
        result[name] = root
    assert not (result['bundle']/'server').exists()
    assert (result['bundle']/'app/server/negotiation/slots.py').is_file()
    assert (result['installed']/'server/negotiation/slots.py').is_file()
    return result


@lru_cache(maxsize=4)
def expected(stage, version):
    return build(stage, version=version, provider=PROVIDER, model=MODEL)


@pytest.mark.parametrize('layout', ['bundle', 'installed'])
@pytest.mark.parametrize('stage', ['analysis', 'reply'])
@pytest.mark.parametrize('version', ['0.15.3', '1.17.1'])
def test_fresh_process_builds_identical_graphs_without_workspace_imports(layouts, layout, stage, version):
    root = layouts[layout]
    output = root/('actual-'+stage+'-'+version+'.json')
    program = """
import json,sys
from pathlib import Path
root=Path(sys.argv[1]).resolve()
sys.path[:0]=[str(root/'app'),str(root)]
assert sys.flags.isolated and sys.flags.optimize==0
from dify.negotiation_v22.build import build
import dify.negotiation_v22.build as builder
import server.negotiation.slots as slots
origins={'build':str(Path(builder.__file__).resolve()),'slots':str(Path(slots.__file__).resolve())}
assert all(Path(path).is_relative_to(root) for path in origins.values())
document=build(sys.argv[2],version=sys.argv[3],provider='CONFIGURE_MODEL_PROVIDER',model='CONFIGURE_MODEL_NAME')
Path(sys.argv[4]).write_text(json.dumps({'document':document,'origins':origins},ensure_ascii=False),encoding='utf8')
"""
    completed = subprocess.run([sys.executable, '-I', '-B', '-c', program,
        str(root), stage, version, str(output)], cwd=root, capture_output=True, text=True, timeout=30)
    assert completed.returncode == 0, completed.stderr
    actual = json.loads(output.read_text(encoding='utf8'))
    assert actual['document'] == expected(stage, version)
    assert Path(actual['origins']['build']) == root/'dify/negotiation_v22/build.py'
    wanted_slots = root/('app/server/negotiation/slots.py' if layout == 'bundle' else 'server/negotiation/slots.py')
    assert Path(actual['origins']['slots']) == wanted_slots
