from compileall import compile_dir
from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path
from shutil import copyfile, make_archive
import subprocess
import sys
import typing as t

try:
    from importlib.metadata import metadata
except ImportError:
    # Python 3.7
    from importlib_metadata import metadata


@contextmanager
def settings_backup() -> t.Iterator[None]:
    files: t.Dict[Path, Path] = {}
    for filename in ("poetry.lock", "pyproject.toml"):
        source_path = Path(filename)
        if source_path.exists():
            target_path = source_path.with_suffix(".bak")
            copyfile(source_path, target_path)
            files[source_path] = target_path
    try:
        yield
    finally:
        for source_path, target_path in files.items():
            target_path.rename(source_path)


def prepare(
    name: str,
    *,
    pkg_name: t.Optional[str] = None,
    source_dir: t.Optional[str] = None,
    target_dir: t.Optional[str] = None,
) -> t.Tuple[str, str, str, Path, Path]:
    """
    Prepare directories for compilation

    :param str name: package name as in requirements
    :param str pkg_name: importable package name if differs from package name
    :param str|None source_dir: package code source dir if not importable package name
    :param str|None target_dir: output dir if not "dist"
    :return: package name, importable package name, its version, source code path,
             path to temporary directory to utilize
    :rtype: tuple
    """
    if not pkg_name:
        pkg_name = name
    source_path = Path(source_dir or pkg_name)
    target_path = Path(target_dir or "dist")

    tmp_path = target_path / "tmp"
    if tmp_path.exists():
        rmdir(tmp_path)

    pkg_version = subprocess.run(
        ["poetry", "version", "--short"], capture_output=True, text=True
    ).stdout.rstrip()

    return name, pkg_name, pkg_version, source_path, tmp_path


def replace_dependency(dependencies: str, *, source: t.Optional[str] = None) -> bool:
    add_command = ["poetry", "add", "package-name"]
    if source:
        add_command.extend(["--source", source])
    for dependency in dependencies:
        result = subprocess.run(["poetry", "remove", dependency])
        if result.returncode != 0:
            return False
        add_command[2] = f"{dependency}-compact"
        result = subprocess.run(add_command)
        if result.returncode != 0:
            return False
    result = subprocess.run(["poetry", "lock", "--no-update"])
    if result.returncode != 0:
        return False
    return True


def compile_package(
    pkg_name: str,
    *,
    source_dir: Path,
    target_dir: Path,
    optimize: bool = True,
    quiet: bool = True,
) -> t.List[str]:
    """
    Compile package and return entries for RECORD wheel file

    :param str pkg_name: importable package name
    :param Path|None source_dir: package code source dir
    :param Path|None target_dir: output dir
    :param bool optimize: compile with maximal optimization (default) or
                          without optimization at all (if false)
    :param bool quiet: suppress non-error output
    :return: list of strings for RECORD wheel file
    """
    remove_cache(source_dir)
    compile_dir(source_dir, optimize=2 if optimize else 0, quiet=int(quiet))

    package_dir = target_dir / f"{pkg_name}-compact"
    package_dir.mkdir(parents=True)

    return [line for line in copy_pyc(source_dir, package_dir)]


def compile_stubs(
    pkg_name: str, *, target_dir: Path, quiet: bool = True
) -> t.List[str]:
    """
    Compile package typing stubs package and return for RECORD wheel file

    :param str pkg_name: importable package name
    :param Path|None target_dir: output dir
    :param bool quiet: suppress non-error output
    :return: list of strings for RECORD wheel file
    :rtype: list[str]
    """
    target_dir.mkdir(parents=True)

    command = [
        "stubgen",
        "--package",
        pkg_name,
        "--output",
        str(target_dir),
        "--export-less",
    ]
    if quiet:
        command.append("--quiet")
    subprocess.run(command)

    package_dir = target_dir / f"{pkg_name}-stubs"
    (target_dir / pkg_name).rename(package_dir)

    return [line for line in list_pyi(package_dir)]


