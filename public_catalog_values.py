"""Lightweight value conversion shared by public catalog generators."""


def public_views(value):
    """Keep measured integer counts, including zero; other values are unknown."""
    return value if type(value) is int else None
