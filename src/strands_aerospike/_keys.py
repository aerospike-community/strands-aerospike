"""Identifier validation for composite Aerospike session keys."""


def validate_identifier(identifier: str, kind: str) -> str:
    """Validate a session/agent/multi-agent id used to build a composite key.

    Composite keys join segments with ``:``, so an id containing that character
    would let one segment bleed into the next and collide with unrelated records.

    Args:
        identifier: The id to validate.
        kind: A human-readable label for the id's role, used in the error message.

    Returns:
        The identifier, unchanged.

    Raises:
        ValueError: If the identifier is empty or contains a ``:``.
    """
    if not identifier:
        raise ValueError(f"{kind} id must not be empty")
    if ":" in identifier:
        raise ValueError(f"{kind} id must not contain ':': {identifier!r}")
    return identifier
