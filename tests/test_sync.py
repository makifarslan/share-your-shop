"""Tests for sync.py against a fake Discord. Run: python -m unittest discover -s tests"""

import io
import json
import os
import shutil
import sys
import tempfile
import unittest
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import sync  # noqa: E402

CHECK = "✅"
GUILD, CHANNEL = "1", "2"
OWNER, MOD, FAN, ALICE, BOB, SPAM, BOT, GONE = "100", "101", "102", "103", "104", "105", "106", "107"
MOD_ROLE = "900"
PAGES = "https://pages.test/share-your-shop"


def user(uid, name, bot=False):
    return {"id": uid, "username": name, "global_name": name.title(), "bot": bot}


def picture(aid, w=1920, h=1080, name="shop.png", kind="image/png", flags=0):
    return {"id": aid, "filename": name, "content_type": kind, "width": w, "height": h,
            "size": 500_000, "url": f"https://cdn.test/{aid}", "flags": flags}


def message(mid, author, attachments=(), reactors=(), emoji=CHECK):
    m = {"id": mid, "author": author, "timestamp": f"2026-11-{int(mid) % 28 + 1:02d}T12:00:00+00:00",
         "attachments": list(attachments), "reactions": []}
    if reactors:
        m["reactions"].append({"count": len(reactors), "emoji": {"id": None, "name": emoji}})
    m["_reactors"] = list(reactors)
    return m


class FakeWeb:
    """Answers the Discord API, the CDN and the Pages site the way the real ones do."""

    def __init__(self, messages, channel_type=0):
        self.messages = messages
        self.channel_type = channel_type
        self.members = {
            MOD: {"roles": [MOD_ROLE]}, FAN: {"roles": []}, ALICE: {"roles": []},
            BOB: {"roles": []}, OWNER: {"roles": []},
        }
        self.pages = {}
        self.calls = []
        self.fail_first = 0

    def __call__(self, url, headers, timeout):
        self.calls.append(url)
        parsed = urllib.parse.urlparse(url)
        query = dict(urllib.parse.parse_qsl(parsed.query))

        if url.startswith("https://cdn.test/"):
            return 200, {}, b"SOURCE:" + parsed.path.rsplit("/", 1)[1].encode()

        if url.startswith(PAGES):
            path = parsed.path[len("/share-your-shop/"):]
            if path in self.pages:
                return 200, {}, self.pages[path]
            return 404, {}, b"not found"

        assert headers["Authorization"] == "Bot TOKEN"
        path = parsed.path[len("/api/v10"):]

        if self.fail_first:
            self.fail_first -= 1
            return 429, {}, json.dumps({"retry_after": 0.5}).encode()

        if path == "/users/@me/guilds":
            return self.ok([{"id": GUILD}])
        if path == f"/guilds/{GUILD}/channels":
            # Servers decorate names: an emoji and a dot in front.
            decorated = chr(0x1F3A8) + chr(0x30FB) + "share-your-shop"
            return self.ok([{"id": "3", "name": "general", "type": 0},
                            {"id": "4", "name": "share-your-shop-rules", "type": 0},
                            {"id": CHANNEL, "name": decorated, "type": self.channel_type}])
        if path == f"/channels/{CHANNEL}":
            return self.ok({"id": CHANNEL, "guild_id": GUILD, "name": "share-your-shop", "type": self.channel_type})
        if path == f"/guilds/{GUILD}":
            return self.ok({"owner_id": OWNER, "roles": [
                {"id": GUILD, "permissions": str(1 << 10)},
                {"id": MOD_ROLE, "permissions": str(sync.MANAGE_MESSAGES)}]})
        if path.startswith(f"/guilds/{GUILD}/members/"):
            uid = path.rsplit("/", 1)[1]
            return self.ok(self.members[uid]) if uid in self.members else (404, {}, b"{}")
        if path == f"/channels/{CHANNEL}/messages":
            newest = sorted(self.messages, key=lambda m: int(m["id"]), reverse=True)
            if "before" in query:
                newest = [m for m in newest if int(m["id"]) < int(query["before"])]
            return self.ok([self.public(m) for m in newest[: int(query.get("limit", 50))]])
        if path == f"/guilds/{GUILD}/threads/active":
            return self.ok({"threads": [{"id": m["id"], "parent_id": CHANNEL} for m in self.messages[:2]]})
        if path == f"/channels/{CHANNEL}/threads/archived/public":
            return self.ok({"threads": [{"id": m["id"], "parent_id": CHANNEL,
                                         "thread_metadata": {"archive_timestamp": "2026-01-01T00:00:00Z"}}
                                        for m in self.messages[2:]], "has_more": False})
        if "/reactions/" in path:
            mid = path.split("/messages/")[1].split("/")[0]
            m = next(m for m in self.messages if m["id"] == mid)
            return self.ok([{"id": uid, "bot": uid == BOT} for uid in m["_reactors"]])
        if "/messages/" in path:  # a forum post's first message
            thread_id, mid = path.split("/")[2], path.rsplit("/", 1)[1]
            assert thread_id == mid
            m = next((m for m in self.messages if m["id"] == mid), None)
            return self.ok(self.public(m)) if m else (404, {}, b"{}")

        raise AssertionError("unexpected call " + url)

    @staticmethod
    def ok(data):
        return 200, {"x-ratelimit-remaining": "5"}, json.dumps(data).encode()

    @staticmethod
    def public(m):
        return {k: v for k, v in m.items() if not k.startswith("_")}


