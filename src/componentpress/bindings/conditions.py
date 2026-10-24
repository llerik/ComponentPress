"""Pure evaluation of the four public conditional-group operators."""


def condition_matches(operator: str, value: str, empty: bool, expected: str | None) -> bool:
    """Evaluate a checked cell; callers diagnose missing/error cells first."""
    return {
        "equals": value == expected,
        "not_equals": value != expected,
        "is_empty": empty,
        "is_not_empty": not empty,
    }[operator]
