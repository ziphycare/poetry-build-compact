from __future__ import annotations

import sys
import typing as t
from compileall import compile_dir
from hashlib import sha256
from pathlib import Path
from shutil import make_archive

from cleo.helpers import option
from poetry.console.commands.command import Command
from poetry.console.commands.installer_command import InstallerCommand
from poetry.core.packages.dependency import Dependency
from poetry.core.packages.dependency_group import MAIN_GROUP
from poetry.factory import Factory
from poetry.plugins.application_plugin import ApplicationPlugin
from tomlkit import inline_table
from tomlkit.toml_document import TOMLDocument

if t.TYPE_CHECKING:
    from poetry.core.packages.dependency_group import DependencyGroup


class BuildCompactPlugin(ApplicationPlugin):
    @property
    def commands(self) -> list[type[Command]]:
        return [BuildCompactCommand, ReplaceCommand]


class BaseReplaceCommand(Command):
    options = [
        option(
            "replace",
            description="Replace these packages dependencies by compiled ones.",
            flag=False,
            multiple=True,
        ),
        option(
            "prefix",
            description="Replace packages dependencies starting from this prefix by compiled ones.",
            flag=False,
            multiple=True,
        ),
        option(
            "suffix",
            description="Suffix of compiled packages.",
            flag=False,
            default="-compact",
        ),
    ]

    def soft_replace(self) -> bool:
        self.line(
            f"<info>Ephemeral dependencies replace in {self.poetry.package.name}</>"
        )

        replaced = False
        dependencies_group = self.poetry.package.dependency_group(MAIN_GROUP)
        for dependency in dependencies_group.dependencies:
            if self.is_replaceable_dependency(dependency.name):
                self.replace_dependency(dependency, dependencies_group)
                replaced = True

        return replaced

    def hard_replace(self) -> TOMLDocument | None:
        content: dict[str, t.Any] = self.poetry.file.read()
        poetry_content = content["tool"]["poetry"]

        if "dependencies" not in poetry_content:
            return None
        section = poetry_content["dependencies"]

        python_constraint = f"~{sys.version_info.major}.{sys.version_info.minor}"
        self.line(f"<info>Fix Python version to {python_constraint}</>")
        self.poetry.package.python_versions = python_constraint
        section["python"] = python_constraint

        self.line(f"<info>Replacing dependencies in {self.poetry.package.name}</>")

        dependencies_group = self.poetry.package.dependency_group(MAIN_GROUP)
        for dependency in dependencies_group.dependencies:
            if self.is_replaceable_dependency(dependency.name):
                self.replace_dependency(dependency, dependencies_group, section)

        if not self.affected_packages:
            return None

        self.poetry.locker.set_local_config(poetry_content)
        assert isinstance(content, TOMLDocument)
        return content

    def is_replaceable_dependency(self, name: str) -> bool:
        return (
            name in self.replace_names
            or not name.endswith(self.suffix)
            and name.startswith(self.replace_prefixes)
        )

    def replace_dependency(
        self,
        dependency: Dependency,
        group: DependencyGroup,
        toml_section: dict[str, t.Any] | None = None,
    ) -> None:
        constraint: dict[str, t.Any] = inline_table()
        constraint["version"] = dependency.pretty_constraint
        if dependency.allows_prereleases():
            constraint["allow-prereleases"] = True
        if dependency.source_name:
            constraint["source"] = dependency.source_name
        if dependency.extras:
            constraint["extras"] = dependency.extras
        if dependency.python_versions != "*":
            constraint["python"] = str(dependency.python_constraint)

        compact_dependency = Factory.create_dependency(
            f"{dependency.name}{self.suffix}",
            constraint,
            groups=[MAIN_GROUP],
            root_dir=self.poetry.file.path.parent,
        )

        installed_version = "!"
        for package in self.poetry.locker.locked_repository().packages:
            if package.name == dependency.name:
                installed_version = package.pretty_version

        self.line(
            "  <fg=green;options=bold>•</> "
            f"Replace <options=bold>{dependency.name}</> "
            f"(<fg=green>{installed_version})</> "
            f"by <fg=cyan>{compact_dependency.base_pep_508_name}</>"
        )

        group.add_dependency(compact_dependency)
        self.affected_packages.append(compact_dependency.name)

        group.remove_dependency(dependency.name)
        self.affected_packages.append(dependency.name)

        if toml_section:
            toml_section[compact_dependency.name] = constraint
            del toml_section[dependency.name]

    def prepare(self) -> None:
        self.suffix = self.option("suffix")
        self.replace_prefixes = tuple(self.option("prefix"))
        self.replace_names = set(self.option("replace"))
        self.affected_packages: list[str] = []

    def check_locker(self) -> bool:
        if not self.poetry.locker.is_locked():
            self.line_error("<error>The lock file does not exist.</>")
            return False

        if not self.poetry.locker.is_fresh():
            self.line_error(
                "<error>"
                "Error: poetry.lock is not consistent with pyproject.toml. "
                "Run `poetry lock [--no-update]` to fix it."
                "</>"
            )
            return False

        return True


