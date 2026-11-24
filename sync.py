#!/usr/bin/env python3
"""
Builds the "share your shop" picture list that the game's main menu shows.

Every run:
  1. Reads the Discord channel where players post their shops (with a read-only bot).
  2. Keeps only the posts a moderator approved with the approve emoji (a check mark by
     default). A moderator is the server owner, anyone with Administrator or Manage
     Messages, or anyone listed in config.json.
  3. Writes a small static site:
         site/manifest.json         the list the game reads
         site/img/<id>_<tag>.jpg    the pictures: 1280x720 JPEG, photo metadata removed
         site/index.html            a plain page to check what is live
  4. Tells the workflow whether anything changed, so a quiet hour deploys nothing.

Needs the DISCORD_BOT_TOKEN environment variable (a GitHub secret). Only the standard
library is used, plus Pillow for the pictures.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import io
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from dataclasses import dataclass, field, fields
from datetime import datetime, timezone
from typing import Callable, Iterable, Iterator

API = "https://discord.com/api/v10"
USER_AGENT = "DiscordBot (https://github.com, 1.0)"

# Bump when make_picture changes. It is part of every picture's file name, so the games
# download the new pictures instead of keeping their old copies.
PROCESS_VERSION = 1

# The game only reads a manifest with this version.
MANIFEST_VERSION = 1

ADMINISTRATOR = 1 << 3
MANAGE_MESSAGES = 1 << 13

TEXT_CHANNEL_TYPES = (0, 5)      # text, announcement
THREAD_CHANNEL_TYPES = (15, 16)  # forum, media

PICTURE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp")
ATTACHMENT_SPOILER = 1 << 3
MAX_SOURCE_BYTES = 25 * 1024 * 1024
JPEG_MAGIC = b"\xff\xd8\xff"


class NotFound(Exception):
    pass


class Forbidden(Exception):
    pass


# ---------------------------------------------------------------------------- settings

@dataclass
class Config:
    channel_name: str = "share-your-shop"
    channel_id: str = ""
    guild_id: str = ""
    approve_emoji: list = field(default_factory=lambda: ["✅"])
    approver_role_ids: list = field(default_factory=list)
    approver_user_ids: list = field(default_factory=list)
    blocked_user_ids: list = field(default_factory=list)
    blocked_message_ids: list = field(default_factory=list)
    max_messages: int = 3000
    max_images: int = 40
    max_per_author: int = 3
    min_width: int = 640
    min_height: int = 360
    image_width: int = 1280
    image_height: int = 720
    jpeg_quality: int = 82
    rotate_seconds: float = 8
    enabled: bool = True

    @staticmethod
    def load(path: str) -> "Config":
        with open(path, encoding="utf-8") as f:
            data = json.load(f)

        known = {f.name for f in fields(Config)}
        # Keys starting with "_" are notes for people.
        unknown = sorted(k for k in data if k not in known and not k.startswith("_"))
        if unknown:
            raise SystemExit(f"config.json: unknown setting(s): {', '.join(unknown)}")

        cfg = Config(**{k: v for k, v in data.items() if k in known})
        if isinstance(cfg.approve_emoji, str):
            cfg.approve_emoji = [cfg.approve_emoji]
        for name in ("approver_role_ids", "approver_user_ids", "blocked_user_ids", "blocked_message_ids"):
            setattr(cfg, name, {str(v).strip() for v in getattr(cfg, name) if str(v).strip()})
        cfg.channel_id = str(cfg.channel_id or "").strip()
        cfg.guild_id = str(cfg.guild_id or "").strip()
        return cfg

    def picture_tag(self) -> str:
        """Short tag in every picture's name: changes when the pictures would come out different."""
        key = f"{PROCESS_VERSION}|{self.image_width}x{self.image_height}|{self.jpeg_quality}"
        return "p" + hashlib.sha256(key.encode()).hexdigest()[:6]


# ---------------------------------------------------------------------------- web

Transport = Callable[[str, dict, float], tuple]


def http_get(url: str, headers: dict, timeout: float) -> tuple:
    """GET a URL. Returns (status, headers, body). HTTP errors come back as a status,
    network errors raise OSError."""
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, {k.lower(): v for k, v in response.headers.items()}, response.read()
    except urllib.error.HTTPError as error:
        return error.code, {k.lower(): v for k, v in (error.headers or {}).items()}, error.read() or b""


