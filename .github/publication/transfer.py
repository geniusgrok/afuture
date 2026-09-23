"""One-shot publication of a hash-pinned recovery archive; no trading access."""
from __future__ import annotations
import base64
import hashlib
import hmac
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parent
M = json.loads((ROOT / 'manifest.json').read_text())
TMP = Path(os.environ['RUNNER_TEMP']) / 'unattended-publication'
OUT = TMP / 'readback'
OUT.mkdir(exist_ok=True)
REPO = 'ychenracing/afuture'
API = 'https://api.github.com/repos/' + REPO

def git(*args: str, data: bytes | None = None) -> str:
    return subprocess.run(['git', *args], input=data, check=True, capture_output=True).stdout.decode().strip()

def api(path: str) -> dict:
    request = urllib.request.Request(API + path, headers={
        'Authorization': 'Bearer ' + os.environ['GH_TOKEN'],
        'Accept': 'application/vnd.github+json',
        'User-Agent': 'afuture-publication',
    })
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)

def stream(url: str, destination: Path, length: int | None = None) -> str:
    digest = hashlib.sha256()
    total = 0
    try:
        with urllib.request.urlopen(url, timeout=60) as response, destination.open('wb') as output:
            while chunk := response.read(1024 * 1024):
                total += len(chunk)
                if total > (length if length is not None else 64 * 1024 * 1024):
                    raise RuntimeError('Download exceeds pinned size')
                digest.update(chunk)
                output.write(chunk)
    except Exception as exc:
        raise RuntimeError('Download failed: ' + type(exc).__name__) from None
    if length is not None and total != length:
        raise RuntimeError('Download length mismatch')
    return digest.hexdigest()

# The private key is never uploaded. Only this running job can decrypt the URL.
deadline = time.monotonic() + 360
while True:
    try:
        response = api('/contents/.github/publication/envelope.json?ref=' + M['transfer_branch'])
        envelope = json.loads(base64.b64decode(response['content']))
        break
    except urllib.error.HTTPError as exc:
        if exc.code != 404 or time.monotonic() >= deadline:
            raise RuntimeError('Encrypted transfer envelope unavailable') from None
        time.sleep(4)
assert envelope['transaction'] == M['transaction']
pub = (TMP / 'public.pem').read_bytes()
assert hashlib.sha256(pub).hexdigest() == envelope['public_key_sha256']
secret = subprocess.run([
    'openssl', 'pkeyutl', '-decrypt', '-inkey', str(TMP / 'private.pem'),
    '-pkeyopt', 'rsa_padding_mode:oaep', '-pkeyopt', 'rsa_oaep_md:sha256',
], input=base64.b64decode(envelope['wrapped_key']), check=True, capture_output=True).stdout
assert len(secret) == 80
ciphertext = base64.b64decode(envelope['ciphertext'])
mac = hmac.new(secret[32:64], M['transaction'].encode() + ciphertext, hashlib.sha256).hexdigest()
assert hmac.compare_digest(mac, envelope['hmac_sha256'])
decrypted = subprocess.run([
    'openssl', 'enc', '-d', '-aes-256-ctr', '-K', secret[:32].hex(), '-iv', secret[64:].hex(),
], input=ciphertext, check=False, capture_output=True)
assert decrypted.returncode == 0, 'Envelope decryption failed'
url = decrypted.stdout.decode()
parsed = urllib.parse.urlparse(url)
assert parsed.scheme == 'https' and (parsed.hostname or '').endswith('.oaiusercontent.com')
assert parsed.path.startswith('/files/') and not parsed.username
archive = TMP / M['archive_name']
assert stream(url, archive, M['archive_length']) == M['archive_sha256']
del url, secret
(TMP / 'private.pem').unlink()
with zipfile.ZipFile(archive) as z:
    for item in z.infolist():
        assert (TMP / 'unpacked' / item.filename).resolve().is_relative_to(TMP / 'unpacked')
    z.extractall(TMP / 'unpacked')
recovery = TMP / 'unpacked'
for line in (recovery / 'SHA256SUMS').read_text().splitlines():
    expected, name = line.split(maxsplit=1)
    path = recovery / name.lstrip('* ')
    assert path.resolve().is_relative_to(recovery)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == expected
