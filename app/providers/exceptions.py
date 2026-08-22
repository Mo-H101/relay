class ProviderError(Exception):
    """
    Base exception for provider-related failures.
    """


class ProviderTimeout(ProviderError):
    """
    Provider did not respond before timeout.
    """


class ProviderResponseLimit(ProviderError):
    """Provider response exceeded Relay's byte, chunk, or time budget."""


class ProviderHTTPError(ProviderError):
    """
    Provider returned an HTTP error.
    """

    def __init__(
        self,
        status_code: int,
        message: str,
        retry_after: float | None = None,
    ):
        self.status_code = status_code
        self.message = message
        self.retry_after = retry_after

        super().__init__(
            f"HTTP {status_code}: {message}"
        )