def short(body: bytes) -> str:
    return body[:200].decode("utf-8", "replace").replace("\n", " ")


class Discord:
    """The few read-only Discord calls this needs, with rate limits and retries."""

    def __init__(self, token: str, transport: Transport = http_get, sleep=time.sleep):
        self.headers = {"Authorization": f"Bot {token}", "User-Agent": USER_AGENT}
        self.transport = transport
        self.sleep = sleep
        self.calls = 0

    def get(self, path: str, params: dict | None = None):
        url = API + path + ("?" + urllib.parse.urlencode(params) if params else "")
        delay = 1.0

        for _ in range(8):
            try:
                status, headers, body = self.transport(url, self.headers, 30)
            except OSError:
                self.sleep(delay)
                delay = min(delay * 2, 30)
                continue

            self.calls += 1

            if status == 429:
                self.sleep(retry_after(headers, body) + 0.25)
                continue
            if status >= 500:
                self.sleep(delay)
                delay = min(delay * 2, 30)
                continue
            if status == 404:
                raise NotFound(path)
            if status in (401, 403):
                raise Forbidden(f"Discord said {status} for {path}: {short(body)}")
            if status >= 400:
                raise RuntimeError(f"Discord said {status} for {path}: {short(body)}")

            # The bucket is empty: wait until it refills, before the next call.
            if headers.get("x-ratelimit-remaining") == "0":
                self.sleep(float(headers.get("x-ratelimit-reset-after", "1")) + 0.05)

            return json.loads(body.decode("utf-8")) if body else None

        raise RuntimeError(f"Discord did not answer {path}")


def retry_after(headers: dict, body: bytes) -> float:
    try:
        return float(json.loads(body.decode("utf-8")).get("retry_after", 1))
    except (ValueError, AttributeError):
        return float(headers.get("retry-after", "1"))


# ---------------------------------------------------------------------------- reading the channel

@dataclass
class Post:
    message_id: str
    channel_id: str  # where its reactions live (the thread, for a forum post)
    author_id: str
    author_name: str
    author_is_bot: bool
    posted: str
    attachments: list
    reactions: list


def post_from_message(message: dict, channel_id: str) -> Post:
    author = message.get("author") or {}
    return Post(
        message_id=str(message.get("id", "")),
        channel_id=channel_id,
        author_id=str(author.get("id", "")),
        author_name=str(author.get("username") or ""),
        author_is_bot=bool(author.get("bot")) or bool(message.get("webhook_id")),
        posted=str(message.get("timestamp") or ""),
        attachments=list(message.get("attachments") or []),
        reactions=list(message.get("reactions") or []),
    )


def find_channel(dc: Discord, cfg: Config) -> tuple:
    """The channel to read and its server, by id or else by name."""
    if cfg.channel_id:
        channel = dc.get(f"/channels/{cfg.channel_id}")
        return channel, str(channel.get("guild_id") or cfg.guild_id)

    guild_ids = [cfg.guild_id] if cfg.guild_id else [str(g["id"]) for g in dc.get("/users/@me/guilds") or []]
    if not guild_ids:
        raise SystemExit("The bot is not in any Discord server yet. Add it with its invite link.")

    for guild_id in guild_ids:
        for channel in dc.get(f"/guilds/{guild_id}/channels") or []:
            if channel.get("name") == cfg.channel_name and channel.get("type") in TEXT_CHANNEL_TYPES + THREAD_CHANNEL_TYPES:
                print(f"Found #{cfg.channel_name}: channel {channel['id']} in server {guild_id}.")
                return channel, guild_id

    raise SystemExit(f"No channel named #{cfg.channel_name} that the bot can see.")


def read_posts(dc: Discord, channel: dict, guild_id: str, cfg: Config) -> Iterator[Post]:
    """Posts newest first."""
    kind = channel.get("type")
    if kind in TEXT_CHANNEL_TYPES:
        yield from read_text_channel(dc, str(channel["id"]), cfg)
    elif kind in THREAD_CHANNEL_TYPES:
        yield from read_forum(dc, str(channel["id"]), guild_id, cfg)
    else:
        raise SystemExit(f"#{channel.get('name')} is a channel type this cannot read ({kind}).")


