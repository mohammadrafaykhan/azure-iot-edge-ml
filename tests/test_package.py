import io
import sys
import tarfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "infra"))

import package


def _tree(root: Path) -> None:
    for name, text in {
        "edgeml/features.py": "x = 1\n",
        "ml/models/v1.json": "{}\n",
        "modules/press-sensor/Dockerfile": "FROM python\n",
        "modules/press-sensor/__pycache__/sensor.cpython-312.pyc": "junk",
        "modules/press-sensor/old.pyc": "junk",
        "service/cli.py": "not shipped\n",
    }.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode())


def test_context_contains_only_build_inputs(tmp_path):
    _tree(tmp_path)
    assert package.source_files(tmp_path) == [
        "edgeml/features.py",
        "ml/models/v1.json",
        "modules/press-sensor/Dockerfile",
    ]


def test_archive_is_reproducible_and_named_by_content(tmp_path):
    _tree(tmp_path / "src")
    first = package.build(tmp_path / "src", tmp_path / "a")
    second = package.build(tmp_path / "src", tmp_path / "b")
    assert first["blob"] == second["blob"] == f"context-{first['hash']}.tar.gz"
    assert Path(first["path"]).read_bytes() == Path(second["path"]).read_bytes()

    (tmp_path / "src/edgeml/features.py").write_bytes(b"x = 2\n")
    assert package.build(tmp_path / "src", tmp_path / "c")["hash"] != first["hash"]


def test_archive_uses_posix_paths_and_fixed_metadata(tmp_path):
    _tree(tmp_path / "src")
    result = package.build(tmp_path / "src", tmp_path / "out")
    with tarfile.open(fileobj=io.BytesIO(Path(result["path"]).read_bytes()), mode="r:gz") as tar:
        members = tar.getmembers()
        assert [m.name for m in members] == package.source_files(tmp_path / "src")
        assert {m.mtime for m in members} == {0} and {m.mode for m in members} == {0o644}
        assert tar.extractfile("edgeml/features.py").read() == b"x = 1\n"
