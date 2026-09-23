"""One-shot verified import of saved Git objects; never changes the task branch."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess


def git(*args, **kwargs):
    return subprocess.check_output(['git', *args], **kwargs).decode().strip()


def require(ok, message):
    if not ok:
        raise RuntimeError(message)


def remote(branch):
    lines = git('ls-remote', 'origin', 'refs/heads/' + branch).splitlines()
    require(len(lines) == 1, 'Remote branch absent or ambiguous: ' + branch)
    return lines[0].split()[0]


base = Path(__file__).resolve().parent
manifest = json.loads((base / 'manifest.json').read_text())
out = Path(os.environ['RUNNER_TEMP']) / 'afuture-publish'
out.mkdir(exist_ok=False)
require(manifest['repository'] == os.environ['GITHUB_REPOSITORY'], 'Wrong repository')
require(os.environ['GITHUB_REF_NAME'] == manifest['transfer_branch'], 'Wrong transfer branch')
source = manifest['source_head']
staging = os.environ['GITHUB_SHA']
require(remote(manifest['transfer_branch']) == staging, 'Transfer branch advanced')
require(remote(manifest['task_branch']) == manifest['expected_task_head'], 'Task branch advanced')
for item in manifest['files']:
    offset = 0
    digest = hashlib.sha256()
    target = out / item['path']
    require(target.parent == out, 'Invalid output path')
    with target.open('xb') as stream:
        for part in item['parts']:
            path = base / part['path']
            require(path.parent == base and not path.is_symlink(), 'Invalid chunk path')
            data = path.read_bytes()
            require(offset == part['offset'] and len(data) == part['length'], 'Chunk offset/length mismatch')
            require(hashlib.sha256(data).hexdigest() == part['sha256'], 'Chunk SHA256 mismatch')
            oid = hashlib.sha1(f'blob {len(data)}\0'.encode() + data).hexdigest()
            require(oid == part['git_blob'], 'Chunk Git object mismatch')
            stream.write(data)
            digest.update(data)
            offset += len(data)
    require(offset == item['length'] and digest.hexdigest() == item['sha256'], 'Full payload mismatch')
with (out / 'source-thin.pack').open('rb') as stream:
    imported = git('index-pack', '--strict', '--fix-thin', '--stdin', stdin=stream)
require(git('cat-file', '-t', source) == 'commit', 'Source commit missing')
require(git('rev-parse', source + '^{tree}') == manifest['source_tree'], 'Source tree mismatch')
subprocess.run(['git', 'merge-base', '--is-ancestor', manifest['expected_task_head'], source], check=True)
subprocess.run(['git', 'fsck', '--full', '--no-dangling', source], check=True)
# Record exact Git bytes and an archive for independent local read-back verification.
for kind, oid in [('commit', source), ('tree', manifest['source_tree'])]:
    (out / ('source.' + kind)).write_bytes(subprocess.check_output(['git', 'cat-file', kind, oid]))
subprocess.run(['git', 'archive', '--format=tar.gz', '--output=' + str(out / 'source.tar.gz'), source], check=True)
shutil.copyfile(base / 'manifest.json', out / 'manifest.json')
shutil.copyfile(base / 'publish.py', out / 'publish.py')
# A normal fast-forward on the transfer branch makes the original commits reachable.
# The task branch is advanced separately by its authorized owner connector.
env = dict(os.environ)
for role in ['AUTHOR', 'COMMITTER']:
    env['GIT_' + role + '_NAME'] = 'github-actions[bot]'
    env['GIT_' + role + '_EMAIL'] = '41898282+github-actions[bot]@users.noreply.github.com'
wrapper = git('commit-tree', manifest['source_tree'], '-p', staging, '-p', source,
              input=b'build: preserve verified unattended source objects\n', env=env)
require(remote(manifest['transfer_branch']) == staging, 'Transfer branch changed before push')
require(remote(manifest['task_branch']) == manifest['expected_task_head'], 'Task branch changed before push')
subprocess.run(['git', 'push', 'origin', wrapper + ':refs/heads/' + manifest['transfer_branch']], check=True)
require(remote(manifest['transfer_branch']) == wrapper, 'Transfer push not verified')
receipt = {
    'schema': 1, 'repository': manifest['repository'], 'source_head': source,
    'source_tree': manifest['source_tree'], 'staging_head': staging,
    'transfer_head': wrapper, 'task_branch_unchanged': remote(manifest['task_branch']),
    'run_id': os.environ['GITHUB_RUN_ID'], 'imported_pack': imported,
    'files': {p.name: {'length': p.stat().st_size, 'sha256': hashlib.sha256(p.read_bytes()).hexdigest()}
              for p in out.iterdir() if p.is_file()},
}
(out / 'receipt.json').write_text(json.dumps(receipt, indent=2) + '\n')
print(json.dumps({k: receipt[k] for k in ['source_head', 'source_tree', 'transfer_head', 'task_branch_unchanged']}))