def read_text_channel(dc: Discord, channel_id: str, cfg: Config) -> Iterator[Post]:
    before = None
    seen = 0

    while seen < cfg.max_messages:
        limit = min(100, cfg.max_messages - seen)
        params = {"limit": limit}
        if before:
            params["before"] = before

        batch = dc.get(f"/channels/{channel_id}/messages", params) or []
        for message in batch:
            seen += 1
            yield post_from_message(message, channel_id)

        if len(batch) < limit:
            return
        before = batch[-1]["id"]


def read_forum(dc: Discord, channel_id: str, guild_id: str, cfg: Config) -> Iterator[Post]:
    """Forum and media channels: every post is a thread, its first message holds the pictures."""
    threads = {}

    active = dc.get(f"/guilds/{guild_id}/threads/active") or {}
    for thread in active.get("threads", []):
        if str(thread.get("parent_id")) == channel_id:
            threads[str(thread["id"])] = thread

    before = None
    while len(threads) < cfg.max_messages:
        params = {"limit": 100}
        if before:
            params["before"] = before
        page = dc.get(f"/channels/{channel_id}/threads/archived/public", params) or {}
        batch = page.get("threads", [])
        for thread in batch:
            threads[str(thread["id"])] = thread
        if not page.get("has_more") or not batch:
            break
        before = batch[-1].get("thread_metadata", {}).get("archive_timestamp")
        if not before:
            break

    newest_first = sorted(threads, key=int, reverse=True)[: cfg.max_messages]
    for thread_id in newest_first:
        try:
            # A forum post's first message has the thread's own id.
            message = dc.get(f"/channels/{thread_id}/messages/{thread_id}")
        except NotFound:
            continue
        yield post_from_message(message, thread_id)


# ---------------------------------------------------------------------------- approval

class Approvers:
    """Who may approve: the owner, Administrator or Manage Messages, or the lists in config.json."""

    def __init__(self, dc: Discord, guild_id: str, cfg: Config):
        self.dc = dc
        self.guild_id = guild_id
        self.cfg = cfg
        guild = dc.get(f"/guilds/{guild_id}") or {}
        self.owner_id = str(guild.get("owner_id", ""))
        self.role_permissions = {str(r["id"]): int(r.get("permissions") or 0) for r in guild.get("roles", [])}
        self.everyone = self.role_permissions.get(guild_id, 0)
        self.known = {}

    def can_approve(self, user_id: str) -> bool:
        if user_id in self.cfg.approver_user_ids or user_id == self.owner_id:
            return True
        if user_id not in self.known:
            self.known[user_id] = self._check_member(user_id)
        return self.known[user_id]

    def _check_member(self, user_id: str) -> bool:
        try:
            member = self.dc.get(f"/guilds/{self.guild_id}/members/{user_id}") or {}
        except NotFound:
            return False  # left the server

        roles = [str(r) for r in member.get("roles", [])]
        if any(r in self.cfg.approver_role_ids for r in roles):
            return True

        permissions = self.everyone
        for role in roles:
            permissions |= self.role_permissions.get(role, 0)
        return bool(permissions & (ADMINISTRATOR | MANAGE_MESSAGES))


def plain_emoji(name: str) -> str:
    # The same emoji with and without the "show as emoji" mark.
    return name.replace("️", "")


def reaction_key(reaction: dict) -> str:
    """How the API names a reaction: the emoji itself, or name:id for a server emoji."""
    emoji = reaction.get("emoji") or {}
    if emoji.get("id"):
        return f"{emoji.get('name')}:{emoji['id']}"
    return str(emoji.get("name") or "")


def is_approve_reaction(reaction: dict, cfg: Config) -> bool:
    """config.json lists an emoji itself, or a server emoji by name or name:id."""
    wanted = {plain_emoji(e) for e in cfg.approve_emoji}
    emoji = reaction.get("emoji") or {}
    name = plain_emoji(str(emoji.get("name") or ""))
    if emoji.get("id"):
        return name in wanted or f"{name}:{emoji['id']}" in wanted
    return name in wanted


def reactors(dc: Discord, post: Post, key: str) -> Iterator[dict]:
    path_key = urllib.parse.quote(key, safe=":")
    after = None
    while True:
        params = {"limit": 100}
        if after:
            params["after"] = after
        users = dc.get(f"/channels/{post.channel_id}/messages/{post.message_id}/reactions/{path_key}", params) or []
        yield from users
        if len(users) < 100:
            return
        after = users[-1]["id"]


