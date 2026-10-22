"""Build the small offline wheel imported by the QTrade V2.2 collector host.

Only shared protocol/receiver/transport code is packaged, never the web app,
customer configuration, data, or any native-account automation entrypoint.
Build dependencies must come from the explicitly supplied local wheelhouse.
"""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT=Path(__file__).resolve().parents[1]
FILES=('relay/__init__.py','relay/protocol.py','relay/collector_bridge.py',
       'relay/qtrade_native_port.py','server/__init__.py',
       'server/receiver_ledger.py','server/transports.py',
       'relay/file_gateway.py','server/file_gateway_transport.py',
       'relay/file_gateway_connection.py',
       'relay/gateway_paths.py','relay/institution_codec.py',
       'relay/institution_directory.py','relay/institution_archive.py',
       'server/institution_transport.py')


def build(output, wheelhouse):
    root=ROOT/'app' if (ROOT/'app/server').is_dir() else ROOT
    output=Path(output).resolve();wheelhouse=Path(wheelhouse).resolve()
    if not wheelhouse.is_dir():raise ValueError('Explicit local build wheelhouse is required')
    output.mkdir(parents=True,exist_ok=True)
    if list(output.glob('xunjia_collector_runtime-*.whl')):
        raise ValueError('Collector wheel output already exists; choose a fresh release directory')
    hashes={name:hashlib.sha256((root/name).read_bytes()).hexdigest() for name in FILES}
    with tempfile.TemporaryDirectory(prefix='xunjia-collector-wheel-') as folder:
        stage=Path(folder)
        for name in FILES:
            target=stage/name;target.parent.mkdir(exist_ok=True)
            shutil.copyfile(root/name,target)
        (stage/'server/_collector_build.py').write_text(
            'VERSION = "2.2.9"\nSOURCE_HASHES = '+repr(hashes)+'\n',encoding='utf8')
        (stage/'pyproject.toml').write_text('[build-system]\nrequires=["setuptools==80.9.0","wheel==0.45.1"]\nbuild-backend="setuptools.build_meta"\n')
        (stage/'setup.py').write_text('from setuptools import setup\nsetup(name="xunjia-collector-runtime",version="2.2.9",packages=["relay","server"],python_requires=">=3.11",install_requires=["cryptography>=50.0.2","PyYAML>=6"])\n')
        subprocess.run([sys.executable,'-m','pip','wheel','--no-deps','--no-index',
                        '--find-links',str(wheelhouse),'--wheel-dir',str(output),str(stage)],check=True,timeout=180)
    if hashes!={name:hashlib.sha256((root/name).read_bytes()).hexdigest() for name in FILES}:
        raise RuntimeError('Protocol source changed during wheel build')
    wheel=next(output.glob('xunjia_collector_runtime-*.whl'))
    evidence={'wheel':wheel.name,'sha256':hashlib.sha256(wheel.read_bytes()).hexdigest(),'source_hashes':hashes}
    (output/'collector-runtime-manifest.json').write_text(json.dumps(evidence,indent=2)+'\n')
    print('COLLECTOR_WHEEL_BUILT '+json.dumps(evidence))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',required=True,type=Path)
    parser.add_argument('--wheelhouse',required=True,type=Path)
    args=parser.parse_args();build(args.output,args.wheelhouse)
