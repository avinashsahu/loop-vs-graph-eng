import unittest
from unittest.mock import MagicMock, patch

import notify


class EmailNotificationTests(unittest.TestCase):
    def test_smtp_delivery_uses_the_configured_timeout(self):
        smtp = MagicMock()
        smtp.__enter__.return_value = smtp

        with (
            patch.object(notify, "EMAIL_ENABLED", True),
            patch.object(notify, "SMTP_HOST", "smtp.test"),
            patch.object(notify, "SMTP_PORT", 587),
            patch.object(notify, "SMTP_TIMEOUT_SECONDS", 7.5),
            patch.object(notify.smtplib, "SMTP", return_value=smtp) as smtp_factory,
        ):
            notify.send_email("subject", "body", recipients=["alerts@example.test"])

        smtp_factory.assert_called_once_with("smtp.test", 587, timeout=7.5)


if __name__ == "__main__":
    unittest.main()
