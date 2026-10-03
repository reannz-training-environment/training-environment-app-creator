"""Generate training-environment apps from app specs.

An app spec (one YAML file in apps/) describes a workshop environment: which
interfaces it offers, which HPC features it emulates, its data and its
software. This package turns a spec into one Open OnDemand app per interface,
and creates or updates the GitHub repository for each.
"""

__version__ = "0.1.0"
