"""Validate the reviewed source commit and installer before creating a mirror release."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess


def resolve_tag(repository: str, tag: str) -> str:
    result = subprocess.run(
        ['git', 'ls-remote', '--tags', repository, f'refs/tags/{tag}', f'refs/tags/{tag}^{{}}'],
        check=True, capture_output=True, text=True, timeout=60,
    )
    refs = {line.split()[1]: line.split()[0] for line in result.stdout.splitlines() if line.strip()}
    return refs.get(f'refs/tags/{tag}^{{}}', refs.get(f'refs/tags/{tag}', ''))


def validate_release(metadata: dict, tag: str, expected_commit: str, github_commit: str,
                     gitee_commit: str, directory: Path) -> str:
    if not re.fullmatch(r'[vV]2\.\d+\.\d+', tag):
        raise ValueError('只接受 V2.x.y 标签')
    if not re.fullmatch(r'[0-9a-f]{40}', expected_commit):
        raise ValueError('必须填写已验收发布提交的完整 40 位 SHA')
    if metadata.get('tagName') != tag or metadata.get('isDraft') is not False or metadata.get('isPrerelease') is not False:
        raise ValueError('GitHub Release 必须是对应标签的正式发布，不能是草稿或预发布')
    if github_commit != expected_commit or gitee_commit != expected_commit:
        raise ValueError('GitHub / Gitee 标签必须都指向已验收的同一提交')
    name = f'khQuantOS_Setup_V{tag[1:]}.exe'
    expected_names = {name, 'SHA256SUMS.txt'}
    names = [asset.get('name') for asset in metadata.get('assets', [])]
    if any(names.count(item) != 1 for item in expected_names):
        raise ValueError('GitHub 缺少唯一的安装包或 SHA256SUMS.txt')
    if {path.name for path in directory.iterdir()} != expected_names:
        raise ValueError('下载目录应仅包含目标安装包和校验和')
    lines = [line.strip() for line in (directory / 'SHA256SUMS.txt').read_text(encoding='utf-8-sig').splitlines() if line.strip()]
    if len(lines) != 1:
        raise ValueError('校验和文件应仅列出该安装包')
    match = re.fullmatch(r'([0-9a-fA-F]{64})\s+\*?([^/\\]+)', lines[0])
    if not match or match.group(2) != name:
        raise ValueError('校验和文件中的安装包名称不匹配')
    installer = directory / name
    if installer.stat().st_size == 0:
        raise ValueError('安装包为空')
    with installer.open('rb') as handle:
        actual = hashlib.file_digest(handle, 'sha256').hexdigest()
    if actual != match.group(1).lower():
        raise ValueError('安装包 SHA256 不匹配，禁止镜像发布')
    return actual


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('metadata', 'tag', 'expected-commit', 'github-repo', 'gitee-repo', 'directory'):
        parser.add_argument('--' + name, required=True)
    args = parser.parse_args()
    digest = validate_release(
        json.loads(Path(args.metadata).read_text(encoding='utf-8-sig')),
        args.tag, args.expected_commit,
        resolve_tag('https://github.com/' + args.github_repo + '.git', args.tag),
        resolve_tag('https://gitee.com/' + args.gitee_repo + '.git', args.tag),
        Path(args.directory),
    )
    print('Release source and installer verified:', digest)


if __name__ == '__main__':
    main()
