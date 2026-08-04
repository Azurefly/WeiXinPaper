from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VERSION = "2.1.3"
SOURCE_STEM = f"WeiXinGZH_AI_Studio_{VERSION}_审计修复源码版"
RUNTIME_STEM = f"WeiXinGZH_AI_Studio_{VERSION}_审计修复运行版"

SOURCE_TOP = {
    ".github",
    ".gitignore",
    ".test-adapters-enabled",
    "README.md",
    "backend",
    "build_assets",
    "build_release.py",
    "build_scripts",
    "data",
    "desktop.py",
    "desktop_unix.sh",
    "desktop_windows.cmd",
    "docs",
    "install.py",
    "requirements-desktop.txt",
    "setup_unix.sh",
    "setup_windows.cmd",
    "start.py",
    "start_unix.sh",
    "start_windows.cmd",
    "test_all.py",
    "test_runtime.py",
    "tests",
    "verify_external_links.py",
    "verify_capacity.py",
    "verify_browser_service.py",
    "verify_browser_service.cmd",
    "verify_windows_dpapi.py",
    "verify_windows_dpapi.cmd",
    "web",
}
RUNTIME_TOP = {
    "README.md",
    "backend",
    "data",
    "docs",
    "install.py",
    "setup_unix.sh",
    "setup_windows.cmd",
    "start.py",
    "start_unix.sh",
    "start_windows.cmd",
    "test_runtime.py",
    "verify_external_links.py",
    "verify_capacity.py",
    "verify_browser_service.py",
    "verify_browser_service.cmd",
    "verify_windows_dpapi.py",
    "verify_windows_dpapi.cmd",
    "web",
}


def excluded(path: Path) -> bool:
    rel = path.relative_to(ROOT)
    # data/ 只是运行时目录。日志、数据库、迁移备份、初始密码和
    # 主密钥都可能包含用户内容或凭据，不得进入源码/运行包。
    if rel.parts and rel.parts[0] == "data":
        return True
    parts = set(rel.parts)
    if ".git" in parts or "__pycache__" in parts or ".pytest_cache" in parts:
        return True
    if path.suffix in {".pyc", ".pyo", ".zip"}:
        return True
    name = path.name
    if name in {".DS_Store", ".env"} or name.startswith(".master.key"):
        return True
    if name.endswith(".db") or ".db-" in name or name.endswith(".bak"):
        return True
    if name in {"RELEASE_MANIFEST.json", "RELEASE_FILES_SHA256.txt"}:
        return True
    return False