def approved_by(dc: Discord, post: Post, approvers: Approvers, cfg: Config) -> str | None:
    """The moderator who approved this post, or None."""
    for reaction in post.reactions:
        if not is_approve_reaction(reaction, cfg):
            continue
        for user in reactors(dc, post, reaction_key(reaction)):
            if user.get("bot"):
                continue
            if approvers.can_approve(str(user.get("id", ""))):
                return str(user["id"])
    return None


# ---------------------------------------------------------------------------- choosing pictures

@dataclass
class Item:
    attachment_id: str
    url: str
    size: int
    author_id: str
    author: str
    posted: str
    message_id: str


def is_picture(attachment: dict) -> bool:
    kind = str(attachment.get("content_type") or "").lower()
    name = str(attachment.get("filename") or "").lower()
    if kind == "image/svg+xml" or name.endswith(".svg"):
        return False
    return kind.startswith("image/") or name.endswith(PICTURE_EXTENSIONS)


def is_spoiler(attachment: dict) -> bool:
    name = str(attachment.get("filename") or "")
    return name.startswith("SPOILER_") or bool(int(attachment.get("flags") or 0) & ATTACHMENT_SPOILER)


def big_enough(attachment: dict, cfg: Config) -> bool:
    width, height = attachment.get("width"), attachment.get("height")
    if not width or not height:
        return True  # unknown here; make_picture checks again
    return int(width) >= cfg.min_width and int(height) >= cfg.min_height


def choose_items(posts: Iterable[Post], dc: Discord, approvers: Approvers, cfg: Config) -> tuple:
    """Approved pictures, newest first, at most max_per_author each and max_images in all."""
    items = []
    per_author = Counter()
    stats = Counter()

    for post in posts:
        stats["posts read"] += 1

        if post.author_is_bot or not post.author_name:
            continue
        if post.author_id in cfg.blocked_user_ids or post.message_id in cfg.blocked_message_ids:
            stats["blocked"] += 1
            continue

        pictures = [a for a in post.attachments if is_picture(a)]
        if not pictures:
            continue
        stats["posts with pictures"] += 1

        usable = [a for a in pictures if not is_spoiler(a) and big_enough(a, cfg)
                  and int(a.get("size") or 0) <= MAX_SOURCE_BYTES]
        stats["pictures left out (spoiler, too small or too big)"] += len(pictures) - len(usable)
        if not usable:
            continue

        if not approved_by(dc, post, approvers, cfg):
            stats["posts waiting for approval"] += 1
            continue
        stats["approved posts"] += 1

        for attachment in usable:
            if per_author[post.author_id] >= cfg.max_per_author:
                stats["pictures over the per-player limit"] += 1
                continue
            if len(items) >= cfg.max_images:
                break
            items.append(Item(
                attachment_id=str(attachment["id"]),
                url=str(attachment.get("url") or attachment.get("proxy_url") or ""),
                size=int(attachment.get("size") or 0),
                author_id=post.author_id,
                author=post.author_name,
                posted=post.posted,
                message_id=post.message_id,
            ))
            per_author[post.author_id] += 1

        if len(items) >= cfg.max_images:
            break

    return items, stats


def source_signature(cfg: Config, items: list) -> str:
    """Changes whenever the site would come out different."""
    data = {
        "manifest": MANIFEST_VERSION,
        "pictures": cfg.picture_tag(),
        "enabled": cfg.enabled,
        "rotate": cfg.rotate_seconds,
        "items": [[i.attachment_id, i.author] for i in items],
    }
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------- pictures

