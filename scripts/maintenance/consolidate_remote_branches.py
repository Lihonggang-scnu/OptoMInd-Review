"""Recoverable branch-name cleanup. Default: verify only; --apply deletes listed old refs.

Uses the caller's existing Git authentication. Never reads or creates credentials.
Keeps main, lihonggang-dev and archive/history without writing their refs.
Deletion-target movement aborts the atomic delete; retained refs are checked
before and after, and concurrent retained-ref changes require review.
Fetch and push destinations must exactly match REPO; URL rewrites are refused.
"""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import uuid

REPO = 'https://github.com/Lihonggang-scnu/OptoMInd-Review.git'
ARCHIVE = '311796aba823e8041aa250402293a1230a1f27c4'
KEEP = {'main', 'lihonggang-dev', 'archive/history'}
ROOT = Path(__file__).resolve().parents[2]


def git(*args: str, cwd: Path | None = None) -> str:
    result = subprocess.run(['git', *args], cwd=cwd, capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())
    return result.stdout.strip()


def remote_heads() -> dict[str, str]:
    return {ref.removeprefix('refs/heads/'): sha
            for sha, ref in (line.split() for line in git('ls-remote', '--heads', REPO, cwd=ROOT).splitlines())}


def verify_destination(cwd: Path | None = None) -> None:
    """Refuse rewrites without exposing resolved URLs or authentication details."""
    cwd = ROOT if cwd is None else cwd
    probe = 'cleanup-destination-' + uuid.uuid4().hex
    try:
        fetch_url = git('ls-remote', '--get-url', REPO, cwd=cwd)
        # get-url rejects command-scoped remotes; remote -v resolves them.
        remotes = git('-c', f'remote.{probe}.url={REPO}', 'remote', '-v', cwd=cwd)
        push_urls = [line[len(probe) + 1:-len(' (push)')]
                     for line in remotes.splitlines()
                     if line.startswith(probe + '\t') and line.endswith(' (push)')]
    except RuntimeError:
        raise RuntimeError('cannot verify repository destination; no refs changed') from None
    if fetch_url != REPO or push_urls != [REPO]:
        raise RuntimeError('repository URL rewrite detected; no refs changed')


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--active-commit', required=True, help='Exact reviewed lihonggang-dev tip SHA')
    parser.add_argument('--backup-dir', type=Path, required=True, help='Fresh local directory for mirror, bundle and receipt')
    parser.add_argument('--apply', action='store_true', help='Delete only unchanged archived branch names')
    args = parser.parse_args()
    if len(args.active_commit) != 40 or any(c not in '0123456789abcdef' for c in args.active_commit):
        parser.error('--active-commit must be a full lowercase SHA')
    manifest = json.loads((ROOT / 'docs/current/HISTORICAL_BRANCHES.json').read_text(encoding='utf-8'))
    original = {r['branch']: r['commit'] for r in manifest['branches']}
    verify_destination()
    observed = remote_heads()
    expected_kept = {'main': manifest['main_unchanged'], 'lihonggang-dev': args.active_commit, 'archive/history': ARCHIVE}
    if any(observed.get(k) != v for k, v in expected_kept.items()):
        raise RuntimeError('retained branch missing or moved; no refs changed')
    if set(observed) - (set(original) | KEEP):
        raise RuntimeError('new unreviewed branches exist; no refs changed')
    targets = {k: v for k, v in original.items() if k not in KEEP and k in observed}
    if any(observed[k] != v for k, v in targets.items()):
        raise RuntimeError('historical tip changed; no refs changed')
    folder = args.backup_dir.resolve()
    folder.mkdir(parents=True, exist_ok=False)
    mirror = folder / 'repository.git'
    git('clone', '--mirror', REPO, str(mirror), cwd=ROOT)
    git('fsck', '--full', cwd=mirror)
    for sha in original.values():
        git('merge-base', '--is-ancestor', sha, ARCHIVE, cwd=mirror)
    bundle = folder / 'before-cleanup.bundle'
    git('bundle', 'create', str(bundle), '--all', cwd=mirror)
    git('bundle', 'verify', str(bundle), cwd=mirror)
    with tempfile.TemporaryDirectory(prefix='restore-', dir=folder) as temp:
        restored = Path(temp) / 'restored.git'
        git('clone', '--mirror', str(bundle), str(restored))
        if git('show-ref', cwd=mirror) != git('show-ref', cwd=restored):
            raise RuntimeError('backup restoration ref mismatch')
        git('fsck', '--full', cwd=restored)
    digest = hashlib.sha256()
    with bundle.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    receipt = {'mode': 'apply' if args.apply else 'verify_only', 'before': observed,
               'planned_deletions': targets, 'keep': expected_kept,
               'backup_sha256': digest.hexdigest(), 'backup_verified': True,
               'deleted': [], 'status': 'verified_not_applied'}
    result_path = folder / 'cleanup-receipt.json'
    result_path.write_text(json.dumps(receipt, indent=2) + '\n', encoding='utf-8')
    if remote_heads() != observed:
        raise RuntimeError('remote changed during verification; no refs changed')
    if args.apply and targets:
        verify_destination(cwd=mirror)
        leases = [f'--force-with-lease=refs/heads/{name}:{sha}' for name, sha in targets.items()]
        deletes = [f':refs/heads/{name}' for name in targets]
        git('-c', 'remote.origin.mirror=false', 'push', '--atomic', *leases, REPO, *deletes, cwd=mirror)
        receipt['deleted'] = list(targets)
    final = remote_heads()
    receipt['after'] = final
    receipt['status'] = 'complete' if final == expected_kept else ('verified_not_applied' if not args.apply else 'needs_review')
    result_path.write_text(json.dumps(receipt, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'status': receipt['status'], 'branch_count': len(final),
                      'deleted_count': len(receipt['deleted']), 'receipt': str(result_path)}, ensure_ascii=False))
    if args.apply and final != expected_kept:
        raise RuntimeError('post-cleanup refs differ from expected; inspect receipt')


if __name__ == '__main__':
    main()