def build_wheel(
    name: str,
    version: str,
    dir: Path,
    pkg_records: t.List[str],
    stubs_for: t.Optional[str] = None,
) -> Path:
    """
    Build wheel for the package

    :param str name: package name as in requirements,
                     in case of stubs packaging it is original package name
    :param str version: package full version,
                        in case of stubs packaging it is original package version
    :param Path dir: output dir
    :param list[str] pkg_records: list of RECORD wheel file strings for included files
    :param str|None stubs_for: in case of stubs packaging it is
                               original package importable name
    :return: path to generated wheel
    :rtype: Path
    """
    wheel_name = f"{stubs_for}-stubs" if stubs_for else f"{name}-compact"
    dist_info_name = wheel_name.replace("-", "_")
    meta_dir = dir / f"{dist_info_name}-{version}.dist-info"
    meta_dir.mkdir()

    python_version = f"{sys.version_info.major}{sys.version_info.minor}"

    pkg_metadata_obj = metadata(name)
    pkg_metadata = []
    processed_keys = set()
    for key in pkg_metadata_obj:
        if key in processed_keys:
            continue
        if stubs_for:
            if key == "Name":
                pkg_metadata.append(f"Name: {wheel_name}")
                continue
            elif key == "Summary":
                pkg_metadata.append(
                    f"Summary: {pkg_metadata_obj['Summary']} typing stubs"
                )
                continue
        pkg_metadata.extend(
            [f"{key}: {value}" for value in pkg_metadata_obj.get_all(key) or []]
        )
        processed_keys.add(key)
    pkg_metadata_file = meta_dir / "METADATA"
    pkg_metadata_content = "\n".join(pkg_metadata).encode()
    pkg_metadata_file.write_bytes(pkg_metadata_content)
    pkg_records.append(record_line(dir, pkg_metadata_file, pkg_metadata_content))

    pkg_wheel = f"""Wheel-Version: 1.0
Generator: ziphy-compiler
Root-Is-Purelib: true
Tag: py{python_version}-none-any""".encode()
    pkg_wheel_file = meta_dir / "WHEEL"
    pkg_wheel_file.write_bytes(pkg_wheel)
    pkg_records.append(record_line(dir, pkg_wheel_file, pkg_wheel))

    pkg_record_file = meta_dir / "RECORD"
    pkg_records.append(f"{pkg_record_file.relative_to(dir)},,")
    pkg_record_content = "\n".join(pkg_records).encode()
    pkg_record_file.write_bytes(pkg_record_content)

    wheel = f"{dist_info_name}-{version}-py{python_version}-none-any"
    target_dir = dir.parent
    result = make_archive(str(target_dir / wheel), "zip", dir)
    result_path = target_dir.parent / (wheel + ".whl")
    (target_dir / result).rename(result_path)
    return result_path


def record_line(base: Path, path: Path, content: bytes) -> str:
    """
    Generate RECORDS wheel file line for a file

    :param Path base: path to directory where files for packaging are
    :param Path path: path to target file (must be `base` subpath)
    :param bytes content: target file contents
    :return: RECORDS wheel file line
    :rtype: str
    """
    digest = sha256(content).hexdigest()
    size = len(content)
    return f"{path.relative_to(base)},sha256={digest},{size}"


def rmdir(path: Path) -> None:
    """Remove probably existing probably not empty directory"""
    for item in path.iterdir():
        if item.is_dir():
            rmdir(item)
        else:
            item.unlink()
    path.rmdir()


def remove_cache(path: Path) -> None:
    """Clear Python bytecache recursively"""
    is_cache = path.name == "__pycache__"
    for item in path.iterdir():
        if item.is_dir():
            remove_cache(item)
        elif is_cache:
            item.unlink()
    if is_cache:
        path.rmdir()