def fake_picture(data, cfg):
    return sync.JPEG_MAGIC + b" made from " + data


def sample_messages():
    alice, bob = user(ALICE, "alice"), user(BOB, "bob")
    return [
        # Two pictures, approved by a moderator.
        message("1000", alice, [picture("5001"), picture("5002", 2560, 1440, "a.jpg", "image/jpeg")], [FAN, MOD]),
        # Only a player reacted: waits.
        message("999", bob, [picture("5003")], [FAN]),
        # Alice again, approved by the owner: one more fits her limit of 3.
        message("998", alice, [picture("5004"), picture("5005")], [OWNER]),
        # Words only.
        message("997", bob),
        # Bots never count.
        message("996", user(BOT, "helper", bot=True), [picture("5006")], [OWNER]),
        # Too small and a spoiler are left out; the third one is fine. The emoji has the VS16 mark.
        message("995", bob, [picture("5007", 320, 240), picture("5008", name="SPOILER_x.png"), picture("5009")],
                [MOD], emoji=CHECK + "️"),
        # A blocked player.
        message("994", user(SPAM, "spam"), [picture("5010")], [OWNER]),
        # The first reactor left the server; the second is a moderator.
        message("993", bob, [picture("5011")], [GONE, MOD]),
        # Not a picture.
        message("992", bob, [picture("5012", name="logo.svg", kind="image/svg+xml")], [OWNER]),
        # A bot's check mark is not an approval.
        message("991", bob, [picture("5013")], [BOT]),
    ]


def config(**changes):
    cfg = sync.Config()
    cfg.blocked_user_ids = {SPAM}
    cfg.approver_role_ids, cfg.approver_user_ids, cfg.blocked_message_ids = set(), set(), set()
    for k, v in changes.items():
        setattr(cfg, k, v)
    return cfg


