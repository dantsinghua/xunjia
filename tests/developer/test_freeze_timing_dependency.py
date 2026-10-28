"""A frozen model runner must import its timing dependency without workspace code."""
from pathlib import Path
import subprocess
import sys
from dify.negotiation_v22.freeze import freeze_sources


def test_frozen_chatflow_imports_from_its_own_complete_snapshot(tmp_path):
    root=Path(__file__).resolve().parents[2]
    frozen=tmp_path/'source'
    freeze_sources(root,frozen)
    code='''import sys
from pathlib import Path
root=Path(sys.argv[1]).resolve()
sys.path.insert(0,str(root))
import server.negotiation.chatflow as flow
import relay.file_gateway as gateway
assert all(Path(m.__file__).resolve().is_relative_to(root) for m in (flow,gateway))
'''
    done=subprocess.run([sys.executable,'-I','-B','-c',code,str(frozen)],
                        cwd=tmp_path,capture_output=True,text=True,timeout=30)
    assert done.returncode==0,done.stderr
