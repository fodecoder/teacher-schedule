"""Single source of truth for the project version (SemVer).

Bump this on every functionally relevant change:

* PATCH (0.1.x): bug fixes, no behaviour/config-schema change.
* MINOR (0.x.0): backward-compatible additions (new optional config keys,
  new CLI flags, new SOFT/MEDIUM constraints behind a weight).
* MAJOR (x.0.0): breaking change to the config schema, the CLI, or the
  output JSON schema (``docs/schema_output.json``).
"""

__version__ = "0.1.0"
