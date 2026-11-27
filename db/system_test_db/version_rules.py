"""Product-version rules shared by the final cut (final_snapshot.py, db.py clone-release).

A patch line is a build X.M.N.build with M = 1..99, for example 140.1.0.2781 and 140.1.4.2805: a patch of a monthly release, not
the monthly build itself (X.0.0.build) and not a feature build (X.100.100.build). A final cut never contains a patch line, so
its runs are dropped when a release is cloned into release-<N>-final and are ignored when the keep list is classified.

report.py keeps its own, wider NONEHF_DROP_RE (EHF builds X.0.N.build as well) for the "Non-EHF" page filter; this module is the
narrower rule the final cut applies.
"""

import re

PATCH_LINE_RE = r"^\d+\.[1-9]\d?\.\d+\.\d+$"
_PATCH_LINE = re.compile(PATCH_LINE_RE)


def is_patch_line(version):
    """True for a product version such as 140.1.4.2805; False for None, a monthly, an EHF or a feature build."""
    return bool(version and _PATCH_LINE.match(version))