assert (recovery / 'SOURCE_SHA').read_text().strip() == M['source_head']
assert (recovery / 'SOURCE_TREE').read_text().strip() == M['source_tree']
git('bundle', 'unbundle', str(recovery / 'afuture.bundle'))
assert git('rev-parse', M['source_head'] + '^{tree}') == M['source_tree']
git('merge-base', '--is-ancestor', M['expected_target_head'], M['source_head'])
manifest = json.loads((recovery / 'source-manifest.json').read_text())
for item in manifest['files']:
    data = subprocess.run(['git', 'show', M['source_head'] + ':' + item['path']], check=True, capture_output=True).stdout
    assert len(data) == item['length']
    assert hashlib.sha256(data).hexdigest() == item['sha256']
    assert hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest() == item['git_blob']
# Publish only new preservation refs. The PR ref is advanced separately by the connector.
assert git('ls-remote', 'origin', 'refs/heads/' + M['target_branch']).split()[0] == M['expected_target_head']
for ref in (M['source_ref'], M['evidence_ref']):
    assert not git('ls-remote', 'origin', 'refs/heads/' + ref), 'Preservation ref already exists'
archive_blob = git('hash-object', '-w', str(archive))
manifest_blob = git('hash-object', '-w', str(ROOT / 'manifest.json'))
handoff_blob = git('hash-object', '-w', str(recovery / 'HANDOFF_PROMPT.md'))
tree = git('mktree', data=(
    f'100644 blob {archive_blob}\t{M["archive_name"]}\n'
    f'100644 blob {handoff_blob}\tHANDOFF_PROMPT.md\n'
    f'100644 blob {manifest_blob}\tpublication-manifest.json\n'
).encode())
os.environ['GIT_AUTHOR_NAME'] = os.environ['GIT_COMMITTER_NAME'] = 'github-actions[bot]'
os.environ['GIT_AUTHOR_EMAIL'] = os.environ['GIT_COMMITTER_EMAIL'] = '41898282+github-actions[bot]@users.noreply.github.com'
evidence_commit = git('commit-tree', tree, '-m', 'archive: preserve exact afuture unattended recovery originals')
git('push', '--atomic', 'origin', M['source_head'] + ':refs/heads/' + M['source_ref'], evidence_commit + ':refs/heads/' + M['evidence_ref'])
for ref, expected in ((M['source_ref'], M['source_head']), (M['evidence_ref'], evidence_commit)):
    assert git('ls-remote', 'origin', 'refs/heads/' + ref).split()[0] == expected
remote_commit = api('/git/commits/' + M['source_head'])
assert remote_commit['sha'] == M['source_head'] and remote_commit['tree']['sha'] == M['source_tree']
# Actual fresh remote downloads, not reads from the local Git object cache.
remote_archive = OUT / M['archive_name']
assert stream('https://raw.githubusercontent.com/' + REPO + '/' + evidence_commit + '/' + M['archive_name'], remote_archive, M['archive_length']) == M['archive_sha256']
with archive.open('rb') as left, remote_archive.open('rb') as right:
    while chunk := left.read(1024 * 1024):
        assert chunk == right.read(len(chunk))
    assert not right.read(1)
remote_source = OUT / 'source-readback.tar.gz'
source_download_sha256 = stream('https://codeload.github.com/' + REPO + '/tar.gz/' + M['source_head'], remote_source)
with tarfile.open(remote_source) as tar:
    members = {member.name.split('/', 1)[1]: member for member in tar.getmembers() if member.isfile()}
    assert set(members) == {item['path'] for item in manifest['files']}
    for item in manifest['files']:
        data = tar.extractfile(members[item['path']]).read()
        assert len(data) == item['length'] and hashlib.sha256(data).hexdigest() == item['sha256']
        assert hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest() == item['git_blob']
receipt = dict(M, evidence_commit=evidence_commit, evidence_tree=tree, archive_git_blob=archive_blob,
    source_files_verified=len(manifest['files']), remote_archive_byte_equal=True,
    source_download_sha256=source_download_sha256, source_parents=[p['sha'] for p in remote_commit['parents']],
    pr_ref_updated=False, main_merged=False, trading_or_deployment_performed=False)
(OUT / 'publication-receipt.json').write_text(json.dumps(receipt, indent=2) + '\n')
print(json.dumps(receipt, indent=2))
