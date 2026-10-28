from pr_review_agent.diff import parse_patch

PATCH = """\
@@ -1,4 +1,5 @@
 import os
-import sys
+import json
+import re

 def main():
@@ -20,2 +21,3 @@ def other():
     a = 1
+    b = 2
\\ No newline at end of file"""


def test_line_numbers_follow_hunks() -> None:
    diff = parse_patch("app.py", PATCH)
    added = [(line.new_line, line.content) for line in diff.added]
    assert added == [(2, "import json"), (3, "import re"), (22, "    b = 2")]


def test_commentable_lines_include_context_but_not_removed() -> None:
    diff = parse_patch("app.py", PATCH)
    assert diff.commentable_lines == {1, 2, 3, 4, 5, 21, 22}


def test_annotated_output_has_new_line_numbers() -> None:
    text = parse_patch("app.py", PATCH).annotated()
    assert "     2 +import json" in text
    assert "       -import sys" in text
    assert "@@ -20,2 +21,3 @@" in text


def test_empty_patch() -> None:
    diff = parse_patch("image.png", None)
    assert diff.lines == [] and diff.commentable_lines == set()
