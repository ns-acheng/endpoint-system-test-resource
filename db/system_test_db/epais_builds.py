"""The client builds that belong to the R144 EPAIS line, in one place.

A feature build is numbered like a release (142.100.100.<build>) but is tested for the current line, so the report page of
release-144 must show it. Not every feature build is an EPAIS build, so the list is explicit: the owner names the versions
(2026-11-11: 10608, 10603, 10595, 10575, 10542, 10528) and gives new ones when a build is added. Nothing else belongs here.
"""

EPAIS_144_BUILDS = (
    "142.100.100.10608",
    "142.100.100.10603",
    "142.100.100.10595",
    "142.100.100.10575",
    "142.100.100.10542",
    "142.100.100.10528",
)
