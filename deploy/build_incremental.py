"""Build a self-contained small incremental capsule from verified offline bundles."""
from __future__ import annotations
import argparse
import base64
import hashlib
import json
import re
from pathlib import Path
import sys
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parent))
import incremental as runtime

SCHEMA_FILES = ('app/server/schema.py', 'sql/oceanbase/001_xunjia_schema_mysql.sql')


OPTIONAL_REPLY_MIGRATION = 'sql/oceanbase/004_message_reply.sql'
QA_HISTORY_MIGRATION = 'sql/oceanbase/005_qa_history.sql'

def compatible_reply_column(source, target, old_files, new_files):
    """Recognize ONLY R6-117's nullable optional column; never execute its DDL.

    Default builds still reject every schema change. The explicit opt-in also
    verifies the generator and exact standalone migration, so it cannot be used
    as a generic permission to delete/change tables, indexes or required columns.
    """
    wrapper = 'app/server/schema.py'
    generator = 'app/server/negotiation/schema.py'
    schema = 'sql/oceanbase/001_xunjia_schema_mysql.sql'
    required = (wrapper, generator, schema)
    if any(name not in old_files or name not in new_files for name in required):
        return False
    if old_files[wrapper] != new_files[wrapper] or OPTIONAL_REPLY_MIGRATION not in new_files:
        return False
    before = (Path(source) / schema).read_text(encoding='utf8')
    after = (Path(target) / schema).read_text(encoding='utf8')
    marker = 'historical INTEGER NOT NULL, reply_json LONGTEXT, UNIQUE(thread_id,event_id)'
    if after.count(marker) != 1 or 'reply_json' in before:
        return False
    table = re.search(r'CREATE TABLE IF NOT EXISTS xj2_message\s*\(.*?;', after, re.S)
    if table is None or marker not in table[0]:
        return False
    if after.replace(marker, 'historical INTEGER NOT NULL, UNIQUE(thread_id,event_id)', 1) != before:
        return False
    old_code = (Path(source) / generator).read_text(encoding='utf8')
    new_code = (Path(target) / generator).read_text(encoding='utf8')
    code_marker = 'historical INTEGER NOT NULL, reply_json {text}, UNIQUE(thread_id,event_id)'
    if new_code.count(code_marker) != 1 or new_code.replace(code_marker,
            'historical INTEGER NOT NULL, UNIQUE(thread_id,event_id)', 1) != old_code:
        return False
    migration = (Path(target) / OPTIONAL_REPLY_MIGRATION).read_text(encoding='utf8')
    statement = ' '.join(re.sub(r'--[^\r\n]*', '', migration).split())
    return statement == 'ALTER TABLE xj2_message ADD COLUMN reply_json LONGTEXT NULL;'

def inspect(root):
    root = Path(root).absolute()
    version = json.loads((root / 'VERSION.json').read_text())
    sha = runtime.digest(root / 'SHA256SUMS.txt')
    names = runtime.verify_tree(root, version['build_id'], sha)
    names['SHA256SUMS.txt'] = sha
    return version, names

def installed_files(files):
    result = {}
    for name, sha in files.items():
        if name.startswith('app/'):
            result[name[4:]] = sha
        elif name.split('/')[0] in ('deploy', 'scripts', 'sql', 'dify', 'frontend') or name == 'VERSION.json':
            result[name] = sha
    return result

