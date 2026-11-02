"""Tests for the project prefix on session names (D19): which directory names the project, and how a name gets the prefix."""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

import support  # noqa: F401  (puts src/ on sys.path)
from gitrepo import GIT, git

from test_operations import lazy

naming = lazy("jam.naming")


class ProjectLabelTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.root, True)

    def folder(self, *parts):
        path = self.root.joinpath(*parts)
        path.mkdir(parents=True)
        return path

    def test_a_directory_outside_any_repository_names_itself(self):
        self.assertEqual(naming.project_label(str(self.folder("notes"))), "notes")

    def test_a_directory_inside_a_repository_names_the_repository(self):
        (self.folder("shop") / ".git").mkdir()
        self.assertEqual(naming.project_label(str(self.folder("shop", "src", "api"))), "shop")

    @unittest.skipIf(GIT is None, "needs git")
    def test_a_linked_worktree_names_its_main_checkout_not_itself(self):
        home, main = self.folder("home"), self.folder("shop")
        git(home, main, "init", "-q")
        git(home, main, "commit", "-q", "--allow-empty", "-m", "init")
        worktree = main / ".claude" / "worktrees" / "fix-login"
        git(home, main, "worktree", "add", "-q", "-b", "fix-login", str(worktree))
        self.assertEqual(naming.project_label(str(worktree)), "shop")

    def test_the_name_is_cut_down_to_the_characters_a_session_name_allows(self):
        cases = {"my project!": "my-project", ".config": "config", "--x--": "x", "a.b_c-d": "a.b_c-d", "Café 2": "Caf-2"}
        for directory, label in cases.items():
            with self.subTest(directory=directory):
                self.assertEqual(naming.project_label(str(self.folder(directory))), label)

    def test_nothing_usable_is_no_label(self):
        self.assertIsNone(naming.project_label(str(self.folder("日本語"))))
        self.assertIsNone(naming.project_label("/"))
        for bad in ("relative/dir", "", None, 5, "/work/\0x"):
            with self.subTest(bad=bad):
                self.assertIsNone(naming.project_label(bad))

    def test_a_path_object_is_accepted(self):
        self.assertEqual(naming.project_label(self.folder("notes")), "notes")


class PrefixedTest(unittest.TestCase):
    def test_a_name_gets_the_project_and_a_dash_in_front(self):
        self.assertEqual(naming.prefixed("fix-tests", "shop"), "shop-fix-tests")

    def test_a_name_that_already_starts_with_the_project_is_left_alone(self):
        for name in ("shop", "shop-fix-tests", "Shop-fix", "SHOP"):
            with self.subTest(name=name):
                self.assertEqual(naming.prefixed(name, "shop"), name)

    def test_a_name_that_only_starts_with_the_same_letters_still_gets_the_prefix(self):
        self.assertEqual(naming.prefixed("shopping", "shop"), "shop-shopping")

    def test_no_name_stays_no_name_so_the_harness_keeps_its_own_title(self):
        self.assertIsNone(naming.prefixed(None, "shop"))

    def test_without_a_label_the_name_is_unchanged(self):
        self.assertEqual(naming.prefixed("fix-tests", None), "fix-tests")

    def test_an_invalid_name_is_left_for_the_harness_to_refuse(self):
        for name in ("-flag", "has space", "n" * 65, ""):
            with self.subTest(name=name):
                self.assertEqual(naming.prefixed(name, "shop"), name)

    def test_the_label_is_shortened_to_fit_and_the_callers_part_is_never_cut(self):
        name = "n" * 60
        self.assertEqual(naming.prefixed(name, "shop-front"), "sho-" + name)  # three characters of the label fit
        self.assertEqual(naming.prefixed("n" * 62, "shop"), "s-" + "n" * 62)
        self.assertEqual(naming.prefixed("n" * 61, "a-bc"), "a-" + "n" * 61)  # a cut label never ends in a separator

    def test_a_label_with_no_room_left_is_left_off(self):
        self.assertEqual(naming.prefixed("n" * 63, "shop"), "n" * 63)
        self.assertEqual(naming.prefixed("n" * 64, "shop"), "n" * 64)

    def test_every_result_is_a_valid_name(self):
        session = lazy("jam.session")
        for name in ("a", "x" * 40, "y" * 62, "z" * 64):
            for label in ("p", "proj.v2", "a" * 64):
                with self.subTest(name=len(name), label=label):
                    self.assertTrue(session.is_id(naming.prefixed(name, label)))


if __name__ == "__main__":
    unittest.main()