class ChooseTests(unittest.TestCase):
    def run_choose(self, web, cfg):
        dc = sync.Discord("TOKEN", web, sleep=lambda s: None)
        channel, guild = sync.find_channel(dc, cfg)
        approvers = sync.Approvers(dc, guild, cfg)
        return sync.choose_items(sync.read_posts(dc, channel, guild, cfg), dc, approvers, cfg)

    def test_only_approved_pictures_newest_first(self):
        items, stats = self.run_choose(FakeWeb(sample_messages()), config())
        self.assertEqual([i.attachment_id for i in items], ["5001", "5002", "5004", "5009", "5011"])
        self.assertEqual([i.author for i in items], ["alice", "alice", "alice", "bob", "bob"])
        self.assertEqual(stats["approved posts"], 4)
        self.assertEqual(stats["posts waiting for approval"], 2)  # 999 (player) and 991 (bot)
        self.assertEqual(stats["blocked"], 1)
        self.assertEqual(stats["pictures over the per-player limit"], 1)
        self.assertEqual(stats["pictures left out (spoiler, too small or too big)"], 2)

    def test_limits(self):
        items, _ = self.run_choose(FakeWeb(sample_messages()), config(max_per_author=1, max_images=10))
        self.assertEqual([i.attachment_id for i in items], ["5001", "5009"])
        items, _ = self.run_choose(FakeWeb(sample_messages()), config(max_images=2))
        self.assertEqual([i.attachment_id for i in items], ["5001", "5002"])

    def test_paging_reads_past_the_first_page(self):
        cfg = config(max_messages=3000)
        msgs = [message(str(2000 + n), user(ALICE, "alice")) for n in range(250)]
        msgs.append(message("10", user(BOB, "bob"), [picture("6000")], [MOD]))
        items, stats = self.run_choose(FakeWeb(msgs), cfg)
        self.assertEqual([i.attachment_id for i in items], ["6000"])
        self.assertEqual(stats["posts read"], 251)

    def test_max_messages_stops_reading(self):
        msgs = [message(str(2000 + n), user(ALICE, "alice")) for n in range(150)]
        msgs.append(message("10", user(BOB, "bob"), [picture("6000")], [MOD]))
        items, stats = self.run_choose(FakeWeb(msgs), config(max_messages=120))
        self.assertEqual(items, [])
        self.assertEqual(stats["posts read"], 120)

    def test_forum_channel(self):
        web = FakeWeb(sample_messages(), channel_type=15)
        items, _ = self.run_choose(web, config())
        self.assertEqual([i.attachment_id for i in items], ["5001", "5002", "5004", "5009", "5011"])

    def test_channel_by_id(self):
        web = FakeWeb(sample_messages())
        items, _ = self.run_choose(web, config(channel_id=CHANNEL, channel_name="anything"))
        self.assertEqual(len(items), 5)
        self.assertFalse([u for u in web.calls if u.endswith("/users/@me/guilds")])

    def test_role_listed_in_config_can_approve(self):
        web = FakeWeb([message("50", user(BOB, "bob"), [picture("7000")], [FAN])])
        web.members[FAN] = {"roles": ["777"]}
        items, _ = self.run_choose(web, config())
        self.assertEqual(items, [])
        items, _ = self.run_choose(web, config(approver_role_ids={"777"}))
        self.assertEqual([i.attachment_id for i in items], ["7000"])

    def test_rate_limit_is_waited_out(self):
        web = FakeWeb(sample_messages())
        web.fail_first = 2
        waits = []
        dc = sync.Discord("TOKEN", web, sleep=waits.append)
        self.assertEqual(dc.get("/users/@me/guilds"), [{"id": GUILD}])
        self.assertEqual(waits, [0.75, 0.75])

    def test_bad_token_is_a_clear_error(self):
        dc = sync.Discord("TOKEN", lambda u, h, t: (401, {}, b'{"message": "401: Unauthorized"}'), sleep=lambda s: None)
        with self.assertRaises(sync.Forbidden):
            dc.get("/users/@me/guilds")


class SiteTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.out = os.path.join(self.dir, "site")
        self.cfg_path = os.path.join(self.dir, "config.json")
        with open(self.cfg_path, "w", encoding="utf-8") as f:
            json.dump({"blocked_user_ids": [SPAM], "approve_emoji": [CHECK]}, f)
        self.output = os.path.join(self.dir, "out.txt")
        self.env = dict(os.environ)
        os.environ.update({"DISCORD_BOT_TOKEN": "TOKEN", "GITHUB_OUTPUT": self.output, "FORCE": ""})

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.env)
        shutil.rmtree(self.dir, ignore_errors=True)

    def run_main(self, web, *extra, maker=fake_picture):
        open(self.output, "w").close()
        code = sync.main(["--config", self.cfg_path, "--out", self.out, "--pages-url", PAGES, *extra], web, maker)
        with open(self.output) as f:
            return code, f.read().strip()

    def publish(self, web):
        """What the deploy step does: the built site goes live."""
        for root, _, files in os.walk(self.out):
            for name in files:
                rel = os.path.relpath(os.path.join(root, name), self.out).replace(os.sep, "/")
                with open(os.path.join(root, name), "rb") as f:
                    web.pages[rel] = f.read()
        shutil.rmtree(self.out)

    def test_build_then_no_change_then_change(self):
        web = FakeWeb(sample_messages())
        code, out = self.run_main(web)
        self.assertEqual((code, out), (0, "changed=true"))

        with open(os.path.join(self.out, "manifest.json"), encoding="utf-8") as f:
            manifest = json.load(f)
        self.assertEqual(manifest["version"], 1)
        self.assertTrue(manifest["enabled"])
        self.assertEqual(manifest["rotate_seconds"], 8)
        self.assertEqual([e["author"] for e in manifest["items"]], ["alice", "alice", "alice", "bob", "bob"])
        tag = sync.Config().picture_tag()
        self.assertEqual(manifest["items"][0]["image"], f"img/5001_{tag}.jpg")
        for entry in manifest["items"]:
            with open(os.path.join(self.out, entry["image"]), "rb") as f:
                self.assertTrue(f.read().startswith(sync.JPEG_MAGIC))
        self.assertIn("@alice", open(os.path.join(self.out, "index.html"), encoding="utf-8").read())

        # Live now. The same channel next hour: nothing to deploy.
        self.publish(web)
        code, out = self.run_main(web)
        self.assertEqual((code, out), (0, "changed=false"))
        self.assertFalse(os.path.exists(self.out))

        # A new approval: rebuilt, and the pictures already live come from the site, not Discord.
        web.messages.append(message("2000", user(BOB, "bob"), [picture("8000")], [MOD]))
        web.calls.clear()
        code, out = self.run_main(web)
        self.assertEqual(out, "changed=true")
        cdn = [u for u in web.calls if u.startswith("https://cdn.test/")]
        self.assertEqual(cdn, ["https://cdn.test/8000"])

    def test_turned_off_lists_nothing(self):
        with open(self.cfg_path, "w", encoding="utf-8") as f:
            json.dump({"enabled": False}, f)
        web = FakeWeb(sample_messages())
        code, out = self.run_main(web)
        self.assertEqual(out, "changed=true")
        with open(os.path.join(self.out, "manifest.json"), encoding="utf-8") as f:
            manifest = json.load(f)
        self.assertFalse(manifest["enabled"])
        self.assertEqual(manifest["items"], [])
        self.assertFalse([u for u in web.calls if "discord" in u])

    def test_no_token_does_nothing_unless_forced(self):
        os.environ["DISCORD_BOT_TOKEN"] = ""
        web = FakeWeb(sample_messages())
        self.assertEqual(self.run_main(web), (0, "changed=false"))
        self.assertEqual(self.run_main(web, "--force"), (0, "changed=true"))

    def test_a_broken_picture_is_left_out(self):
        def maker(data, cfg):
            if data.endswith(b"5002"):
                raise ValueError("cannot read")
            return fake_picture(data, cfg)

        self.run_main(FakeWeb(sample_messages()), maker=maker)
        with open(os.path.join(self.out, "manifest.json"), encoding="utf-8") as f:
            ids = [e["id"] for e in json.load(f)["items"]]
        self.assertEqual(ids, ["5001", "5004", "5009", "5011"])

    def test_unknown_setting_is_refused(self):
        with open(self.cfg_path, "w", encoding="utf-8") as f:
            json.dump({"max_image": 3}, f)
        with self.assertRaises(SystemExit):
            sync.Config.load(self.cfg_path)


try:
    from PIL import Image
except ImportError:
    Image = None


@unittest.skipIf(Image is None, "Pillow is not installed")
class PictureTests(unittest.TestCase):
    def encode(self, image, fmt="JPEG", **kw):
        buf = io.BytesIO()
        image.save(buf, fmt, **kw)
        return buf.getvalue()

    def check(self, data):
        out = Image.open(io.BytesIO(data))
        self.assertEqual(out.format, "JPEG")
        self.assertEqual(out.size, (1280, 720))
        self.assertEqual(len(out.getexif()), 0)
        self.assertNotIn("icc_profile", out.info)
        return out

    def test_turned_phone_photo_loses_its_metadata(self):
        exif = Image.Exif()
        exif[0x0112] = 6        # stored on its side
        exif[0x010F] = "Phone"  # camera maker
        source = Image.new("RGB", (900, 1600), (200, 120, 60))
        self.check(sync.make_picture(self.encode(source, exif=exif.tobytes()), sync.Config()))

    def test_upright_picture_sits_over_a_blurred_copy(self):
        source = Image.new("RGB", (900, 1600), (250, 250, 250))
        out = self.check(sync.make_picture(self.encode(source), sync.Config())).convert("RGB")
        self.assertGreater(out.getpixel((640, 360))[0], 230)  # the picture itself
        self.assertLess(out.getpixel((40, 360))[0], 140)      # darkened sides

    def test_transparent_png(self):
        source = Image.new("RGBA", (1920, 1080), (255, 0, 0, 0))
        out = self.check(sync.make_picture(self.encode(source, "PNG"), sync.Config())).convert("RGB")
        self.assertLess(max(out.getpixel((640, 360))), 60)

    def test_too_small(self):
        with self.assertRaises(ValueError):
            sync.make_picture(self.encode(Image.new("RGB", (320, 240))), sync.Config())


if __name__ == "__main__":
    unittest.main()
