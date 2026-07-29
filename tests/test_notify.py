import importlib
import os
import unittest
from unittest.mock import MagicMock, patch

import notify


class NotificationConfigurationTests(unittest.TestCase):
    def test_direct_import_loads_dotenv_before_reading_notification_flags(self):
        original_enabled = os.environ.pop("SLACK_ENABLED", None)
        original_webhook = os.environ.pop("SLACK_WEBHOOK_URL", None)

        def load_test_environment():
            os.environ["SLACK_ENABLED"] = "1"
            os.environ["SLACK_WEBHOOK_URL"] = "https://hooks.slack.test/example"

        try:
            with patch(
                "dotenv.load_dotenv",
                side_effect=load_test_environment,
            ) as load_dotenv:
                reloaded = importlib.reload(notify)

            load_dotenv.assert_called_once_with()
            self.assertTrue(reloaded.SLACK_ENABLED)
            self.assertEqual(
                reloaded.SLACK_WEBHOOK_URL,
                "https://hooks.slack.test/example",
            )
        finally:
            if original_enabled is None:
                os.environ.pop("SLACK_ENABLED", None)
            else:
                os.environ["SLACK_ENABLED"] = original_enabled
            if original_webhook is None:
                os.environ.pop("SLACK_WEBHOOK_URL", None)
            else:
                os.environ["SLACK_WEBHOOK_URL"] = original_webhook
            importlib.reload(notify)


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
