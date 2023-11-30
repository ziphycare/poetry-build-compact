# Poetry "build-compact" plugin

Poetry plugin providing `build-compact` command which builds wheel containing only *.pyc
files for the current Python version. Created package name has suffix added to the
original name. It is possible to replace main dependencies by name or prefix by the
packages with the same suffix. The `pyproject.toml` and `poetry.lock` files stay intact,
as well as virtual environment.

Plugin also provides `replace` command which replaces main dependencies by name or
prefix by the packages with the same suffix. The `pyproject.toml` and `poetry.lock` as
well as virtual environment are updated.