def build(baselines, target, output, *, allow_optional_reply_migration=False, automatic_reply_migration=False):
    target, output = Path(target), Path(output)
    if output.exists() or output.with_suffix(output.suffix + '.sha256').exists():
        raise runtime.IncrementalError('OUTPUT_ALREADY_EXISTS')
    version, files = inspect(target)
    if not baselines:
        raise runtime.IncrementalError('BASELINE_REQUIRED')
    sources = []
    optional_reply = False
    for source in baselines:
        info, hashes = inspect(source)
        changed = any(name not in files or hashes.get(name) != files[name] for name in SCHEMA_FILES)
        if changed:
            if not (allow_optional_reply_migration or automatic_reply_migration) or not compatible_reply_column(source, target, hashes, files):
                raise runtime.IncrementalError('SCHEMA_CHANGE_REQUIRES_EXPLICIT_MIGRATION')
            optional_reply = True
        sources.append((Path(source), info, hashes))
    payload = {}
    for name, sha in files.items():
        if any(hashes.get(name) != sha for _, _, hashes in sources):
            payload[name] = base64.b64encode(zlib.compress((target / name).read_bytes(), 9)).decode('ascii')
    capsule = {'schema': 1, 'build_id': version['build_id'], 'manifest_sha256': files['SHA256SUMS.txt'],
               'baselines': {hashes['SHA256SUMS.txt']: info['build_id'] for _, info, hashes in sources},
               'files': files, 'sizes': {name: (target / name).stat().st_size for name in files}}
    if optional_reply:
        capsule['optional_database_migrations'] = [{
            'path': OPTIONAL_REPLY_MIGRATION, 'table': 'xj2_message', 'column': 'reply_json',
            'automatic': False, 'restart_required': True,
            'required_for_feature': 'qidian_quoted_reply_context',
            'without_migration': 'existing messages continue; quoted reply context is not stored',
        }]
    if automatic_reply_migration:
        if not optional_reply or 'deploy/incremental_database.py' not in files:
            raise runtime.IncrementalError('AUTOMATIC_MIGRATION_IMPLEMENTATION_REQUIRED')
        capsule['automatic_reply_migration'] = True
        capsule['installed_files'] = {info['build_id']: installed_files(hashes) for _, info, hashes in sources}
        capsule['installed_files'][version['build_id']] = installed_files(files)
        capsule['optional_database_migrations'][0]['automatic'] = True
    if QA_HISTORY_MIGRATION in files:
        required=('deploy/qa_history_migration.py','deploy/database_upgrade.py','app/server/qa_history.py')
        if any(name not in files for name in required):
            raise runtime.IncrementalError('QA_HISTORY_MIGRATION_IMPLEMENTATION_REQUIRED')
        capsule['automatic_qa_history_migration'] = True
        capsule['installed_files'] = {info['build_id']: installed_files(hashes) for _, info, hashes in sources}
        capsule['installed_files'][version['build_id']] = installed_files(files)
        capsule['qa_history_migration'] = {'path':QA_HISTORY_MIGRATION,'automatic':True,
            'database':'xunjia_business','source_retained':True,'before_application_switch':True}
    template = Path(runtime.__file__).read_text()
    assert template.count('CAPSULE = None') == template.count('PAYLOAD = {}') == 1
    rendered = template.replace('CAPSULE = None', 'CAPSULE = ' + repr(capsule)).replace('PAYLOAD = {}', 'PAYLOAD = ' + repr(payload))
    compile(rendered, str(output), 'exec')
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open('x', encoding='utf8') as stream:
        stream.write(rendered)
    output.chmod(0o700)
    sha = runtime.digest(output)
    output.with_suffix(output.suffix + '.sha256').write_text(sha + '  ' + output.name + '\n')
    return {'script': str(output), 'sha256': sha, 'bytes': output.stat().st_size,
            'build_id': version['build_id'], 'changed_files': len(payload), 'reused_files': len(files) - len(payload),
            'supported_baselines': len(capsule['baselines']), 'database_ddl_executed': False}

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base', type=Path, action='append', required=True)
    parser.add_argument('--target', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--allow-optional-reply-migration', action='store_true',
                        help='Permit only the reviewed nullable reply_json addition; DDL remains manual')
    parser.add_argument('--automatic-reply-migration', action='store_true',
                        help='Apply the reviewed additive reply_json migration before switching the application')
    args = parser.parse_args()
    print(json.dumps(build(args.base, args.target, args.output,
                           allow_optional_reply_migration=args.allow_optional_reply_migration,
                           automatic_reply_migration=args.automatic_reply_migration)))