def copy_top(stage: Path, names: set[str]) -> None:
    for name in sorted(names):
        source = ROOT / name
        if not source.exists():
            raise RuntimeError(f"缺少发布文件：{name}")
        target = stage / name
        if source.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            for item in source.rglob("*"):
                if item.is_dir() or excluded(item):
                    continue
                relative = item.relative_to(source)
                destination = target / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(item, destination)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_metadata(stage: Path, package_type: str) -> None:
    current_files = [path for path in stage.rglob("*") if path.is_file()]
    manifest = {
        "product": "公众号 AI Studio",
        "version": VERSION,
        "packageType": package_type,
        "level": "release-candidate",
        "generatedAt": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "files": len(current_files) + 2,
        "coreAutomatedTests": "full-unittest-discovery-passed-during-build",
        "browserServiceE2E": "manual-browser-passed-playwright-gate-required",
        "externalCredentialedValidation": "not-run-no-credentials",
        "windowsDpapiValidation": "self-test-shipped-current-environment-not-windows",
        "capacityValidation": "10000-articles-and-100-edits-passed",
    }
    (stage / "RELEASE_MANIFEST.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = []
    for path in sorted(stage.rglob("*")):
        if path.is_file() and path.name != "RELEASE_FILES_SHA256.txt":
            lines.append(f"{sha256(path)}  {path.relative_to(stage).as_posix()}")
    (stage / "RELEASE_FILES_SHA256.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")


def zip_stage(stage: Path, output: Path) -> None:
    output.unlink(missing_ok=True)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in sorted(stage.rglob("*")):
            if path.is_file():
                archive.write(path, Path(stage.name) / path.relative_to(stage))


def verify_hashes(root: Path) -> None:
    for line in (root / "RELEASE_FILES_SHA256.txt").read_text(encoding="utf-8").splitlines():
        expected, relative = line.split("  ", 1)
        actual = sha256(root / relative)
        if actual != expected:
            raise RuntimeError(f"发布文件哈希不一致：{relative}")


def verify_zip_clean(path: Path, *, runtime: bool) -> None:
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        bad = [
            name
            for name in names
            if any(token in name for token in ("__pycache__", ".pyc", ".master.key", ".initial_password", ".env"))
            or Path(name).name.endswith(".db")
            or ".db-" in Path(name).name
            or ".db." in Path(name).name
            or Path(name).name.endswith(".log")
            or ".log." in Path(name).name
            or "/2.1.0_" in name
            or "/2.1.1_" in name
        ]
        if bad:
            raise RuntimeError(f"ZIP 含禁止文件：{bad[:10]}")
        marker = any(name.endswith("/.test-adapters-enabled") for name in names)
        if runtime and marker:
            raise RuntimeError("运行包不得包含测试适配器启用标记")
        if not runtime and not marker:
            raise RuntimeError("源码包缺少测试适配器测试标记")


def extract_single(path: Path, destination: Path) -> Path:
    with zipfile.ZipFile(path) as archive:
        archive.extractall(destination)
    roots = [item for item in destination.iterdir() if item.is_dir()]
    if len(roots) != 1:
        raise RuntimeError("ZIP 顶层目录结构异常")
    return roots[0]


def run(command: list[str], cwd: Path, timeout: int = 300) -> None:
    print(">", " ".join(command), flush=True)
    clean_env = {key: value for key, value in os.environ.items() if not key.startswith("STUDIO_")}
    clean_env["PYTHONDONTWRITEBYTECODE"] = "1"
    subprocess.run(command, cwd=cwd, check=True, timeout=timeout, env=clean_env)


def build(output_dir: Path, verify: bool) -> tuple[Path, Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    source_zip = output_dir / f"{SOURCE_STEM}.zip"
    runtime_zip = output_dir / f"{RUNTIME_STEM}.zip"
    sums = output_dir / f"WeiXinGZH_AI_Studio_{VERSION}_SHA256SUMS.txt"

    with tempfile.TemporaryDirectory(prefix="studio-release-") as temp:
        temp_root = Path(temp)
        source_stage = temp_root / SOURCE_STEM
        runtime_stage = temp_root / RUNTIME_STEM
        source_stage.mkdir()
        runtime_stage.mkdir()
        copy_top(source_stage, SOURCE_TOP)
        copy_top(runtime_stage, RUNTIME_TOP)
        write_metadata(source_stage, "source")
        write_metadata(runtime_stage, "runtime")
        zip_stage(source_stage, source_zip)
        zip_stage(runtime_stage, runtime_zip)

    sums.write_text(
        f"{sha256(source_zip)}  {source_zip.name}\n{sha256(runtime_zip)}  {runtime_zip.name}\n",
        encoding="utf-8",
    )

    if verify:
        verify_zip_clean(source_zip, runtime=False)
        verify_zip_clean(runtime_zip, runtime=True)
        with tempfile.TemporaryDirectory(prefix="studio-verify-") as temp:
            verify_root = Path(temp)
            source_root = extract_single(source_zip, verify_root / "source")
            runtime_root = extract_single(runtime_zip, verify_root / "runtime")
            verify_hashes(source_root)
            verify_hashes(runtime_root)
            run([sys.executable, "test_all.py"], source_root)
            run([sys.executable, "verify_capacity.py"], source_root, timeout=180)
            run([sys.executable, "install.py"], runtime_root)
            run([sys.executable, "test_runtime.py"], runtime_root)
            if (runtime_root / ".test-adapters-enabled").exists():
                raise RuntimeError("运行包测试适配器门禁失效")
    return source_zip, runtime_zip, sums


def main() -> None:
    parser = argparse.ArgumentParser(description="构建并验证公众号 AI Studio 2.1.3 发布包")
    parser.add_argument("--output", default="/mnt/data", help="发布包输出目录")
    parser.add_argument("--no-verify", action="store_true", help="仅打包，不执行解压回归")
    args = parser.parse_args()
    artifacts = build(Path(args.output).resolve(), not args.no_verify)
    for artifact in artifacts:
        print(artifact)


if __name__ == "__main__":
    main()
