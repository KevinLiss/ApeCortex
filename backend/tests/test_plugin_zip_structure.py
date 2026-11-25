"""插件 zip 安装结构回归（M5）：三种打包方式的行为契约。

背景：宿主 import_plugin 曾把"唯一顶层目录就是插件包目录"误判为外层壳，
导致最自然的 `zip -r kb.zip knowledge_base` 通过校验却在解压阶段报错。
"""
import json
import shutil
import zipfile
from pathlib import Path

import pytest

from src.plugins.manager import PluginManager


def _make_pkg(tmp_path: Path) -> Path:
    pkg = tmp_path / "src" / "knowledge_base"
    (pkg / "pipeline").mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "pipeline" / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "plugin.json").write_text(
        json.dumps({"name": "knowledge_base", "display_name": "ApeCortex", "version": "0.4.0"}),
        encoding="utf-8",
    )
    return tmp_path / "src"


def _zip(src_root: Path, out: Path, entries: list[tuple[str, Path]]) -> Path:
    with zipfile.ZipFile(out, "w") as zf:
        for arc_root, real in entries:
            for p in real.rglob("*"):
                if p.is_file():
                    arc = f"{arc_root}/{p.relative_to(real).as_posix()}" if arc_root else p.relative_to(real).as_posix()
                    zf.write(p, arc)
    return out


@pytest.fixture
def manager(tmp_path):
    m = PluginManager()
    m._builtin_dir = tmp_path / "builtin"
    return m


def test_install_root_package_dir_only(tmp_path, manager):
    """zip 根只有 knowledge_base/（最自然的打包含义）：必须成功。"""
    src = _make_pkg(tmp_path)
    zp = _zip(src, tmp_path / "b.zip", [("knowledge_base", src / "knowledge_base")])
    manifest = PluginManager._validate_plugin_zip(zp)
    assert manifest["name"] == "knowledge_base"
    got = manager.import_plugin(zp)
    assert (manager._builtin_dir / got["name"] / "__init__.py").exists()


def test_install_wrapped_with_manifest(tmp_path, manager):
    """外层目录包裹 + 外层根放 plugin.json：成功。"""
    src = _make_pkg(tmp_path)
    wrap = tmp_path / "wrap"
    shutil.copytree(src / "knowledge_base", wrap / "knowledge_base")
    shutil.copyfile(src / "knowledge_base" / "plugin.json", wrap / "plugin.json")
    zp = _zip(tmp_path, tmp_path / "c.zip", [("outer", wrap)])
    got = manager.import_plugin(zp)
    assert (manager._builtin_dir / got["name"] / "__init__.py").exists()


def test_install_rejects_missing_manifest(tmp_path):
    """外层目录但外层无 plugin.json：校验阶段明确报错。"""
    src = _make_pkg(tmp_path)
    zp = _zip(tmp_path, tmp_path / "d.zip", [("outer/knowledge_base", src / "knowledge_base")])
    with pytest.raises(ValueError, match="plugin.json"):
        PluginManager._validate_plugin_zip(zp)
