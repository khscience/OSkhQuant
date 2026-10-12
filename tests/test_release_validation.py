import hashlib
import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location('os_release_validation', Path(__file__).parents[1] / 'packaging' / 'verify_release.py')
validation = importlib.util.module_from_spec(spec)
spec.loader.exec_module(validation)


def prepared(tmp_path):
    name = 'khQuantOS_Setup_V2.2.0.exe'
    content = b'isolated fixture installer bytes'
    (tmp_path / name).write_bytes(content)
    digest = hashlib.sha256(content).hexdigest()
    (tmp_path / 'SHA256SUMS.txt').write_text(f'{digest}  {name}\n', encoding='utf-8')
    metadata = {'tagName': 'V2.2.0', 'isDraft': False, 'isPrerelease': False,
                'assets': [{'name': name}, {'name': 'SHA256SUMS.txt'}]}
    return metadata, digest


def test_reviewed_release_accepts_matching_commit_and_bytes(tmp_path):
    metadata, digest = prepared(tmp_path)
    assert validation.validate_release(metadata, 'V2.2.0', 'a' * 40, 'a' * 40, 'a' * 40, tmp_path) == digest


@pytest.mark.parametrize('fault', ['draft', 'prerelease', 'old_github', 'old_gitee', 'corrupt', 'missing_asset', 'path_traversal'])
def test_release_gate_rejects_wrong_source_or_assets(tmp_path, fault):
    metadata, _ = prepared(tmp_path)
    github = gitee = 'a' * 40
    if fault == 'draft': metadata['isDraft'] = True
    if fault == 'prerelease': metadata['isPrerelease'] = True
    if fault == 'old_github': github = 'b' * 40
    if fault == 'old_gitee': gitee = 'b' * 40
    if fault == 'corrupt': (tmp_path / 'khQuantOS_Setup_V2.2.0.exe').write_bytes(b'wrong release')
    if fault == 'missing_asset': metadata['assets'].pop()
    if fault == 'path_traversal': (tmp_path / 'SHA256SUMS.txt').write_text('a' * 64 + '  ../old.exe\n')
    with pytest.raises(ValueError):
        validation.validate_release(metadata, 'V2.2.0', 'a' * 40, github, gitee, tmp_path)
