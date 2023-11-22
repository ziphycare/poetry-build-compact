from poetry.console.commands.command import Command
from poetry.plugins.application_plugin import ApplicationPlugin
from cleo.helpers import option
from poetry.core.packages.dependency import Dependency

from poetry.console.application import Application


class BuildCompactPlugin(ApplicationPlugin):
    @property
    def commands(self) -> list[type[Command]]:
        return [BuildCompactCommand]


class BuildCompactCommand(Command):
    name = "build-compact"
    description = "Compile package into bytecode wheel"
    options = [
        option(
            "no-optimize",
            "Do not optimize generated files (leave asserts and docstrings).",
            flag=True,
        ),
        option(
            "replace",
            "Replace these packages dependencies by compiled ones.",
            flag=False,
            multiple=True,
        ),
        option(
            "prefix",
            "Replace packages dependencies starting from this prefix by compiled ones.",
            flag=False,
            multiple=True,
        ),
        option(
            "suffix",
            "Suffix to add to compiled package.",
            flag=False,
            default="-compact",
        ),
    ]

    def handle(self) -> int:
        suffix = self.option("suffix")
        prefixes = tuple(self.option("prefix"))
        self.line(self.poetry.package.name)

        dependencies_group = self.poetry.package.dependency_group("main")
        for dependency in dependencies_group.dependencies:
            if dependency.name.startswith(prefixes) and not dependency.name.endswith(
                suffix
            ):
                compact_dependency = Dependency(
                    f"{dependency.name}{suffix}",
                    dependency.constraint,
                    optional=dependency.is_optional,
                    groups=dependency.groups,
                    allows_prereleases=dependency.allows_prereleases,
                    extras=dependency.extras,
                    source_type=dependency.source_type,
                    source_url=dependency.source_url,
                    source_reference=dependency.source_reference,
                    source_resolved_reference=dependency.source_resolved_reference,
                    source_subdirectory=dependency.source_subdirectory,
                )
                self.line(f"Replace {dependency.name}@{dependency.constraint} by {compact_dependency.name}@{compact_dependency.constraint}")
                dependencies_group.add_dependency(compact_dependency)
                dependencies_group.remove_dependency(dependency)

        return 0
