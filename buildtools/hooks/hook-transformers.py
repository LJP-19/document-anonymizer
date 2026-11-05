"""Custom PyInstaller hook for `transformers`.

## History - two attempts before this one, both insufficient on real CI

Attempt 1 (v0.37.10): added "transformers" to build.py's COLLECT_ALL
(--collect-all). Did not fix it - the crash reproduced identically on a
real macOS CI run.

Attempt 2 (v0.37.12): a custom hook using
`collect_data_files("transformers", include_py_files=True)` plus
`copy_metadata()` for the specific packages transformers checks at
startup. Verified independently that this data collection logic produces
the correct file list when called directly in Python - but the crash
STILL reproduced identically on BOTH a real Windows and a real macOS CI
run, with the exact same FileNotFoundError on
transformers/models/__init__.pyc. Correct data in isolation did not
translate into correct behavior in a real PyInstaller Analysis pass -
`include_py_files=True` on `collect_data_files` is evidently not
equivalent, in practice, to PyInstaller's own dedicated mechanism for
this exact problem.

## What this attempt does differently

`pyinstaller-hooks-contrib` (a REQUIRED, hard dependency of pyinstaller
itself - confirmed directly: `pip show pyinstaller` lists it under
Requires) already ships its own hook-transformers.py, discovered
automatically without needing --additional-hooks-dir at all. It uses a
DIFFERENT, PyInstaller-native mechanism this project's own hook was not
using at all:

    module_collection_mode = 'pyz+py'

This is a module-level hook variable PyInstaller's Analysis pass reads
directly (not a function call) - it tells PyInstaller to collect this
package BOTH compiled into the PYZ archive AND as genuine loose .py
source files, using PyInstaller's own loader machinery rather than a
hand-assembled `datas` list. PyInstaller's own documentation calls this
"the preferred way of collecting source .py files" over
collect_data_files(..., include_py_files=True). Adding a CUSTOM hook for
transformers (attempt 2, above) likely SHADOWED this already-existing
contrib hook - PyInstaller uses one hook per module name, not a merge of
several - which may be why attempt 2 made no observable difference: it
replaced a community-maintained, battle-tested mechanism with a narrower,
unproven one of my own.

This hook now sets `module_collection_mode = 'pyz+py'` directly (adopting
the proven mechanism rather than guessing at an alternative), keeps the
include_py_files data collection as a harmless, redundant supplement, and
reuses the contrib hook's OWN broader metadata-copying technique -
checking transformers' FULL dependency table (not just the narrower
startup-check subset) and copying metadata only for whatever is actually
installed in the build environment. Verified this exact technique
directly against the real installed packages before adopting it.

## Attempt 3 (this one): 'pyz+py' reproduced the IDENTICAL crash on a real
## macOS CI run - traced the actual mechanism this time, not just tried
## another plausible-sounding option

Read PyInstaller's own loader source directly
(PyInstaller/loader/pyimod02_importers.py, `_fixup_frozen_stdlib`) rather
than guess again. `module_collection_mode = 'pyz+py'` keeps the `PYZ`
(archived) flag set alongside `PY` (external source file) - it does not
replace the archived copy, it adds a second, additional copy on disk. The
loader's own comment is explicit: for a module that is still loaded from
the archive, `__file__` is SYNTHESIZED as
`os.path.join(sys._MEIPASS, *orig_name.split('.')) + '.pyc'` - "to be
consistent with our PyiFrozenLoader" - a conventional path that may not
correspond to a real file, regardless of whether an external `.py` copy
also exists elsewhere. transformers.models reads `_file =
globals()["__file__"]` (confirmed directly: in a normal unfrozen
environment this is exactly the module's own real path) and passes it to
`os.listdir()`. As long as `PYZ` stays set, the archived copy is what
actually gets imported, `__file__` stays synthetic, and the extra loose
`.py` copy the `PY` flag placed elsewhere is never consulted by anything
- explaining, precisely, why 'pyz+py' reproduced the identical crash.

Changed to `module_collection_mode = "py"` - PY only, no PYZ at all - so
there is no archived alternative for the loader to prefer. This forces
transformers (and, via PyInstaller's own top-down mode inheritance, its
whole subpackage tree unless something more specific overrides it) to be
loaded from the real, on-disk external file, giving it a genuine,
resolvable `__file__` whose directory actually contains its sibling
submodules.

## What is still unverified

None of this can be proven from this sandbox - it has no Windows or
macOS PyInstaller build capability. Two independently-reasoned fixes have
now failed identically on real CI. This one is built on having actually
read the loader code responsible for the exact symptom, rather than
another plausible-sounding option - a stronger basis than either prior
attempt, but not proof. The next real build is still the only real test.
"""

from PyInstaller.utils.hooks import (
    collect_data_files,
    collect_submodules,
    copy_metadata,
    get_module_attribute,
    is_module_satisfies,
    logger,
)

datas = collect_data_files("transformers", include_py_files=True)
hiddenimports = collect_submodules("transformers")

# The PyInstaller-native, community-proven mechanism for "this package
# needs its own real source files at runtime" - see docstring above.
module_collection_mode = "py"

# Reuses pyinstaller-hooks-contrib's own technique exactly: check
# transformers' FULL dependency table (not just the narrower subset its
# startup check verifies - other code paths query other packages' metadata
# too), and copy metadata only for what is actually installed in THIS
# build environment, so nothing gets bundled that was never there and
# nothing real gets missed.
try:
    _deps = get_module_attribute("transformers.dependency_versions_table", "deps")
except Exception:
    logger.warning(
        "hook-transformers: failed to query dependency table "
        "(transformers.dependency_versions_table.deps)!",
        exc_info=True,
    )
    _deps = {}

for _name, _req in _deps.items():
    if not is_module_satisfies(_req):
        continue
    try:
        datas += copy_metadata(_name)
    except Exception:
        pass
