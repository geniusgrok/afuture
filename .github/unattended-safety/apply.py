"""Temporary bounded source transport, not a deployment or trading command."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile

BRANCH = 'codex/unattended-24x7-20260922'
ROOT = Path(__file__).resolve().parents[2]
BATCH = Path(__file__).resolve().parent


def git(*args: str) -> str:
    return subprocess.check_output(['git', *args], cwd=ROOT, text=True).strip()


def require(ok: bool, message: str) -> None:
    if not ok:
        raise RuntimeError(message)


def blob(data: bytes) -> str:
    return hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()


def main() -> None:
    require(os.environ.get('GITHUB_REPOSITORY') == 'ychenracing/afuture', 'wrong repository')
    require(os.environ.get('GITHUB_REF') == f'refs/heads/{BRANCH}', 'wrong branch')
    head = git('rev-parse', 'HEAD')
    require(head == os.environ['GITHUB_SHA'], 'checkout identity mismatch')
    require(not git('status', '--porcelain'), 'worktree is not clean')
    raw = (BATCH / 'manifest.json').read_bytes()
    require(hashlib.sha256(raw).hexdigest() == '02793ab073721a0cfda45002fcb9dab55d5584d429083287b21cd103fddd7d3e', 'manifest changed')
    manifest = json.loads(raw)
    require(manifest['schema_version'] == 1, 'unknown schema')
    subprocess.run(['git', 'merge-base', '--is-ancestor', manifest['base'], 'HEAD'], cwd=ROOT, check=True)
    transport_changes = git('diff', '--name-only', manifest['base'], 'HEAD').splitlines()
    require(all(p.startswith('.github/unattended-safety/') or p == '.github/workflows/unattended-safety.yml' for p in transport_changes), 'parallel source changes appeared')
    chunks = []
    offset = 0
    for index, part in enumerate(manifest['parts']):
        require(part['path'] == f'part-{index:03d}.patch', 'noncanonical part path')
        data = (BATCH / part['path']).read_bytes()
        require(part['offset'] == offset and len(data) == part['bytes'], 'part length or offset mismatch')
        require(hashlib.sha256(data).hexdigest() == part['sha256'], 'part SHA256 mismatch')
        require(blob(data) == part['git_blob'], 'part Git blob mismatch')
        chunks.append(data)
        offset += len(data)
    patch = b''.join(chunks)
    require(len(patch) == manifest['patch_bytes'], 'patch length mismatch')
    require(hashlib.sha256(patch).hexdigest() == manifest['patch_sha256'], 'patch SHA256 mismatch')
    expected = set()
    for entry in manifest['files']:
        path = entry['path']
        require(path.startswith(('afuture/', 'tests/', 'docs/')) or path == 'README.md', 'out-of-scope source path')
        require('..' not in Path(path).parts and path not in expected, 'unsafe or duplicate path')
        expected.add(path)
        present = git('ls-tree', 'HEAD', '--', path)
        before = present.split()[2] if present else None
        require(before == entry['before'], f'base blob changed: {path}')
    with tempfile.NamedTemporaryFile(suffix='.patch') as out:
        out.write(patch)
        out.flush()
        subprocess.run(['git', 'apply', '--check', '--index', out.name], cwd=ROOT, check=True)
        subprocess.run(['git', 'apply', '--index', out.name], cwd=ROOT, check=True)
    require(set(git('diff', '--cached', '--name-only').splitlines()) == expected, 'unexpected changed paths')
    require(not git('diff', '--name-only'), 'unstaged changes appeared')
    for entry in manifest['files']:
        data = (ROOT / entry['path']).read_bytes()
        require(len(data) == entry['bytes'] and hashlib.sha256(data).hexdigest() == entry['sha256'], f'output mismatch: {entry["path"]}')
        require(blob(data) == entry['after'], f'output Git blob mismatch: {entry["path"]}')
        require(git('rev-parse', ':' + entry['path']) == entry['after'], 'index mismatch')
    require(git('ls-remote', 'origin', f'refs/heads/{BRANCH}').split()[0] == head, 'remote advanced; refuse overwrite')
    git('config', 'user.name', 'github-actions[bot]')
    git('config', 'user.email', '41898282+github-actions[bot]@users.noreply.github.com')
    git('commit', '-m', 'fix: resume exact activation crash and persist critical notifications')
    subprocess.run(['git', 'push', 'origin', f'HEAD:refs/heads/{BRANCH}'], cwd=ROOT, check=True)
    after = git('rev-parse', 'HEAD')
    require(git('ls-remote', 'origin', f'refs/heads/{BRANCH}').split()[0] == after, 'remote ref not verified')
    evidence = Path(os.environ['RUNNER_TEMP']) / 'afuture-applied-source'
    evidence.mkdir()
    (evidence / 'SOURCE_SHA').write_text(after + '\n')
    (evidence / 'git-tree.txt').write_text(git('ls-tree', '-r', '--full-tree', 'HEAD') + '\n')
    (evidence / 'manifest.json').write_bytes(raw)
    subprocess.run(['git', 'archive', '--format=tar.gz', '--output=' + str(evidence / 'source.tar.gz'), 'HEAD'], cwd=ROOT, check=True)
    (evidence / 'SHA256SUMS').write_text(''.join(hashlib.sha256(p.read_bytes()).hexdigest() + '  ' + p.name + '\n' for p in sorted(evidence.iterdir())))
    print(json.dumps({'source_sha': after, 'files_verified': len(expected), 'patch_sha256': manifest['patch_sha256'], 'deployed': False, 'field_month_verified': False}))


if __name__ == '__main__':
    main()
