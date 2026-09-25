import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from ev.accessibility import AccessibilityBridge
from ev.goals import validate_conditions, verify_conditions
from ev.tools.browser import browser_document


class Node:
    def __init__(self, role, path, *, url="", states=(), children=()):
        self.role, self.path, self.url, self.states, self.children = (
            role,
            path,
            url,
            states,
            children,
        )

    def get_role_name(self):
        return self.role

    def is_document(self):
        return self.role in {"document web", "document frame"}

    def get_state_set(self):
        return SimpleNamespace(contains=lambda state: state in self.states)

    def get_child_count(self):
        return len(self.children)

    def get_child_at_index(self, index):
        return self.children[index]


class BrowserDocumentBridgeTests(unittest.TestCase):
    def setUp(self):
        self.bridge = AccessibilityBridge.__new__(AccessibilityBridge)
        self.bridge.Atspi = SimpleNamespace(
            StateType=SimpleNamespace(SHOWING="showing", BUSY="busy"),
            Document=SimpleNamespace(
                get_document_attribute_value=Mock(side_effect=lambda node, key: node.url)
            ),
        )
        self.document = Node(
            "document web", "/document", url="https://example.com/expected", states={"showing"}
        )
        self.root = Node("frame", "/window", children=[self.document])
        self.bridge._applications = Mock(return_value=[object()])
        self.bridge._process_id = Mock(return_value=123)
        self.bridge._window_roots = Mock(side_effect=lambda *args: [self.root])

    def test_visible_document_prunes_iframes_and_ignores_hidden_tabs(self):
        self.document.children = [
            Node("document web", "/iframe", url="https://other.example/", states={"showing"})
        ]
        self.root.children.append(Node("document web", "/hidden", url="https://hidden.example/"))
        result = self.bridge.browser_document(123, "Exact page")
        self.assertEqual(result["url"], self.document.url)
        self.assertFalse(result["busy"])
        self.assertFalse(result["page_content_verified"])
        self.assertEqual(self.bridge.Atspi.Document.get_document_attribute_value.call_count, 2)

    def test_busy_state_is_not_treated_as_complete_loading(self):
        self.document.states.add("busy")
        self.assertTrue(self.bridge.browser_document(123, "Exact page")["busy"])

    def test_ambiguous_or_unknown_documents_fail_closed(self):
        self.root.children.append(
            Node("document frame", "/second", url="https://other.example/", states={"showing"})
        )
        with self.assertRaises(RuntimeError):
            self.bridge.browser_document(123, "Exact page")
        self.root.children = []
        with self.assertRaises(RuntimeError):
            self.bridge.browser_document(123, "Exact page")

    def test_address_bar_text_cannot_substitute_for_document_url(self):
        self.root.children = [
            Node("entry", "/address", url="https://example.com/expected", states={"showing"})
        ]
        with self.assertRaises(RuntimeError):
            self.bridge.browser_document(123, "Exact page")
        self.bridge.Atspi.Document.get_document_attribute_value.assert_not_called()

    def test_aria_document_role_without_document_interface_is_only_a_container(self):
        container = Node("document frame", "/aria", children=[self.document])
        container.is_document = lambda: False
        self.root.children = [container]
        self.assertEqual(self.bridge.browser_document(123, "Exact page")["url"], self.document.url)

    def test_firefox_native_chrome_root_is_traversed_not_mistaken_for_page(self):
        self.root.is_document = lambda: True
        self.root.url = "chrome://browser/content/browser.xhtml"
        self.root.states = {"showing"}
        self.assertEqual(self.bridge.browser_document(123, "Exact page")["url"], self.document.url)

    def test_visible_internal_dialog_cannot_be_ignored_to_claim_underlying_page(self):
        self.root.children.append(
            Node(
                "document web",
                "/dialog",
                url="chrome://browser/content/spotlight.html",
                states={"showing"},
            )
        )
        with self.assertRaises(RuntimeError):
            self.bridge.browser_document(123, "Exact page")

    def test_cross_bus_paths_do_not_hide_ambiguous_visible_documents(self):
        other = Node(
            "document web", self.document.path, url="https://other.example/", states={"showing"}
        )
        self.document.app = SimpleNamespace(bus_name=":1.1")
        other.app = SimpleNamespace(bus_name=":1.2")
        self.root.children.append(other)
        with self.assertRaises(RuntimeError):
            self.bridge.browser_document(123, "Exact page")

    def test_changed_unsupported_or_credentialled_document_url_is_rejected(self):
        for url in ("file:///etc/passwd", "about:blank", "https://user:secret@example.com/", ""):
            self.document.url = url
            with self.assertRaises(RuntimeError):
                self.bridge.browser_document(123, "Exact page")
        self.bridge.Atspi.Document.get_document_attribute_value.side_effect = [
            "https://example.com/a",
            "https://example.com/b",
        ]
        with self.assertRaises(RuntimeError):
            self.bridge.browser_document(123, "Exact page")

    def test_partial_tree_refuses_even_a_single_found_document(self):
        self.root.children += [Node("label", f"/{index}") for index in range(250)]
        with self.assertRaises(RuntimeError):
            self.bridge.browser_document(123, "Exact page")


class BrowserDocumentGoalTests(unittest.IsolatedAsyncioTestCase):
    async def test_exact_fresh_nonbusy_document_url_satisfies_only_declared_condition(self):
        condition = {
            "kind": "browser_url",
            "window_id": "exact",
            "expected": "https://example.com/page",
        }
        data = {
            "window_id": "exact",
            "url": condition["expected"],
            "busy": False,
            "captured_at_monotonic": time.monotonic(),
        }
        requester = AsyncMock(return_value={"status": "completed", "result": data})
        result = await verify_conditions([condition], requester, "fixture")
        self.assertTrue(result["verified"])
        self.assertEqual(result["scope"], "declared_conditions")
        self.assertEqual(requester.await_args.args[0]["name"], "browser.document")
        for change in (
            {"busy": True},
            {"busy": None},
            {"window_id": "other"},
            {"url": "https://example.com/page?different"},
            {"captured_at_monotonic": time.monotonic() - 3},
            {"captured_at_monotonic": time.monotonic() + 10},
        ):
            requester.return_value = {"status": "completed", "result": {**data, **change}}
            self.assertFalse(
                (await verify_conditions([condition], requester, "fixture"))["verified"]
            )

    async def test_focus_change_discards_url(self):
        context = SimpleNamespace(
            accessibility=SimpleNamespace(
                browser_document=Mock(return_value={"url": "https://example.com/"})
            )
        )
        with patch(
            "ev.tools.browser.browser_scope",
            new=AsyncMock(side_effect=[(123, "Page"), (456, "Other")]),
        ):
            with self.assertRaises(RuntimeError):
                await browser_document({"window_id": "exact"}, context)

    def test_condition_rejects_nonweb_urls_and_missing_identity(self):
        for condition in (
            {"kind": "browser_url", "expected": "https://example.com/"},
            {"kind": "browser_url", "window_id": "exact", "expected": "file:///private"},
        ):
            with self.assertRaises(ValueError):
                validate_conditions([condition])
