class WildberriesOrdersError(Exception):
    """Base error for Wildberries orders client."""


class WildberriesAuthError(WildberriesOrdersError):
    """Session expired or user is not logged in."""


class WildberriesAntibotError(WildberriesOrdersError):
    """Antibot blocked the request (often 498 + HTML)."""
