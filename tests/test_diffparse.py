from catchrate.diffparse import parse_diff, to_ranges

DIFF_U0 = """\
diff --git a/pkg/calc.py b/pkg/calc.py
index 1111111..2222222 100644
--- a/pkg/calc.py
+++ b/pkg/calc.py
@@ -2 +2 @@ def inc(x):
-    return x - 1
+    return x + 1
@@ -10,0 +11,2 @@ def dec(x):
+    if x is None:
+        return None
@@ -20,3 +22 @@ def mul(x, y):
-    a = 1
-    b = 2
-    c = 3
+    abc = 6
diff --git a/tests/calc/tests.py b/tests/calc/tests.py
new file mode 100644
index 0000000..3333333
--- /dev/null
+++ b/tests/calc/tests.py
@@ -0,0 +1,3 @@
+class IncTests:
+    def test_inc(self):
+        assert inc(1) == 2
diff --git a/pkg/old.py b/pkg/new.py
similarity index 90%
rename from pkg/old.py
rename to pkg/new.py
index 4444444..5555555 100644
--- a/pkg/old.py
+++ b/pkg/new.py
@@ -1 +1 @@
-x = 1
+x = 2
diff --git a/pkg/gone.py b/pkg/gone.py
deleted file mode 100644
index 6666666..0000000
--- a/pkg/gone.py
+++ /dev/null
@@ -1,2 +0,0 @@
-a = 1
-b = 2
diff --git a/pkg/logo.png b/pkg/logo.png
index 7777777..8888888 100644
Binary files a/pkg/logo.png and b/pkg/logo.png differ
"""


def test_parses_line_numbers_and_paths_from_zero_context_diff():
    calc, test, renamed, gone, logo = parse_diff(DIFF_U0)

    assert (calc.old_path, calc.new_path) == ("pkg/calc.py", "pkg/calc.py")
    assert calc.removed_lines() == [2, 20, 21, 22]
    assert calc.added_lines() == [2, 11, 12, 22]
    assert calc.insertion_points() == [10]
    assert calc.hunks[0].removed == ((2, "    return x - 1"),)
    assert calc.hunks[0].added == ((2, "    return x + 1"),)

    assert (test.old_path, test.new_path) == (None, "tests/calc/tests.py")
    assert test.added_lines() == [1, 2, 3]
    assert test.insertion_points() == [0]

    assert (renamed.old_path, renamed.new_path) == ("pkg/old.py", "pkg/new.py")
    assert (gone.old_path, gone.new_path) == ("pkg/gone.py", None)
    assert gone.removed_lines() == [1, 2]
    assert logo.binary and logo.path == "pkg/logo.png" and not logo.hunks


def test_context_lines_are_not_reported_as_changes():
    diff = """\
diff --git a/f.py b/f.py
--- a/f.py
+++ b/f.py
@@ -3,6 +3,7 @@
 a
-b
+B
 c
 d
+e
 f
 g
"""
    (fd,) = parse_diff(diff)
    assert fd.removed_lines() == [4]
    assert fd.added_lines() == [4, 7]
    assert fd.insertion_points() == [6]  # "e" was inserted after old line 6 ("d")


def test_deletion_points_mark_where_lines_disappeared():
    diff = """\
diff --git a/t.py b/t.py
--- a/t.py
+++ b/t.py
@@ -9 +8,0 @@ class T:
-    @expectedFailure
@@ -20 +19 @@ class T:
-        x = 1
+        x = 2
"""
    (fd,) = parse_diff(diff)
    assert fd.deletion_points() == [8]  # the replacement at line 20 is not a deletion
    assert fd.removed_lines() == [9, 20]
    assert fd.added_lines() == [19]


def test_no_newline_markers_are_ignored():
    diff = """\
diff --git a/a.txt b/a.txt
--- a/a.txt
+++ b/a.txt
@@ -1 +1 @@
-old
\\ No newline at end of file
+new
\\ No newline at end of file
"""
    (fd,) = parse_diff(diff)
    assert fd.hunks[0].removed == ((1, "old"),)
    assert fd.hunks[0].added == ((1, "new"),)


def test_removed_lines_that_look_like_headers_stay_in_the_hunk():
    diff = """\
diff --git a/a.txt b/a.txt
--- a/a.txt
+++ b/a.txt
@@ -1,2 +1 @@
--- not a header
-+++ nor this
+ok
"""
    (fd,) = parse_diff(diff)
    assert (fd.old_path, fd.new_path) == ("a.txt", "a.txt")
    assert [t for _, t in fd.hunks[0].removed] == ["-- not a header", "+++ nor this"]


def test_to_ranges():
    assert to_ranges([5, 1, 2, 3, 7, 8, 3]) == [(1, 3), (5, 5), (7, 8)]
    assert to_ranges([]) == []
