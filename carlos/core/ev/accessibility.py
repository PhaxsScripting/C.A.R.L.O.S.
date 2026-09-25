from __future__ import annotations

from ev.platform import executable as _platform_executable

from collections import deque
import time
import math

import re
import subprocess
from pathlib import Path
from typing import Any


class AccessibilityBridge:
    """Narrow AT-SPI semantic inspection/action boundary."""

    def __init__(self) -> None:
        self._import_error = ""
        try:
            import gi

            gi.require_version("Atspi", "2.0")
            from gi.repository import Atspi

            self.Atspi = Atspi
        except Exception as error:  # pragma: no cover - host dependency path
            self.Atspi = None
            self._import_error = str(error)

    def status(self) -> dict[str, Any]:
        if self.Atspi is None:
            return {
                "available": False,
                "status": "UNAVAILABLE",
                "reason": self._import_error or "AT-SPI Python bindings missing",
            }
        enabled = self._status_flag("IsEnabled")
        screen_reader_enabled = self._status_flag("ScreenReaderEnabled")
        applications = self._applications()
        names = [str(item.get_name() or "") for item in applications]
        useful = [name for name in names if name and name != "xdg-desktop-portal-gtk"]
        qt_accessibility = self._qt_accessibility_build()
        if useful:
            reason = "Applications expose semantic AT-SPI trees"
        elif qt_accessibility is False:
            reason = "AT-SPI is enabled, but Gentoo Qt was built without the accessibility USE flag"
        else:
            reason = "AT-SPI is installed, but current applications are not exposing useful semantic trees"
        return {
            "available": True,
            "status": "READY" if useful else "LIMITED",
            "session_enabled": enabled and screen_reader_enabled,
            "is_enabled": enabled,
            "screen_reader_enabled": screen_reader_enabled,
            "application_count": len(applications),
            "applications": names,
            "controllable_application_count": len(useful),
            "qt_accessibility_build": qt_accessibility,
            "reason": reason,
            "network_exposed": False,
        }

    def enable_session(self, enabled: bool) -> dict[str, Any]:
        if self.Atspi is None:
            raise RuntimeError(self._import_error or "AT-SPI is unavailable")
        value = "<true>" if enabled else "<false>"
        results: dict[str, subprocess.CompletedProcess[str]] = {}
        for property_name in ("IsEnabled", "ScreenReaderEnabled"):
            results[property_name] = subprocess.run(
                [
                    _platform_executable("/usr/bin/gdbus"),
                    "call",
                    "--session",
                    "--dest",
                    "org.a11y.Bus",
                    "--object-path",
                    "/org/a11y/bus",
                    "--method",
                    "org.freedesktop.DBus.Properties.Set",
                    "org.a11y.Status",
                    property_name,
                    value,
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=5,
                check=False,
            )
        is_enabled = self._status_flag("IsEnabled")
        screen_reader_enabled = self._status_flag("ScreenReaderEnabled")
        actual = is_enabled and screen_reader_enabled
        command_errors = {
            name: result.stderr.strip()[:300]
            for name, result in results.items()
            if result.returncode != 0
        }
        return {
            "verified": not command_errors and actual is enabled,
            "session_enabled": actual,
            "is_enabled": is_enabled,
            "screen_reader_enabled": screen_reader_enabled,
            "requested": enabled,
            "errors": command_errors,
            "note": "Session-only flag; applications may need to be restarted before they expose AT-SPI",
        }

    def list_elements(
        self,
        application: str = "",
        query: str = "",
        role: str = "",
        limit: int = 100,
        process_id: int = 0,
        window_title: str = "",
    ) -> dict[str, Any]:
        if self.Atspi is None:
            raise RuntimeError(self._import_error or "AT-SPI is unavailable")
        application_words = self._words(application)
        query_words = self._words(query)
        role_words = self._words(role)
        exact_window_scope = process_id > 0 and bool(window_title.strip())
        matches: list[dict[str, Any]] = []
        scanned = 0
        errors = 0
        scoped_roots = 0
        deadline = time.monotonic() + 2.0
        limited = False
        for app in self._applications():
            try:
                app_name = str(app.get_name() or "")
            except Exception:
                errors += 1
                continue
            # A live KWin PID plus exact top-level title is the stronger scope.
            # This also lets natural commands target "the current window"
            # without guessing an AT-SPI application name.
            if (
                application_words
                and not exact_window_scope
                and not application_words.issubset(self._words(app_name))
            ):
                continue
            if process_id and self._process_id(app) != process_id:
                continue
            roots = self._window_roots(app, window_title)
            scoped_roots += len(roots)
            queue = deque((root, 0) for root in roots)
            seen = set()
            while queue and scanned < 2000 and len(matches) < limit and time.monotonic() < deadline:
                node, depth = queue.popleft()
                identity = str(getattr(node, "path", "")) or id(node)
                if identity in seen:
                    continue
                seen.add(identity)
                scanned += 1
                try:
                    serialized = self._serialize(node, app_name)
                    searchable = self._words(f"{serialized['name']} {serialized['description']}")
                    role_tokens = self._words(serialized["role"])
                    if (not query_words or query_words.issubset(searchable)) and (
                        not role_words or role_words.issubset(role_tokens)
                    ):
                        matches.append(serialized)
                    # Web controls commonly live beyond eight wrapper levels.
                    # Increase depth, while keeping a node/time/cycle budget.
                    if depth < 24:
                        for index in range(min(int(node.get_child_count()), 250)):
                            child = node.get_child_at_index(index)
                            if child is not None:
                                queue.append((child, depth + 1))
                    elif int(node.get_child_count()):
                        limited = True
                except Exception:
                    errors += 1
            limited = limited or bool(queue)
        return {
            "elements": matches,
            "count": len(matches),
            "scanned": scanned,
            "errors": errors,
            "truncated": limited or len(matches) >= limit or scanned >= 2000,
            "scope": {
                "process_id": process_id,
                "window_title": window_title,
                "matched_roots": scoped_roots,
            },
            "backend": self.status(),
        }

    def activate(
        self,
        application: str,
        name: str,
        role: str = "",
        process_id: int = 0,
        window_title: str = "",
    ) -> dict[str, Any]:
        node, before = self._resolve_node(application, name, role, process_id, window_title)
        actions = before["actions"]
        supported = ("click", "press", "activate", "open", "toggle")
        # Firefox exposes native hyperlink activation as "jump". This is a
        # link action, not permission to invoke arbitrary named UI actions.
        if before.get("role", "").casefold() == "link":
            supported += ("jump",)
        preferred = next((item for item in supported if item in actions), None)
        if preferred is None:
            raise RuntimeError("resolved element exposes no supported activation action")
        index = actions.index(preferred)
        succeeded = bool(node.do_action(index))
        return {
            "verified": succeeded,
            "action_accepted": succeeded,
            "action": preferred,
            "element": before,
            "evidence": (
                "AT-SPI do_action returned success" if succeeded else "AT-SPI rejected the action"
            ),
        }

    def set_text(
        self,
        application: str,
        name: str,
        text: str,
        role: str = "",
        process_id: int = 0,
        window_title: str = "",
    ) -> dict[str, Any]:
        node, before = self._resolve_node(application, name, role, process_id, window_title)
        if not bool(node.is_editable_text()):
            raise RuntimeError("resolved element is not editable text")
        succeeded = bool(node.set_text_contents(text))
        verified = False
        readback_available = False
        try:
            # PyGObject's current AT-SPI override exposes get_text() as an
            # interface accessor; call the underlying Text API explicitly for
            # the ranged readback.  Treat missing readback as unverified rather
            # than turning a write acknowledgement into a false success.
            observed = str(self.Atspi.Text.get_text(node, 0, -1))
            readback_available = True
            verified = succeeded and observed == text
        except Exception:
            verified = False
        return {
            "verified": verified,
            "write_accepted": succeeded,
            "readback_available": readback_available,
            "element": before,
            "characters": len(text),
            "text_returned": False,
        }

    def _resolve_node(
        self,
        application: str,
        name: str,
        role: str,
        process_id: int = 0,
        window_title: str = "",
    ) -> tuple[Any, dict[str, Any]]:
        listing = self.list_elements(application, name, role, 12, process_id, window_title)
        if listing.get("truncated") or listing.get("errors"):
            raise RuntimeError(
                "Semantic search is incomplete; refusing to select a possibly ambiguous target"
            )
        elements = listing["elements"]
        exact = [item for item in elements if item["name"].casefold() == name.casefold()]
        candidates = exact or elements
        if len(candidates) != 1:
            raise RuntimeError(
                f"semantic target is {'missing' if not candidates else 'ambiguous'}; {len(candidates)} candidates"
            )
        target = candidates[0]
        for app in self._applications():
            app_name = str(app.get_name() or "")
            if app_name != target["application"]:
                continue
            if process_id and self._process_id(app) != process_id:
                continue
            queue = deque((root, 0) for root in self._window_roots(app, window_title))
            seen = set()
            deadline = time.monotonic() + 2.0
            while queue and len(seen) < 2000 and time.monotonic() < deadline:
                node, depth = queue.popleft()
                identity = str(getattr(node, "path", "")) or id(node)
                if identity in seen:
                    continue
                seen.add(identity)
                try:
                    if str(getattr(node, "path", "")) == target["path"]:
                        current = self._serialize(node, app_name)
                        if any(
                            current.get(key) != target.get(key) for key in ("name", "role", "path")
                        ):
                            raise RuntimeError("Semantic target identity changed during resolution")
                        return node, current
                    if depth < 24:
                        for index in range(min(int(node.get_child_count()), 250)):
                            child = node.get_child_at_index(index)
                            if child is not None:
                                queue.append((child, depth + 1))
                except Exception:
                    continue
        raise RuntimeError("semantic target disappeared before execution")

    @staticmethod
    def _process_id(node: Any) -> int:
        try:
            return int(node.get_process_id())
        except Exception:
            return 0

    @staticmethod
    def _title_key(value: str) -> str:
        return re.sub(r"\s+", " ", value.casefold()).strip()

    def _window_roots(self, app: Any, window_title: str) -> list[Any]:
        if not window_title:
            return [app]
        expected = self._title_key(window_title)
        roots: list[Any] = []
        try:
            if self._title_key(str(app.get_name() or "")) == expected:
                roots.append(app)
            for index in range(min(int(app.get_child_count()), 250)):
                child = app.get_child_at_index(index)
                if child is None:
                    continue
                role = str(child.get_role_name() or "").casefold()
                title = self._title_key(str(child.get_name() or ""))
                if title == expected and role in {"frame", "window", "dialog", "file chooser"}:
                    roots.append(child)
        except Exception:
            return []
        # More than one exact root is unsafe; later node names may be identical.
        return roots if len(roots) == 1 else []

    def _serialize(self, node: Any, application: str) -> dict[str, Any]:
        actions: list[str] = []
        try:
            if node.is_action():
                for index in range(min(int(node.get_n_actions()), 20)):
                    actions.append(str(node.get_action_name(index) or "").casefold())
        except Exception:
            actions = []
        states = {}
        try:
            state_set = node.get_state_set()
            for name in (
                "CHECKED",
                "SELECTED",
                "SELECTABLE",
                "ENABLED",
                "SENSITIVE",
                "SHOWING",
                "VISIBLE",
                "FOCUSED",
                "EXPANDED",
                "EDITABLE",
                "READ_ONLY",
                "DEFUNCT",
                "MODAL",
            ):
                states[name.casefold()] = bool(
                    state_set.contains(getattr(self.Atspi.StateType, name))
                )
        except Exception:
            states = {}  # Missing state is unknown, never false.
        return {
            "application": application,
            "path": str(getattr(node, "path", "")),
            "name": str(node.get_name() or ""),
            "description": str(node.get_description() or ""),
            "role": str(node.get_role_name() or "unknown"),
            "actions": actions,
            "editable_text": bool(node.is_editable_text()),
            "value_control": bool(node.is_value()),
            "states": states,
        }

    def browser_document(self, process_id: int, window_title: str):
        """Read the visible top-level document URL, not an address-bar edit.

        Prune document subtrees so embedded frames cannot impersonate the tab.
        No browser profile, network request, focus change or keyboard input.
        """
        from urllib.parse import urlsplit

        if self.Atspi is None or process_id <= 0 or not window_title:
            raise RuntimeError("An exact accessible browser window is required")
        roots = []
        for app in self._applications():
            if self._process_id(app) == process_id:
                roots.extend(self._window_roots(app, window_title))
        if len(roots) != 1:
            raise RuntimeError("Browser window root is missing or ambiguous")
        queue = deque([(roots[0], 0)])
        seen, documents = set(), []
        deadline, partial = time.monotonic() + 2, False
        while queue and len(seen) < 1500 and time.monotonic() < deadline:
            node, depth = queue.popleft()
            path = str(getattr(node, "path", "")) or str(id(node))
            identity = (str(getattr(getattr(node, "app", None), "bus_name", "")), path)
            if identity in seen:
                continue
            seen.add(identity)
            try:
                # ARIA role=document can label an ordinary editable div. It
                # does not implement the native Document interface and cannot
                # supply authoritative top-level browser document metadata.
                if node.is_document():
                    states = node.get_state_set()
                    if not states.contains(self.Atspi.StateType.SHOWING):
                        continue
                    url = self.Atspi.Document.get_document_attribute_value(node, "DocURL")
                    busy = bool(states.contains(self.Atspi.StateType.BUSY))
                    second = self.Atspi.Document.get_document_attribute_value(node, "DocURL")
                    if not isinstance(url, str) or url != second or len(url) > 2000:
                        raise ValueError("Document URL is unavailable or changed")
                    # Firefox's window itself implements Document for browser
                    # chrome. Descend ONLY this exact native root, not arbitrary
                    # internal documents/dialogs that may obscure the real tab.
                    chrome_root = depth == 0 and url == "chrome://browser/content/browser.xhtml"
                    if not chrome_root:
                        parsed = urlsplit(url)
                        if (
                            parsed.scheme not in {"http", "https"}
                            or not parsed.hostname
                            or parsed.username is not None
                            or parsed.password is not None
                        ):
                            raise ValueError("Document URL is unsupported")
                        documents.append({"url": url, "path": path, "busy": busy})
                        continue  # Do not treat an iframe as the enclosing page.
                count = int(node.get_child_count())
                if depth >= 24 and count or count > 250:
                    partial = True
                if depth < 24:
                    for index in range(min(count, 250)):
                        child = node.get_child_at_index(index)
                        if child is not None:
                            queue.append((child, depth + 1))
            except Exception:
                partial = True
        if partial or queue or len(documents) != 1:
            raise RuntimeError("Visible browser document observation is incomplete or ambiguous")
        return {
            **documents[0],
            "process_id": process_id,
            "window_title": window_title,
            "captured_at_monotonic": time.monotonic(),
            "source": "AT-SPI top-level document DocURL",
            "page_content_verified": False,
            "untrusted_external_content": True,
        }

    def inspect_control(self, application, name, role, process_id, window_title, path):
        node, current = self._resolve_node(application, name, role, process_id, window_title)
        if current["path"] != path:
            raise RuntimeError("Control path changed; inspect the window again")
        result = {"element": current, "text_available": False, "value_available": False}
        # Do not read text implicitly from password fields or arbitrary pages.
        if current["role"].casefold() in {"entry", "text", "editable text", "document text"}:
            try:
                count = int(node.get_character_count())
                result.update(
                    text=self.Atspi.Text.get_text(node, 0, min(count, 2000)),
                    text_available=True,
                    text_truncated=count > 2000,
                )
            except Exception:
                pass
        try:
            if node.is_value():
                result.update(
                    value_available=True,
                    value=float(self.Atspi.Value.get_current_value(node)),
                    minimum=float(self.Atspi.Value.get_minimum_value(node)),
                    maximum=float(self.Atspi.Value.get_maximum_value(node)),
                )
        except Exception:
            pass
        return result

    def set_control(self, application, name, role, process_id, window_title, path, action, value):
        node, before = self._resolve_node(application, name, role, process_id, window_title)
        if (
            before["path"] != path
            or before.get("states", {}).get("enabled") is not True
            or before.get("states", {}).get("sensitive") is not True
        ):
            raise RuntimeError(
                "Control changed, is disabled or has no reliable enabled-state evidence"
            )
        role = before["role"]
        if action == "checked":
            if (
                role.casefold() not in {"check box", "toggle button", "radio button"}
                or type(value) is not bool
            ):
                raise ValueError("Checked state requires a boolean and a checkable role")
            current = before["states"].get("checked")
            if type(current) is not bool:
                raise RuntimeError("Checked state unavailable")
            if current == value:
                return {"verified": True, "already_set": True, "action": action}
            index = next(
                (
                    i
                    for i, a in enumerate(before["actions"])
                    if a in {"toggle", "click", "press", "activate"}
                ),
                None,
            )
            if index is None or not node.do_action(index):
                return {"verified": False, "error": "Control rejected activation"}
        elif action == "value":
            if (
                role.casefold() not in {"slider", "spin button", "scroll bar"}
                or type(value) not in (int, float)
                or not math.isfinite(value)
                or not node.is_value()
            ):
                raise ValueError(
                    "A finite value and a value-capable slider/spin control are required"
                )
            low, high = self.Atspi.Value.get_minimum_value(
                node
            ), self.Atspi.Value.get_maximum_value(node)
            if not low <= value <= high:
                raise ValueError("Requested value is outside the control's reported range")
            if not self.Atspi.Value.set_current_value(node, value):
                return {"verified": False, "error": "Control rejected value"}
        elif action == "selected":
            if value is not True or role.casefold() not in {
                "page tab",
                "list item",
                "menu item",
                "table cell",
            }:
                raise ValueError("Selection requires a selectable tab/list/menu item")
            if before["states"].get("selected") is True:
                return {"verified": True, "already_set": True, "action": action}
            if role.casefold() == "table cell":
                # GTK file choosers expose row selection through Table rather
                # than Selection on the cell's parent. Ground the cell again
                # at its reported coordinates before selecting the row.
                if not node.is_table_cell():
                    raise RuntimeError("Cell exposes no native table-cell interface")
                table = self.Atspi.TableCell.get_table(node)
                row, column, row_span, column_span = self.Atspi.TableCell.get_row_column_span(node)
                if (
                    table is None
                    or not table.is_table()
                    or row < 0
                    or column < 0
                    or row_span != 1
                    or column_span != 1
                ):
                    raise RuntimeError("Table cell coordinates are ambiguous")
                observed = self.Atspi.Table.get_accessible_at(table, row, column)
                grounded = observed is not None and str(getattr(observed, "path", "")) == path
                if observed is not None and not grounded:
                    # GTK may expose a container cell with an icon and text
                    # renderer. Only accept a direct, uniquely matching child
                    # of this exact row/column cell, never another row/tree.
                    try:
                        count = int(observed.get_child_count())
                        if 0 < count <= 16:
                            children = [observed.get_child_at_index(i) for i in range(count)]
                            grounded = (
                                sum(str(getattr(child, "path", "")) == path for child in children)
                                == 1
                            )
                    except Exception:
                        grounded = False
                if not grounded:
                    raise RuntimeError(
                        f"Table cell moved before selection (row={row}, column={column}, target={path}, observed={getattr(observed, 'path', '')})"
                    )
                accepted = self.Atspi.Table.add_row_selection(table, row)
            else:
                parent = node.get_parent()
                if not parent or not parent.is_selection():
                    raise RuntimeError("Parent exposes no selection interface")
                accepted = self.Atspi.Selection.select_child(parent, node.get_index_in_parent())
            if not accepted:
                return {"verified": False, "error": "Parent rejected selection"}
        else:
            raise ValueError("Unsupported control state")
        # Independent lookup and state readback; path/name reuse fails closed.
        after = self.inspect_control(application, name, role, process_id, window_title, path)
        actual = (
            after.get("value")
            if action == "value"
            else after["element"].get("states", {}).get(action)
        )
        verified = (
            abs(actual - value) <= 0.001
            if action == "value" and type(actual) in (int, float)
            else type(actual) is bool and actual == value
        )
        return {
            "verified": verified,
            "action": action,
            "actual": actual,
            "verification_scope": "Exact accessible control state readback",
        }

    def focus_empty_control(self, application, name, role, process_id, window_title, path):
        """Focus an exact empty editable field; never activate/submit it."""
        before = self.inspect_control(application, name, role, process_id, window_title, path)
        element = before["element"]
        states = element.get("states", {})
        if (
            element.get("role", "").casefold() not in {"entry", "text", "editable text"}
            or before.get("text_available") is not True
            or before.get("text_truncated")
            or before.get("text") != ""
            or states.get("read_only") is True
            or not all(
                states.get(key) is True for key in ("enabled", "sensitive", "showing", "editable")
            )
        ):
            raise RuntimeError(
                "Only an exact visible, enabled, empty non-password field can receive keyboard text"
            )
        node, current = self._resolve_node(application, name, role, process_id, window_title)
        if current["path"] != path or not node.is_editable_text() or not node.is_component():
            raise RuntimeError("Empty field changed or has no native focus interface")
        if not self.Atspi.Component.grab_focus(node):
            raise RuntimeError("Native field focus request was rejected")
        return {"focus_requested": True, "verified": False}

    def replace_control_text(
        self, application, name, role, process_id, window_title, path, expected, text
    ):
        """Compare-before-write on an exact observed editable control; no submit."""
        node, before = self._resolve_node(application, name, role, process_id, window_title)
        if before["path"] != path or before["role"].casefold() not in {
            "entry",
            "text",
            "editable text",
        }:
            raise RuntimeError("Exact non-password editable control is required")
        states = before.get("states", {})
        if (
            states.get("enabled") is not True
            or states.get("sensitive") is not True
            or not node.is_editable_text()
        ):
            raise RuntimeError("Control is disabled or has no editable-state evidence")
        count = int(node.get_character_count())
        if count < 0 or count > 2000:
            raise RuntimeError("Control text is too large for a complete comparison")
        current = str(self.Atspi.Text.get_text(node, 0, -1))
        if current != expected:
            raise RuntimeError("Control text changed since observation; no text was written")
        if current == text:
            return {
                "verified": True,
                "already_set": True,
                "submitted": False,
                "characters": len(text),
            }
        accepted = bool(node.set_text_contents(text))
        after = self.inspect_control(application, name, role, process_id, window_title, path)
        verified = (
            accepted
            and after.get("text_available") is True
            and not after.get("text_truncated")
            and after.get("text") == text
        )
        return {
            "verified": verified,
            "write_accepted": accepted,
            "submitted": False,
            "characters": len(text),
            "verification_scope": "Exact accessible field text readback; application save/submit not performed",
        }

    def _applications(self) -> list[Any]:
        if self.Atspi is None:
            return []
        try:
            desktop = self.Atspi.get_desktop(0)
            return [desktop.get_child_at_index(index) for index in range(desktop.get_child_count())]
        except Exception:
            return []

    @staticmethod
    def _words(value: str) -> set[str]:
        return set(re.findall(r"[a-z0-9]+", value.casefold()))

    @staticmethod
    def _status_flag(property_name: str) -> bool:
        if property_name not in {"IsEnabled", "ScreenReaderEnabled"}:
            raise ValueError("Unsupported accessibility status property")
        try:
            result = subprocess.run(
                [
                    _platform_executable("/usr/bin/qdbus6"),
                    "org.a11y.Bus",
                    "/org/a11y/bus",
                    "org.freedesktop.DBus.Properties.Get",
                    "org.a11y.Status",
                    property_name,
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=3,
                check=False,
            )
            return result.returncode == 0 and result.stdout.strip().casefold() == "true"
        except (OSError, subprocess.SubprocessError):
            return False

    @staticmethod
    def _enabled() -> bool:
        """Compatibility helper for callers that only need effective state."""

        return AccessibilityBridge._status_flag("IsEnabled") and AccessibilityBridge._status_flag(
            "ScreenReaderEnabled"
        )

    @staticmethod
    def _qt_accessibility_build() -> bool | None:
        package_roots = sorted(Path("/var/db/pkg/dev-qt").glob("qtbase-*/USE"))
        if not package_roots:
            return None
        try:
            return any(
                "accessibility" in path.read_text(encoding="utf-8").split()
                for path in package_roots
            )
        except OSError:
            return None