def make_picture(data: bytes, cfg: Config) -> bytes:
    """Any picture -> a width x height JPEG without photo metadata (location, camera...).

    A picture close to the right shape is cropped to fill. One far from it (a phone held
    upright, a very wide screen) is shown whole over a blurred, darkened copy of itself.
    """
    from PIL import Image, ImageCms, ImageEnhance, ImageFilter, ImageOps

    Image.MAX_IMAGE_PIXELS = 80_000_000
    width, height = cfg.image_width, cfg.image_height

    image = Image.open(io.BytesIO(data))
    image.seek(0)  # first frame of a GIF or animated WebP
    icc = image.info.get("icc_profile")

    # Phones often store a picture on its side with a note to turn it.
    image = ImageOps.exif_transpose(image)

    if image.width < cfg.min_width or image.height < cfg.min_height:
        raise ValueError(f"too small ({image.width}x{image.height})")

    # Transparent parts over a dark grey instead of whatever colour they hide.
    if image.mode in ("RGBA", "LA", "PA") or (image.mode == "P" and "transparency" in image.info):
        rgba = image.convert("RGBA")
        flat = Image.new("RGB", rgba.size, (28, 28, 30))
        flat.paste(rgba, mask=rgba.getchannel("A"))
        image = flat
    else:
        image = image.convert("RGB")

    # Wide-gamut pictures (Display P3 from phones and Macs) to plain sRGB.
    if icc:
        try:
            source = ImageCms.ImageCmsProfile(io.BytesIO(icc))
            image = ImageCms.profileToProfile(image, source, ImageCms.createProfile("sRGB"), outputMode="RGB")
        except Exception:
            pass

    target = width / height
    shape = image.width / image.height
    lost = 1 - (target / shape if shape > target else shape / target)

    if lost <= 0.15:
        picture = ImageOps.fit(image, (width, height), Image.LANCZOS)
    else:
        picture = ImageOps.fit(image, (width, height), Image.LANCZOS).filter(ImageFilter.GaussianBlur(28))
        picture = ImageEnhance.Brightness(picture).enhance(0.45)
        scale = min(width / image.width, height / image.height)
        front = image.resize((max(1, round(image.width * scale)), max(1, round(image.height * scale))), Image.LANCZOS)
        picture.paste(front, ((width - front.width) // 2, (height - front.height) // 2))

    out = io.BytesIO()
    # No exif= and no icc_profile=: nothing from the original file is kept.
    picture.save(out, "JPEG", quality=cfg.jpeg_quality, optimize=True, progressive=False, subsampling="4:2:0")
    return out.getvalue()


# ---------------------------------------------------------------------------- the site

def fetch_json(url: str, transport: Transport) -> dict | None:
    try:
        status, _, body = transport(url, {"User-Agent": USER_AGENT, "Cache-Control": "no-cache"}, 20)
    except OSError:
        return None
    if status != 200:
        return None
    try:
        data = json.loads(body.decode("utf-8"))
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def deployed_manifest(pages_url: str, transport: Transport) -> dict | None:
    if not pages_url:
        return None
    # A new query string gets past the CDN's 10 minute cache.
    return fetch_json(f"{pages_url.rstrip('/')}/manifest.json?run={int(time.time())}", transport)


def fetch_bytes(url: str, transport: Transport, limit: int) -> bytes | None:
    for attempt in range(3):
        try:
            status, _, body = transport(url, {"User-Agent": USER_AGENT}, 60)
        except OSError:
            time.sleep(2 * (attempt + 1))
            continue
        if status == 200 and len(body) <= limit:
            return body
        if status < 500:
            return None
        time.sleep(2 * (attempt + 1))
    return None


def build_site(out_dir: str, cfg: Config, items: list, signature: str, pages_url: str,
               deployed: dict | None, transport: Transport = http_get,
               picture_maker: Callable[[bytes, Config], bytes] = make_picture) -> dict:
    os.makedirs(os.path.join(out_dir, "img"), exist_ok=True)
    tag = cfg.picture_tag()
    live = {str(i.get("image")) for i in (deployed or {}).get("items", []) if isinstance(i, dict)}

    entries = []
    for item in items:
        name = f"img/{item.attachment_id}_{tag}.jpg"
        data = None

        # Already made in an earlier run: take it from the live site, not from Discord.
        if pages_url and name in live:
            data = fetch_bytes(f"{pages_url.rstrip('/')}/{name}", transport, 4 * 1024 * 1024)
            if data is not None and not data.startswith(JPEG_MAGIC):
                data = None

        if data is None:
            source = fetch_bytes(item.url, transport, MAX_SOURCE_BYTES) if item.url else None
            if source is None:
                print(f"  could not download picture {item.attachment_id}, left out")
                continue
            try:
                data = picture_maker(source, cfg)
            except Exception as error:  # a broken or odd file must not stop the rest
                print(f"  picture {item.attachment_id} left out: {error}")
                continue

        with open(os.path.join(out_dir, name), "wb") as f:
            f.write(data)

        entries.append({
            "id": item.attachment_id,
            "image": name,
            "author": item.author,
            "posted": item.posted,
        })
        print(f"  {name}  @{item.author}")

    manifest = {
        "version": MANIFEST_VERSION,
        "enabled": bool(cfg.enabled),
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "rotate_seconds": cfg.rotate_seconds,
        "source": signature,
        "items": entries,
    }

    # ASCII only, so any JSON reader takes it.
    with open(os.path.join(out_dir, "manifest.json"), "w", encoding="utf-8", newline="\n") as f:
        json.dump(manifest, f, ensure_ascii=True, indent=1)
        f.write("\n")

    with open(os.path.join(out_dir, "index.html"), "w", encoding="utf-8", newline="\n") as f:
        f.write(index_page(manifest))

    # Plain files; no Jekyll step on GitHub Pages.
    open(os.path.join(out_dir, ".nojekyll"), "w").close()
    return manifest


def index_page(manifest: dict) -> str:
    cards = "\n".join(
        f'<figure><img src="{html.escape(e["image"])}" loading="lazy" alt="">'
        f'<figcaption>@{html.escape(e["author"])}</figcaption></figure>'
        for e in manifest["items"]
    ) or "<p>Nothing approved yet.</p>"
    state = "" if manifest["enabled"] else "<p><b>Turned off in config.json: the game shows nothing.</b></p>"
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="robots" content="noindex">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Share your shop</title>
<style>
body {{ font: 15px/1.4 system-ui, sans-serif; margin: 24px; background: #1d1b19; color: #f5e6cf; }}
main {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(320px, 1fr)); gap: 18px; }}
figure {{ margin: 0; }} img {{ width: 100%; display: block; border-radius: 6px; }}
figcaption {{ margin-top: 6px; opacity: .85; }} small {{ opacity: .6; }}
</style></head><body>
<h1>Share your shop</h1>
<p><small>{len(manifest["items"])} pictures in the game's main menu. Updated {html.escape(manifest["updated"])}.</small></p>
{state}
<main>
{cards}
</main>
</body></html>
"""


# ---------------------------------------------------------------------------- main

def set_output(name: str, value: str) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{name}={value}\n")


def env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes")


def main(argv=None, transport: Transport = http_get,
         picture_maker: Callable[[bytes, Config], bytes] = make_picture) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--out", default="site")
    parser.add_argument("--pages-url", default=os.environ.get("PAGES_URL", ""))
    parser.add_argument("--force", action="store_true", default=env_flag("FORCE"),
                        help="build the site even if nothing changed")
    args = parser.parse_args(argv)

    cfg = Config.load(args.config)
    token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    deployed = deployed_manifest(args.pages_url, transport)

    items = []
    if not cfg.enabled:
        print("Turned off in config.json: the site will list nothing.")
    elif not token:
        if not args.force:
            print("::warning::DISCORD_BOT_TOKEN is not set. Add it under Settings > Secrets and variables > Actions.")
            set_output("changed", "false")
            return 0
        print("No Discord token: building an empty site.")
    else:
        dc = Discord(token, transport)
        try:
            channel, guild_id = find_channel(dc, cfg)
            approvers = Approvers(dc, guild_id, cfg)
            items, stats = choose_items(read_posts(dc, channel, guild_id, cfg), dc, approvers, cfg)
        except Forbidden as error:
            print(f"::error::{error}. Check the token, that the bot is in the server, "
                  "that it can see the channel, and that Message Content Intent is on.")
            return 1
        for name, count in stats.items():
            print(f"{name}: {count}")
        print(f"pictures chosen: {len(items)}  (Discord calls: {dc.calls})")

    signature = source_signature(cfg, items)
    if not args.force and deployed and deployed.get("source") == signature:
        print("No change since the last deploy.")
        set_output("changed", "false")
        return 0

    manifest = build_site(args.out, cfg, items, signature, args.pages_url, deployed, transport, picture_maker)
    print(f"Site built: {len(manifest['items'])} pictures.")
    set_output("changed", "true")
    return 0


if __name__ == "__main__":
    sys.exit(main())