def copy_pyc(
    source: Path, dest: Path, base: t.Optional[Path] = None
) -> t.Iterator[str]:
    """
    Copy Python bytecache files in a package structure

    :param Path source: package code path
    :param Path dest: directory to copy "byte package" to
    :param Path|None base: path to directory containing code if not `source` parent
    :return: generator of RECORDS wheel file lines for each copied file
    :rtype: t.Iterator[str]
    """
    if not base:
        base = dest.parent
    for item in source.iterdir():
        if item.is_dir():
            if item.name == "__pycache__":
                yield from copy_pyc(item, dest, base)
            else:
                new_dest = dest / item.name
                new_dest.mkdir()
                yield from copy_pyc(item, new_dest, base)
        elif item.suffix == ".pyc":
            content = item.read_bytes()
            target = dest / (item.stem.split(".")[0] + item.suffix)
            target.write_bytes(content)
            yield record_line(base, target, content)


def list_pyi(path: Path, base: t.Optional[Path] = None) -> t.Iterator[str]:
    """
    List typing stubs files in a directory

    :param Path source: package code path
    :param Path|None base: path to directory containing code if not `path` parent
    :return: generator of RECORDS wheel file lines for each ".pyi" file
    :rtype: t.Iterator[str]
    """
    if not base:
        base = path.parent
    for item in path.iterdir():
        if item.is_dir():
            yield from list_pyi(item, base)
        elif item.suffix == ".pyi":
            yield record_line(base, item, item.read_bytes())


if __name__ == "__main__":
    from argparse import ArgumentParser

    parser = ArgumentParser(
        description="Compile Python package into byte-code wheel and typing stubs wheel"
    )
    parser.add_argument("name", help="Package name")
    parser.add_argument(
        "-p", "--package", help="Importable package name (if differs from package name)"
    )
    parser.add_argument(
        "-s",
        "--source",
        help="Path to the code source dir (if differs from importable package name)",
    )
    parser.add_argument(
        "-t", "--target", help='Path to the output dir (default "dist")'
    )
    parser.add_argument(
        "--replace-dependency",
        nargs="*",
        help="Replace these packages dependencies by binary ones",
    )
    parser.add_argument(
        "--dependency-source",
        help="Binary dependencies source (must be already defined)",
    )
    parser.add_argument(
        "--no-package", action="store_true", help="Skip package compilation"
    )
    parser.add_argument(
        "--with-stubs", action="store_true", help="Compile typing stubs"
    )
    parser.add_argument(
        "--no-optimize",
        action="store_true",
        help="Do not optimize generated files (leave asserts and docstrings)",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Output processed files and leave temporary files",
    )
    args = parser.parse_args()

    with settings_backup():
        name, pkg_name, pkg_version, source_dir, tmp_dir = prepare(
            args.name,
            pkg_name=args.package,
            source_dir=args.source,
            target_dir=args.target,
        )

        if args.replace_dependency:
            if replace_dependency(
                args.replace_dependency, source=args.dependency_source
            ):
                print("Replaced dependencies to binary ones")
            else:
                print("Build process failed")
                exit(1)

        if not args.no_package:
            pkg_dir = tmp_dir / "package"
            pkg_records = compile_package(
                pkg_name,
                source_dir=source_dir,
                target_dir=pkg_dir,
                optimize=not args.no_optimize,
                quiet=not args.verbose,
            )
            wheel = build_wheel(name, pkg_version, pkg_dir, pkg_records)
            print(f"Compiled package wheel {wheel}")

        if args.with_stubs:
            stubs_dir = tmp_dir / "stubs"
            stub_records = compile_stubs(
                pkg_name, target_dir=stubs_dir, quiet=not args.verbose
            )
            wheel = build_wheel(
                name, pkg_version, stubs_dir, stub_records, stubs_for=pkg_name
            )
            print(f"Compiled stubs wheel {wheel}")

        if not args.verbose:
            remove_cache(source_dir)
            rmdir(tmp_dir)
            print("Compilation process artifacts cleared")
