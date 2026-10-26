"""Regression coverage for every notification-channel template editor."""
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SOURCE = (ROOT / "webui/src/views/UnifiedPages.jsx").read_text(encoding="utf-8")
CSS = (ROOT / "webui/src/index.css").read_text(encoding="utf-8")


class NotificationTemplateUiTests(unittest.TestCase):
    def test_every_outbound_channel_has_an_event_template_editor(self):
        for channel in ("Webhook", "Telegram", "PushPlus"):
            self.assertIn(f'<MessageTemplateEditor channel="{channel}"', SOURCE)
        self.assertIn("<MessageTemplateEditor channel={activeFeishuChannel.name || activeFeishuChannel.id}", SOURCE)

    def test_editor_exposes_only_the_backend_template_fields(self):
        expected = "{{title}} {{content}} {{event}} {{sim_name}} {{msisdn}} {{from}} {{text}} {{instance}} {{iccid}}"
        self.assertIn(expected, SOURCE)

    def test_each_channel_can_test_the_selected_event(self):
        for method in ("testWebhook", "testTelegram", "testPushPlus", "testFeishu"):
            self.assertIn(f"api.{method}", SOURCE)
        self.assertGreaterEqual(SOURCE.count("_test_event: event"), 4)

    def test_channel_test_buttons_show_and_lock_the_pending_state(self):
        self.assertIn("const [channelTesting, setChannelTesting] = useState('')", SOURCE)
        self.assertIn("disabled={!!channelTesting}", SOURCE)
        self.assertIn("channelTesting === key ? 'Testing…' : 'Test'", SOURCE)
        self.assertGreaterEqual(SOURCE.count("{testButton("), 4)

    def test_feishu_bots_use_stable_ids_and_support_crud_and_line_routing(self):
        self.assertIn("key={channel.id}", SOURCE)
        self.assertIn("const addFeishuChannel = () =>", SOURCE)
        self.assertIn("const removeFeishuChannel = id =>", SOURCE)
        self.assertIn("const toggleFeishuInstance =", SOURCE)
        self.assertIn("instances.map(instance =>", SOURCE)
        self.assertIn("`feishu:${activeFeishuChannel.id}`", SOURCE)
        self.assertIn("const [activeFeishuId, setActiveFeishuId]", SOURCE)
        self.assertIn("channel.id === activeFeishuChannel?.id ? 'active' : ''", SOURCE)
        self.assertIn("activeFeishuChannel && <section", SOURCE)

    def test_event_forwarding_options_are_collapsed_like_template_editors(self):
        self.assertIn('<details className="u-event-options"><summary>', SOURCE)

    def test_notification_channel_types_use_a_responsive_two_by_two_grid(self):
        self.assertIn('className="u-page u-notifications-page"', SOURCE)
        self.assertIn('.u-notifications-page>.u-tabs+.u-device-grid { grid-template-columns:repeat(2,minmax(0,1fr))', CSS)
        self.assertIn('.u-notifications-page>.u-tabs+.u-device-grid { grid-template-columns:1fr;', CSS)


if __name__ == "__main__":
    unittest.main()
