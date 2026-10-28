"""WebUI after the systemd move (SLIM_PLAN 12.11-6): no Docker or relay controls, a line limit
of one or two, and a compliance notice that says so."""
import re
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "webui" / "src"


def source(*names):
    return "\n".join((SRC / name).read_text() for name in names)


class SlimUiTests(unittest.TestCase):
    def test_no_docker_maintenance_or_storage(self):
        text = source("views/UnifiedPages.jsx", "i18n.jsx", "api.js")
        for needle in ("prune_build_cache", "prune_old_images", "docker_images_bytes",
                       "build_cache_bytes", "container_writable_bytes", "Docker build cache",
                       "rollback images", "container mode"):
            self.assertNotIn(needle, text)

    def test_no_media_relay(self):
        text = source("softphone.js", "GlobalSoftphone.jsx", "views/Softphone.jsx",
                      "views/UnifiedPages.jsx", "i18n.jsx")
        for needle in ("relayunavailable", "relayunreachable", "RELAY_UNAVAILABLE",
                       "iceTransportPolicy", "MediaModePanel", "install.sh media"):
            self.assertNotIn(needle, text)
        self.assertIn("iceServers: []", source("softphone.js"))

    def test_line_limit_setting(self):
        pages = source("views/UnifiedPages.jsx")
        self.assertIn("max_lines", pages)
        self.assertIn("line_limit?.product_max", pages)
        zh = source("i18n.jsx")
        self.assertIn("'At most {count} line(s)': '最多 {count} 条'", zh)

    def test_compliance_notice_names_the_ceiling(self):
        text = source("i18n.jsx")
        self.assertIn("最多 10 条 SIM 线路", text)
        self.assertIn("at most ten SIM lines", text)
        self.assertNotRegex(text, r"最多 ?2 ?条 SIM|two SIM lines")


if __name__ == "__main__":
    unittest.main()
