"""Safety and coordination tests. Every test uses its own disposable board."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import subprocess
import struct
import sys
import tempfile
import unittest
import zlib

import board


class BoardTests(unittest.TestCase):
    def setUp(self):
        # Exercise the same mapped/shared filesystem as the real board, in isolation.
        test_root = Path(board.__file__).resolve().parent / "tmp"
        test_root.mkdir(exist_ok=True)
        self.folder = tempfile.TemporaryDirectory(prefix=".forum-test-", dir=test_root)
        self.assertTrue(Path(self.folder.name).resolve().is_relative_to(test_root.resolve()))
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / "board-data.js"
        self.photo = self.path.parent / "photo.png"
        self.other_photo = self.path.parent / "other-photo.png"
        self.photo.write_bytes(self.pixel_png(b"\xb9\x70\x38"))
        self.other_photo.write_bytes(self.pixel_png(b"\x45\x68\x90"))
        self.call("init")
        self.call("register", "--id", "coordinator", "--name", "Coordinator", "--role", "Coordinator", "--focus", "Coordination")

    @staticmethod
    def pixel_png(rgb):
        def chunk(kind, data):
            return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
        return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(b"\x00" + rgb)) + chunk(b"IEND", b"")

    def call(self, *args, good=True):
        if args[0] == "register" and "--avatar" not in args:
            photo = self.photo if args[args.index("--id") + 1] == "coordinator" else self.other_photo
            args = (*args, "--avatar", str(photo))
        result = subprocess.run([sys.executable, str(Path(board.__file__)), "--board", str(self.path), *args], capture_output=True, text=True, encoding="utf-8")
        if good:
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout)
        self.assertNotEqual(result.returncode, 0)
        return result

    def test_concurrent_posts_do_not_overwrite_each_other(self):
        def post(i):
            return self.call("post", "--agent", "coordinator", "--body", f"Concurrent post {i} 🏛️")
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(post, range(24)))
        data = board.read_board(self.path)
        self.assertEqual(len(data["posts"]), 24)
        self.assertEqual(len({p["id"] for p in data["posts"]}), 24)
        self.assertEqual(data["revision"], 26)
        self.assertEqual({r["revision"] for r in results}, set(range(3, 27)))

    def test_unicode_multiline_and_html_stay_exact_text(self):
        body = "Hello, @coordinator 👋\n\nالعربية works too.\n<script>alert('no')</script>"
        source = self.path.parent / "post.txt"
        source.write_text(body, encoding="utf-8")
        self.call("post", "--agent", "coordinator", "--body-file", str(source))
        self.assertEqual(board.read_board(self.path)["posts"][0]["body"], body)

    def test_failed_recipients_and_replies_preserve_board(self):
        original = self.path.read_bytes()
        self.call("post", "--agent", "coordinator", "--to", "missing", "--body", "No recipient", good=False)
        self.call("post", "--agent", "coordinator", "--reply-to", "missing", "--body", "No parent", good=False)
        self.assertEqual(self.path.read_bytes(), original)
        self.call("init", good=False)
        self.assertEqual(self.path.read_bytes(), original)

    def test_unique_profiles_and_idempotent_registration(self):
        self.call("register", "--id", "test_specialist", "--name", "Test Specialist", "--role", "Fixture", "--focus", "Isolated tests")
        data = board.read_board(self.path)
        a, b = data["agents"].values()
        self.assertNotEqual(a["color"], b["color"])
        self.assertNotEqual(a["avatar"], b["avatar"])
        self.assertNotEqual((self.path.parent / a["avatar"]).read_bytes(), (self.path.parent / b["avatar"]).read_bytes())
        result = self.call("register", "--id", "coordinator", "--name", "Coordinator", "--role", "Coordinator", "--focus", "Coordination")
        self.assertFalse(result["saved"])
        self.call("register", "--id", "other", "--name", "Coordinator", "--role", "Other", "--focus", "Other", good=False)
        self.call("register", "--id", "everyone", "--name", "Invalid", "--role", "Other", "--focus", "Other", good=False)
        self.assertEqual(board.read_board(self.path)["revision"], data["revision"])

    def test_reply_context_and_backups(self):
        first = self.call("post", "--agent", "coordinator", "--body", "Original post")["post_id"]
        previous = self.path.read_bytes()
        second = self.call("post", "--agent", "coordinator", "--reply-to", first, "--body", "Reply")["post_id"]
        context = self.call("show", "--post", second)
        self.assertEqual([p["id"] for p in context["posts"]], [first, second])
        self.assertEqual(self.path.with_name(".board-data.previous.js").read_bytes(), previous)
        self.assertTrue(self.call("validate")["valid"])

    def test_prompt_sync_and_late_chat_binding(self):
        prompt = self.path.parent / "AGENT_PROMPT.md"
        prompt.write_text("Meet the team 🏛️", encoding="utf-8")
        self.call("sync-prompt")
        self.assertEqual(board.read_board(self.path)["meta"]["provisioning_prompt"], "Meet the team 🏛️")
        self.call("bind-chat", "--agent", "coordinator", "--thread-id", "verified-fixture-chat")
        self.call("bind-chat", "--agent", "coordinator", "--thread-id", "different-chat", good=False)
        self.assertEqual(board.read_board(self.path)["agents"]["coordinator"]["thread_id"], "verified-fixture-chat")

    def test_photo_change_preserves_identity_color_and_history(self):
        self.call("post", "--agent", "coordinator", "--body", "Existing history")
        original = board.read_board(self.path)
        self.call("picture", "--agent", "coordinator", "--image", str(self.other_photo))
        updated = board.read_board(self.path)
        self.assertEqual(original["posts"], updated["posts"])
        for field in ("id", "name", "role", "focus", "color", "tint", "joined_at"):
            self.assertEqual(original["agents"]["coordinator"][field], updated["agents"]["coordinator"][field])
        self.assertNotEqual(original["agents"]["coordinator"]["avatar"], updated["agents"]["coordinator"]["avatar"])
        self.call("register", "--id", "duplicate_photo", "--name", "Duplicate photo fixture", "--role", "Fixture", "--focus", "Tests", good=False)

    def test_profile_update_preserves_identity_history_and_rejects_bad_updates(self):
        self.call("register", "--id", "test_specialist", "--name", "Test Specialist", "--role", "Fixture", "--focus", "Tests")
        self.call("post", "--agent", "test_specialist", "--body", "Keep this history")
        before = board.read_board(self.path)
        self.call("profile", "--agent", "test_specialist", "--name", "Updated Specialist", "--role", "Reviewer")
        after = board.read_board(self.path)
        self.assertEqual(after["posts"], before["posts"])
        for key in ("id", "avatar", "color", "tint", "thread_id", "joined_at", "focus"):
            self.assertEqual(before["agents"]["test_specialist"][key], after["agents"]["test_specialist"][key])
        self.assertEqual(after["agents"]["test_specialist"]["name"], "Updated Specialist")
        self.assertEqual(after["agents"]["test_specialist"]["role"], "Reviewer")
        original = self.path.read_bytes()
        for args in (("--name", "Coordinator"), ("--role", " "), ()):
            self.call("profile", "--agent", "test_specialist", *args, good=False)
        self.assertEqual(self.path.read_bytes(), original)

    def test_short_and_full_names_are_independent_and_keep_history(self):
        self.call("register", "--id", "test_specialist", "--name", "River", "--full-name", "River Quinn", "--role", "Reviewer", "--focus", "Tests")
        self.call("post", "--agent", "test_specialist", "--body", "An existing post")
        before = board.read_board(self.path)
        self.assertEqual(before["agents"]["test_specialist"]["name"], "River")
        self.assertEqual(before["agents"]["test_specialist"]["full_name"], "River Quinn")
        self.call("profile", "--agent", "test_specialist", "--full-name", "River Q. Quinn")
        after = board.read_board(self.path)
        self.assertEqual(after["posts"], before["posts"])
        for field in ("id", "name", "role", "avatar", "color", "tint", "thread_id", "joined_at"):
            self.assertEqual(before["agents"]["test_specialist"][field], after["agents"]["test_specialist"][field])
        self.assertEqual(after["agents"]["test_specialist"]["full_name"], "River Q. Quinn")
        original = self.path.read_bytes()
        self.call("profile", "--agent", "test_specialist", "--full-name", " ", good=False)
        self.assertEqual(self.path.read_bytes(), original)

    def test_legacy_profiles_without_full_names_remain_readable(self):
        data = board.read_board(self.path)
        del data["agents"]["coordinator"]["full_name"]
        board.save_board(self.path, data)
        self.assertNotIn("full_name", board.read_board(self.path)["agents"]["coordinator"])
        self.assertTrue(self.call("validate")["valid"])

    def test_meme_images_are_copied_and_survive_source_removal(self):
        self.call("post", "--agent", "coordinator", "--body", "Photo meme 😼", "--image", str(self.photo), "--alt", "A fixture cat photo", "--image", str(self.other_photo), "--alt", "A fixture dog photo")
        images = board.read_board(self.path)["posts"][0]["images"]
        self.assertEqual(len(images), 2)
        self.assertEqual(images[0]["alt"], "A fixture cat photo")
        self.photo.unlink()
        self.other_photo.unlink()
        self.assertTrue(all((self.path.parent / image["src"]).is_file() for image in images))
        self.assertEqual(self.call("validate")["images"], 2)

    def test_bad_attachments_and_missing_descriptions_do_not_change_board(self):
        original = self.path.read_bytes()
        fake = self.path.parent / "fake.png"
        fake.write_text("<script>Not a PNG</script>", encoding="utf-8")
        self.call("post", "--agent", "coordinator", "--body", "Bad image", "--image", str(fake), "--alt", "Spoofed image", good=False)
        self.call("post", "--agent", "coordinator", "--body", "Missing description", "--image", str(self.photo), good=False)
        self.assertEqual(self.path.read_bytes(), original)
        for value in ("../photo.png", "https://example.com/cat.jpg", "javascript:alert(1)"):
            with self.assertRaises(ValueError):
                board.local_image_path(value)

    def test_requested_image_replacement_only_changes_the_selected_attachment(self):
        self.call("register", "--id", "test_specialist", "--name", "Test Specialist", "--role", "Fixture", "--focus", "Tests")
        post_id = self.call("post", "--agent", "coordinator", "--body", "Portrait reveal", "--image", str(self.photo), "--alt", "Old portrait", "--image", str(self.other_photo), "--alt", "Second attachment")["post_id"]
        self.call("post", "--agent", "test_specialist", "--reply-to", post_id, "--body", "A reply")
        before = board.read_board(self.path)
        original = self.path.read_bytes()
        for agent, index in (("test_specialist", "0"), ("coordinator", "-1"), ("coordinator", "2")):
            self.call("replace-image", "--agent", agent, "--post", post_id, "--index", index, "--image", str(self.other_photo), "--alt", "New portrait", good=False)
        self.assertEqual(self.path.read_bytes(), original)
        self.call("replace-image", "--agent", "coordinator", "--post", post_id, "--image", str(self.other_photo), "--alt", "New portrait")
        after = board.read_board(self.path)
        expected_posts = json.loads(json.dumps(before["posts"]))
        expected_posts[0]["images"][0] = {"src": before["posts"][0]["images"][1]["src"], "alt": "New portrait"}
        self.assertEqual(after["posts"], expected_posts)
        self.assertEqual(after["agents"], before["agents"])
        self.assertEqual(self.path.with_name(".board-data.previous.js").read_bytes(), original)

    def test_new_profiles_require_raster_pictures(self):
        source = self.path.parent / "icon.svg"
        source.write_text('<svg xmlns="http://www.w3.org/2000/svg"/>', encoding="utf-8")
        self.call("register", "--id", "svg_fixture", "--name", "SVG fixture", "--role", "Fixture", "--focus", "Tests", "--avatar", str(source), good=False)
        self.assertEqual(len(board.read_board(self.path)["agents"]), 1)

    def test_mentions_detect_names_and_replies_without_self_or_substring_noise(self):
        self.call("register", "--id", "test_specialist", "--name", "River", "--full-name", "River Quinn", "--role", "Reviewer", "--focus", "Tests", "--thread-id", "fixture-chat")
        post = self.call("post", "--agent", "coordinator", "--body", "Riverton, `River`, and https://example.com/River are not mentions. Coordinator is me.")
        self.assertEqual(post["mentions"], [])
        post = self.call("post", "--agent", "coordinator", "--body", "@test_specialist, RIVER, River Quinn: one notification please.")
        self.assertEqual([row["agent_id"] for row in post["mentions"]], ["test_specialist"])
        self.assertEqual(post["mentions"][0]["thread_id"], "fixture-chat")
        reply = self.call("post", "--agent", "test_specialist", "--reply-to", post["post_id"], "--body", "Here is my reply.")
        self.assertEqual([row["agent_id"] for row in reply["mentions"]], ["coordinator"])
        entry = next(row for row in self.call("mentions", "--all")["mentions"] if row["post_id"] == post["post_id"])
        self.assertEqual(entry["state"], "replied")

    def add_lounge_colleagues(self):
        self.call("register", "--id", "test_specialist", "--name", "River", "--role", "Reviewer", "--focus", "Tests")
        third_photo = self.path.parent / "third-photo.png"
        third_photo.write_bytes(self.pixel_png(b"\x87\x35\x61"))
        self.call("register", "--id", "test_designer", "--name", "Pixel", "--role", "Designer", "--focus", "Tests", "--avatar", str(third_photo))

    def test_new_social_topics_invite_everyone_but_replies_and_work_stay_selective(self):
        self.add_lounge_colleagues()
        update = self.call("post", "--agent", "coordinator", "--body", "A routine work update.")
        self.assertEqual(update["mentions"], [])
        topic = self.call("post", "--agent", "coordinator", "--kind", "social", "--to", "everyone", "--to", "test_specialist", "--body", "What makes a useful team note?")
        self.assertEqual([m["agent_id"] for m in topic["mentions"]], ["test_designer", "test_specialist"])
        self.assertEqual(board.read_board(self.path)["posts"][-1]["to"], ["everyone", "test_specialist"])
        reply = self.call("post", "--agent", "test_specialist", "--kind", "social", "--reply-to", topic["post_id"], "--body", "A short question with enough context.")
        self.assertEqual([m["agent_id"] for m in reply["mentions"]], ["coordinator"])
        addressed = self.call("post", "--agent", "coordinator", "--kind", "social", "--to", "test_specialist", "--body", "An aside for one colleague.")
        self.assertEqual([m["agent_id"] for m in addressed["mentions"]], ["test_specialist"])

    def test_social_broadcast_does_not_retroactively_invite_the_old_wall(self):
        self.add_lounge_colleagues()
        old_post = self.call("post", "--agent", "coordinator", "--kind", "social", "--body", "An old introduction.")["post_id"]
        legacy = board.read_board(self.path)
        del legacy["mentions"]
        board.save_board(self.path, legacy)
        self.assertEqual(self.call("mention-backfill")["queued"], 0)
        new_post = self.call("post", "--agent", "coordinator", "--kind", "social", "--body", "A new topic.")["post_id"]
        self.call("mention-backfill")
        entries = self.call("mentions", "--all")["mentions"]
        self.assertEqual(len(entries), 2)
        self.assertEqual({m["post_id"] for m in entries}, {new_post})
        self.assertNotIn(old_post, {m["post_id"] for m in entries})

    def test_mention_claim_is_atomic_and_dispatch_needs_the_matching_receipt(self):
        self.call("register", "--id", "test_specialist", "--name", "River", "--role", "Reviewer", "--focus", "Tests", "--thread-id", "fixture-chat")
        post_id = self.call("post", "--agent", "coordinator", "--body", "River, your thoughts?")["post_id"]
        def claim(_):
            return self.call("mention", "--post", post_id, "--to", "test_specialist", "--by", "coordinator", "--state", "claimed", "--note", "Direct question")
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(claim, range(2)))
        self.assertEqual(sum(row["claimed"] for row in results), 1)
        original = self.path.read_bytes()
        self.call("mention", "--post", post_id, "--to", "test_specialist", "--by", "coordinator", "--state", "sent", "--note", "Tool returned success", "--receipt", "wrong-chat", good=False)
        self.assertEqual(self.path.read_bytes(), original)
        self.call("post", "--agent", "test_specialist", "--reply-to", post_id, "--body", "Fast reply")
        result = self.call("mention", "--post", post_id, "--to", "test_specialist", "--by", "coordinator", "--state", "sent", "--note", "Tool returned success", "--receipt", "fixture-chat")
        self.assertEqual(result["mention"]["state"], "replied")
        self.assertFalse(claim(0)["claimed"])

    def test_mention_backfill_is_idempotent_and_respects_existing_replies_and_skips(self):
        self.call("register", "--id", "test_specialist", "--name", "River", "--role", "Reviewer", "--focus", "Tests")
        first = self.call("post", "--agent", "coordinator", "--body", "River, welcome.")["post_id"]
        self.call("post", "--agent", "test_specialist", "--reply-to", first, "--body", "Thank you")
        old = board.read_board(self.path)
        del old["mentions"]
        board.save_board(self.path, old)
        self.call("mention-backfill")
        original = board.read_board(self.path)
        entry = next(row for row in original["mentions"] if row["post_id"] == first)
        self.assertEqual(entry["state"], "replied")
        pending = self.call("mentions")["mentions"][0]
        self.call("mention", "--post", pending["post_id"], "--to", pending["agent_id"], "--by", "coordinator", "--state", "skipped", "--note", "A closed thank-you needs no response")
        self.call("mention-backfill")
        after = board.read_board(self.path)
        self.assertEqual(len(after["mentions"]), len(original["mentions"]))
        self.assertEqual(self.call("mentions")["mentions"], [])

    def test_image_only_posts_are_supported_but_empty_posts_are_rejected(self):
        self.call("post", "--agent", "coordinator", "--kind", "social", "--image", str(self.photo), "--alt", "A photo meme without a caption")
        post = board.read_board(self.path)["posts"][0]
        self.assertEqual(post["body"], "")
        self.assertEqual(len(post["images"]), 1)
        original = self.path.read_bytes()
        self.call("post", "--agent", "coordinator", good=False)
        self.assertEqual(self.path.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
