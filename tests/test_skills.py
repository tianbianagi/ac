import tempfile
import unittest
from pathlib import Path

from ac import skills


def write_skill(base, name, body="Do the thing.", description="A skill"):
    path = Path(base) / name / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\nname: {name}\ndescription: {description}\n---\n\n{body}\n")
    return path


class ParseTest(unittest.TestCase):
    def test_frontmatter_and_body(self):
        meta, body = skills.parse('---\nname: x\ndescription: "Quoted: yes"\n---\n\n# Body\ntext\n')
        self.assertEqual(meta, {"name": "x", "description": "Quoted: yes"})
        self.assertEqual(body, "# Body\ntext")

    def test_folded_description(self):
        meta, _ = skills.parse("---\ndescription: >\n  first line\n  second line\nname: x\n---\nb")
        self.assertEqual(meta, {"description": "first line second line", "name": "x"})

    def test_no_frontmatter(self):
        self.assertEqual(skills.parse("just a body\n"), ({}, "just a body"))

    def test_unterminated_frontmatter_is_body(self):
        self.assertEqual(skills.parse("---\nname: x\nbody")[0], {})


class SkillsTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.first, self.second = self.root / "first", self.root / "second"
        self.dirs = [self.first, self.second, self.root / "does-not-exist"]

    def test_discover_earlier_dir_wins(self):
        write_skill(self.first, "haiku", body="from first")
        write_skill(self.second, "haiku", body="from second")
        write_skill(self.second, "brief", description="Be brief")
        found = skills.discover(self.dirs)
        self.assertEqual(sorted(found), ["brief", "haiku"])
        self.assertEqual(found["haiku"].body, "from first")
        self.assertEqual(found["brief"].description, "Be brief")

    def test_resolve_by_name_only(self):
        path = write_skill(self.first, "haiku")
        self.assertEqual(skills.resolve("haiku", self.dirs).path, path)
        with self.assertRaisesRegex(skills.SkillError, "available: haiku"):
            skills.resolve("nope", self.dirs)
        # A path is never a skill, whatever it points at: only the libraries are read.
        loose = self.root / "loose.md"
        loose.write_text("no frontmatter here")
        for ref in (str(path), str(path.parent), str(loose), "../first/haiku", "./haiku", "~/x"):
            with self.assertRaises(skills.SkillError, msg=ref):
                skills.resolve(ref, self.dirs)
            with self.assertRaises(skills.SkillError, msg=ref):
                skills.normalize_ref(ref, self.dirs)
        self.assertEqual(skills.normalize_ref("haiku", self.dirs), "haiku")

    def test_label(self):
        self.assertEqual(skills.label("haiku"), "haiku")
        self.assertEqual(skills.label("/old/skills/haiku/SKILL.md"), "haiku")   # stored before
        self.assertEqual(skills.label("/old/notes/style.md"), "style")

    def test_compose_nothing_is_none(self):
        self.assertEqual(skills.compose_system(None, [], self.dirs), (None, [], []))
        self.assertEqual(skills.compose_system("  ", [], self.dirs)[0], None)

    def test_compose(self):
        write_skill(self.first, "haiku", body="Answer in haiku.")
        write_skill(self.first, "brief", body="Be brief.")
        prompt, active, missing = skills.compose_system("Base.", ["haiku", "gone", "brief"],
                                                        self.dirs)
        self.assertEqual(prompt, 'Base.\n\n<skill name="haiku">\nAnswer in haiku.\n</skill>\n\n'
                                 '<skill name="brief">\nBe brief.\n</skill>')
        self.assertEqual([s.name for s in active], ["haiku", "brief"])
        self.assertEqual(missing, ["gone"])
        self.assertNotIn("description", prompt)
        # A session from before may still name a skill by path: it is missing now, never read.
        secret = self.root / "secret.md"
        secret.write_text("private")
        prompt, _, missing = skills.compose_system(None, [str(secret)], self.dirs)
        self.assertEqual((prompt, missing), (None, [str(secret)]))

    def test_edits_apply_live_and_change_sha(self):
        path = write_skill(self.first, "haiku", body="v1")
        _, (before,), _ = skills.compose_system(None, ["haiku"], self.dirs)
        path.write_text("v2")
        prompt, (after,), _ = skills.compose_system(None, ["haiku"], self.dirs)
        self.assertIn("v2", prompt)
        self.assertNotEqual(before.sha, after.sha)


if __name__ == "__main__":
    unittest.main()
