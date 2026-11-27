"""Platform suffix of a SystemTest entry-point name: test_x_win / test_x_mac / test_x_linux.

One place knows the convention. To add an OS, add ONE row to OS_SUFFIX (suffix -> run.platform value); the
case grouping in db.py, the SQL name matching and the report guard all read it from here.

Names recorded before the _win rename carry no suffix and mean Windows (LEGACY_SUFFIX). So 'test_x' and 'test_x_win'
are the same case across the rename, and 'test_x_mac' is a different one.
"""

# suffix -> test_run.platform value
OS_SUFFIX = {"win": "windows", "mac": "mac", "linux": "linux"}
LEGACY_SUFFIX = "win"  # a name with no suffix was written before the rename and is a Windows test
DEFAULT_PLATFORM_SUFFIX = "win"  # the variant a short pattern resolves to when it names no OS

_SUFFIX_RE = "_(" + "|".join(OS_SUFFIX) + ")$"


def split_os(name):
    """('test_x_mac') -> ('test_x', 'mac'); an unsuffixed legacy name -> ('test_x', 'win')."""
    name = str(name)
    for suffix in OS_SUFFIX:
        if name.endswith("_" + suffix):
            return name[: -len(suffix) - 1], suffix
    return name, LEGACY_SUFFIX


def canonical_name(name):
    """The name as it is written today: legacy unsuffixed -> test_x_win, suffixed names unchanged."""
    base, suffix = split_os(name)
    return f"{base}_{suffix}"


def platform_of(name):
    """test_run.platform value this name belongs on."""
    return OS_SUFFIX[split_os(name)[1]]


def canon_sql(expr):
    """SQL that gives canonical_name(expr): test_x -> test_x_win, test_x_mac stays. Lets a query match a case
    across the rename without rewriting stored history."""
    return f"(CASE WHEN {expr} ~ '{_SUFFIX_RE}' THEN {expr} ELSE {expr} || '_{LEGACY_SUFFIX}' END)"


def platform_guard_sql(name_col="tr.test_name", platform_col="run.platform"):
    """SQL condition: the OS in the test name must agree with the run's platform."""
    whens = " ".join(f"WHEN {name_col} ~ '_{s}$' THEN '{p}'" for s, p in OS_SUFFIX.items())
    return f"(CASE {whens} ELSE '{OS_SUFFIX[LEGACY_SUFFIX]}' END) = {platform_col}"
