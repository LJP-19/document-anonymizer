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

A second, separate gap surfaced only once the first was fixed: transformers
verifies its own runtime dependencies at import time
(transformers/dependency_versions_check.py, `pkgs_to_check_at_runtime`) by
reading each package's installed-metadata via importlib.metadata.version().
That metadata (.dist-info) is a different thing from the package's own
source files - collect_data_files()/--collect-all do not bundle it, only
copy_metadata() does. Missing it produced a real, reported crash on a real
macOS CI run: PackageNotFoundError for "regex", the first package in that
check list. Read transformers' own dependency_versions_check.py directly to
get the real, complete list rather than guessing - fixing them one at a
time as each one surfaces would mean N separate broken builds for a list
that is fully knowable up front.
"""

from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

datas = collect_data_files("transformers", include_py_files=True)
hiddenimports = collect_submodules("transformers")

#: Exactly transformers/dependency_versions_check.py's own
#: pkgs_to_check_at_runtime list, minus "python" (not a real installed
#: package) and "accelerate" (this project does not install it, and
#: transformers itself skips the check when it is absent - matching that
#: here rather than bundling an unused package's metadata).
for _pkg in (
    "tqdm", "regex", "requests", "packaging", "filelock", "numpy",
    "tokenizers", "huggingface-hub", "safetensors", "pyyaml",
):
    datas += copy_metadata(_pkg)
