# Poetry "build-compact" plugin

Poetry plugin providing `build-compact` command which builds wheel containing only *.pyc
files for the current Python version. Created package name has suffix added to the
original name. It is possible to replace main dependencies by name or prefix by the
packages with the same suffix. Non-main dependencies are not touched. The
`pyproject.toml` and `poetry.lock` files stay intact, as well as virtual environment.

Plugin also provides `replace` command which replaces main dependencies by name or
prefix by the packages with the same suffix. Affected packages are replaced by compact
ones of the exactly same version as currently installed original package. The
`pyproject.toml` and `poetry.lock` as well as virtual environment are updated.

## Changelog
* 1.2.1: sync dependencies only in virtual environments
* 1.2.0: use project virtual env Python version instead of Poetry one, keep dependencies markers in compact packages
* 1.1.0: `replace` command now uses installed versions exactly instead of original versions constraint
