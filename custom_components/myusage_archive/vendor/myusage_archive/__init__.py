"""myusage-archive: unofficial archiver for Exceleron MyUsage utility portals.

Not affiliated with, endorsed by, or supported by Exceleron Software, LLC or
Orlando Utilities Commission. MyUsage is a mark of Exceleron Software.
"""

from .client import LoginAttempt, LoginMode, MyUsageClient
from .exceptions import (
    ArchiveVersionError,
    AuthenticationError,
    DataError,
    LayoutError,
    MfaRequiredError,
    MyUsageError,
    SessionExpiredError,
    TransportError,
    UnsupportedAccountError,
)

__version__ = "0.1.0"

__all__ = [
    "ArchiveVersionError",
    "AuthenticationError",
    "DataError",
    "LayoutError",
    "LoginAttempt",
    "LoginMode",
    "MfaRequiredError",
    "MyUsageClient",
    "MyUsageError",
    "SessionExpiredError",
    "TransportError",
    "UnsupportedAccountError",
    "__version__",
]
