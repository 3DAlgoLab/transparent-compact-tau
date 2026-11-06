"""Single source of the version string.

It lives in its own module because Tau loads a directory extension's entry file
under a synthesized module name with `submodule_search_locations` set, which means
the package's `__init__.py` is **not** executed in that path. Anything the entry
point needs must come from a sibling module reached by a relative import.
"""

__version__ = "0.1.1"
