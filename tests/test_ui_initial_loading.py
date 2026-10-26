"""Regression coverage for truthful asynchronous UI states."""
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SOFTPHONE = (ROOT / "webui/src/views/Softphone.jsx").read_text(encoding="utf-8")
DEVICES = (ROOT / "webui/src/views/UnifiedPages.jsx").read_text(encoding="utf-8")
APP = (ROOT / "webui/src/App.jsx").read_text(encoding="utf-8")
ESIM = (ROOT / "webui/src/views/Esim.jsx").read_text(encoding="utf-8")
LOGS = (ROOT / "webui/src/views/Logs.jsx").read_text(encoding="utf-8")


class InitialLoadingUiTests(unittest.TestCase):
    def test_softphone_does_not_report_unregistered_before_first_registration(self):
        self.assertIn("const [reg, setReg] = useState('loading')", SOFTPHONE)
        self.assertIn("registeredOnce.current ? 'unregistered' : 'connecting'", SOFTPHONE)
        self.assertIn("registeredOnce.current = true", SOFTPHONE)

    def test_softphone_disabled_notice_waits_for_provisioning(self):
        self.assertIn("prov && !prov.enabled", SOFTPHONE)
        self.assertNotIn("!prov?.enabled", SOFTPHONE)

    def test_device_pages_wait_for_the_first_hardware_scan(self):
        self.assertIn("const pending = discovering", DEVICES)
        self.assertIn("if (discovering) return <Discovering t={t} />", DEVICES)

    def test_global_line_views_wait_for_the_first_snapshot(self):
        self.assertIn("const [initialLoading, setInitialLoading] = useState(true)", APP)
        self.assertIn("const [loadErrors, setLoadErrors] = useState({})", APP)
        self.assertIn("initialLoading,loadErrors,refreshDevices", APP)
        self.assertIn("if (initialLoading && !id)", SOFTPHONE)
        self.assertIn("if (initialLoading && !present.length)", ESIM)
        self.assertIn("loadErrors?.cards && !present.length", ESIM)

    def test_empty_and_unavailable_states_are_not_used_while_loading(self):
        self.assertIn("historyLoading && calls.length === 0", SOFTPHONE)
        self.assertIn("!historyLoading && calls.length === 0", SOFTPHONE)
        self.assertIn("statusLoading", ESIM)
        self.assertNotIn("setStatus({ available: false })", ESIM)
        self.assertIn("loading ? `${t('Loading')}…`", LOGS)

    def test_settings_failures_do_not_create_fake_empty_configuration(self):
        self.assertNotIn("catch(() => setS({ proxy: {} }))", DEVICES)
        self.assertNotIn("catch(() => setS({ webhook: {}, telegram: {}, pushplus: {} }))", DEVICES)
        self.assertNotIn("catch(() => setS({ tls: {}, retry: {}", DEVICES)


if __name__ == "__main__":
    unittest.main()
