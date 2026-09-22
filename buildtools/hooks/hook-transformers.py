"""Custom PyInstaller hook for `transformers`.

`--collect-all transformers` (COLLECT_ALL in build.py) is NOT sufficient on
its own: by default PyInstaller's data-file collection explicitly EXCLUDES
.py/.pyc files (they normally get compiled into the PYZ archive instead of
staying as loose files - see PyInstaller's own collect_data_files() docs).
`transformers/models/__init__.py` calls `os.listdir()` on its own package
directory at import time to discover which model submodules exist - a
dynamic filesystem read that needs those files to genuinely exist on disk,
not just be traceable as importable Python code. A real, reported crash
on TWO separate real CI runs (Windows then macOS) showed the exact same
failure - FileNotFoundError on
Contents/Frameworks/transformers/models/__init__.pyc - which the plain
--collect-all flag alone did not fix (that was tried first; this is the
generic hook-mechanism escalation PyInstaller itself documents for exactly
this pattern - a package that "requires source .py files to be available"
or "tries to extend sys.path... in a way that is incompatible with
PyInstaller's frozen importer").

include_py_files=True is the specific, documented switch that keeps these
files as loose files instead of archiving them - this is what --collect-all
does not do by itself.
"""

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

datas = collect_data_files("transformers", include_py_files=True)
hiddenimports = collect_submodules("transformers")