class ReplaceCommand(BaseReplaceCommand, InstallerCommand):
    name = "replace"
    description = "Replace dependencies by their compiled analogs"

    def handle(self) -> int:
        if not self.check_locker():
            return 1

        self.prepare()
        if not self.replace_prefixes and not self.replace_names:
            self.line("Nothing to replace.")
            return 0

        if not self.install():
            return 1

        self.line("")
        if poetry_content := self.hard_replace():
            self.line("")

            self.installer.set_locker(self.poetry.locker)
            self.installer.set_package(self.poetry.package)
            self.installer.dry_run(False)
            self.installer.verbose(self.io.is_verbose())
            self.installer.update(True)
            self.installer.execute_operations(False)
            self.installer.whitelist(self.affected_packages)

            if self.installer.run() != 0:
                return 1

            self.line("")
            self.poetry.file.write(poetry_content)

            return self.call("install", f"--sync --all-extras --only={MAIN_GROUP}")
        return 0

    def install(self, lock: bool = False) -> bool:
        self.installer.set_locker(self.poetry.locker)
        self.installer.set_package(self.poetry.package)
        self.installer.verbose(self.io.is_verbose())
        self.installer.update(False)
        self.installer.execute_operations(not lock)
        self.installer.requires_synchronization(not lock)
        self.installer.only_groups([MAIN_GROUP])
        self.installer.extras(list(self.poetry.package.extras))

        return self.installer.run() == 0


class BuildCompactCommand(BaseReplaceCommand):
    name = "build-compact"
    description = "Compile package into bytecode wheel"
    options = [
        option(
            "optimize",
            "-o",
            description="Optimize bytecode files (remove asserts and docstrings).",
        ),
    ] + BaseReplaceCommand.options

    def handle(self) -> int:
        if not self.check_locker():
            return 1

        self.prepare()

        try:
            if self.soft_replace():
                self.line("")
            self.build_wheel()
        finally:
            self.clear()

        return 0

    def prepare(self) -> None:
        super().prepare()
        version = self.poetry.package.version.to_string()

        self.optimize = self.option("optimize")

        self.compact_name = f"{self.poetry.package.name}{self.suffix}"
        self.compact_dist_name = self.compact_name.replace("-", "_")

        assert self.poetry.package.root_dir
        self.dist_dir = self.poetry.package.root_dir / "dist"
        self.tmp_dir = self.dist_dir / "tmp"
        self.meta_dir = self.tmp_dir / f"{self.compact_dist_name}-{version}.dist-info"

        self.records: list[str] = []
        self.python_tag = f"py{sys.version_info.major}{sys.version_info.minor}"

        self.clear()

    def build_wheel(self) -> Path:
        self.line("<info>Building compact wheel</>")

        self.meta_dir.mkdir(parents=True)

        self.compile()
        self.metadata_file()
        self.wheel_file()
        self.record_file()

        version = self.poetry.package.version.to_string()
        wheel = f"{self.compact_dist_name}-{version}-{self.python_tag}-none-any"
        result = make_archive(str(self.dist_dir / wheel), "zip", self.tmp_dir)
        result_path = self.dist_dir / (wheel + ".whl")
        (self.dist_dir / result).rename(result_path)
        self.line(f"Built <c2>{result_path}</c2>")
        return result_path

    def clear(self) -> None:
        if self.tmp_dir.exists():
            rmdir(self.tmp_dir)

        assert self.poetry.package.root_dir
        remove_cache(self.poetry.package.root_dir)

    def compile(self) -> None:
        compile_dir(
            str(self.poetry.package.root_dir),
            ddir=f"<{self.compact_dist_name}>",
            optimize=2 if self.optimize else 0,
            quiet=1,
        )

        for include in self.poetry.package.packages:
            if "include" in include:
                source_dir = self.poetry.package.root_dir / include["include"]
                package_dir = self.tmp_dir / include["include"]
                package_dir.mkdir()
                self.records.extend(
                    [line for line in copy_pyc(source_dir, package_dir)]
                )

    def wheel_file(self) -> None:
        content = f"""Wheel-Version: 1.0
Generator: ziphy-compiler
Root-Is-Purelib: true
Tag: {self.python_tag}-none-any
""".encode()

        wheel_file = self.meta_dir / "WHEEL"
        wheel_file.write_bytes(content)
        self.records.append(record_line(self.tmp_dir, wheel_file, content))

    def metadata_file(self) -> None:
        this_python = f"{sys.version_info.major}.{sys.version_info.minor}"
        next_python = f"{sys.version_info.major}.{sys.version_info.minor + 1}"
        content = f"""\
Metadata-Version: 2.1
Name: {self.compact_name}
Version: {self.poetry.package.version}
Summary: {self.poetry.package.description}
License: Proprietary
Requires-Python: >={this_python},<{next_python}
"""
        if self.poetry.package.author_name:
            content += f"Author: {self.poetry.package.author_name}\n"

        if self.poetry.package.author_email:
            content += f"Author-email: {self.poetry.package.author_email}\n"

        if self.poetry.package.maintainer_name:
            content += f"Maintainer: {self.poetry.package.maintainer_name}\n"

        if self.poetry.package.maintainer_email:
            content += f"Maintainer-email: {self.poetry.package.maintainer_email}\n"

        dependencies = sorted(
            self.poetry.package.dependency_group(MAIN_GROUP).dependencies,
            key=lambda d: d.name,
        )
        for dependency in dependencies:
            content += f"Requires-Dist: {dependency.base_pep_508_name}\n"

        content_bytes = content.encode()

        metadata_file = self.meta_dir / "METADATA"
        metadata_file.write_bytes(content_bytes)
        self.records.append(record_line(self.tmp_dir, metadata_file, content_bytes))

    def record_file(self) -> None:
        record_file = self.meta_dir / "RECORD"
        self.records.append(f"{record_file.relative_to(self.tmp_dir)},,")
        record_file.write_bytes("\n".join(self.records).encode())


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
