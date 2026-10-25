"""Account matching for a verified session."""


def same_account(observed: str | None, expected: str) -> bool:
    return observed is not None and observed.strip().casefold() == expected.strip().casefold() != ""
