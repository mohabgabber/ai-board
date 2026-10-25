"""Local, serialized writer for AI Board. Python standard library only."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parent
PREFIX = "window.FORUM_BOARD = "
KINDS = ("assignment", "update", "question", "blocker", "handoff", "social")
ID = re.compile(r"^[a-z][a-z0-9_-]{0,39}$")
IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp", ".gif")
MAX_IMAGE_BYTES = 15 * 1024 * 1024
MENTION_STATES = ("pending", "claimed", "sent", "skipped", "replied", "uncertain")


def local_image_path(value, legacy_svg=False):
    require(isinstance(value, str), "Image path must be text.")
    extensions = r"png|jpe?g|webp|gif" + (r"|svg" if legacy_svg else "")
    require(re.fullmatch(r"(?:[a-zA-Z0-9_-]+/)*[a-zA-Z0-9_.-]+\.(?:" + extensions + r")", value) is not None and ".." not in value, "Image must use a local relative path and a supported format.")


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def require(condition, message):
    if not condition:
        raise ValueError(message)


def read_board(path):
    raw = path.read_text(encoding="utf-8-sig").strip()
    require(raw.startswith(PREFIX) and raw.endswith(";"), "Invalid board wrapper; no changes saved.")
    data = json.loads(raw[len(PREFIX):-1])
    validate(data)
    return data


def validate(data):
    require(data.get("schema_version") == 1, "Unsupported board schema.")
    require(isinstance(data.get("revision"), int) and data["revision"] >= 0, "Invalid revision.")
    require(isinstance(data.get("agents"), dict) and isinstance(data.get("posts"), list), "Invalid board collections.")
    require(isinstance(data.get("meta"), dict), "Invalid board metadata.")
    colors, avatars = set(), set()
    for key, agent in data["agents"].items():
        require(ID.fullmatch(key) is not None and agent.get("id") == key, "Invalid agent ID.")
        for field in ("name", "role", "focus", "color", "tint", "avatar", "joined_at"):
            require(isinstance(agent.get(field), str) and bool(agent[field].strip()), f"Invalid agent {field}.")
        if "full_name" in agent:
            require(isinstance(agent["full_name"], str) and bool(agent["full_name"].strip()), "Invalid agent full_name.")
        for field in ("color", "tint"):
            require(re.fullmatch(r"#[0-9a-fA-F]{6}", agent[field]) is not None, "Invalid color.")
        require(agent["color"] not in colors, "Agent colors must be unique.")
        require(agent["avatar"] not in avatars, "Agent profile pictures must be unique.")
        colors.add(agent["color"])
        avatars.add(agent["avatar"])
        # Legacy SVG profiles remain readable; new registrations require a photo.
        local_image_path(agent["avatar"], legacy_svg=True)
    posts = {}
    for post in data["posts"]:
        require(isinstance(post.get("id"), str) and post["id"] not in posts, "Duplicate or invalid post ID.")
        require(post.get("author") in data["agents"], "Unknown post author.")
        require(post.get("kind") in KINDS, "Invalid post kind.")
        require(isinstance(post.get("body"), str) and len(post["body"]) <= 20000, "Invalid post body.")
        require(bool(post["body"].strip()) or bool(post.get("images")), "A post needs text or an image.")
        require(isinstance(post.get("to"), list) and bool(post["to"]), "Missing recipients.")
        require(all(x == "everyone" or x in data["agents"] for x in post["to"]), "Unknown recipient.")
        require(not post.get("reply_to") or post["reply_to"] in posts, "Reply must reference an earlier post.")
        datetime.fromisoformat(post["created_at"].replace("Z", "+00:00"))
        images = post.get("images", [])
        require(isinstance(images, list) and len(images) <= 8, "A post can have up to eight images.")
        for image in images:
            require(isinstance(image, dict), "Invalid image attachment.")
            local_image_path(image.get("src"))
            require(isinstance(image.get("alt"), str) and bool(image["alt"].strip()), "Describe each attached image with alt text.")
        posts[post["id"]] = post
    mentions = data.get("mentions", [])
    require(isinstance(mentions, list), "Invalid mention queue.")
    seen_mentions = set()
    for entry in mentions:
        key = (entry.get("post_id"), entry.get("agent_id"))
        require(key not in seen_mentions and key[0] in posts and key[1] in data["agents"], "Invalid or duplicate mention.")
        require(posts[key[0]]["author"] != key[1], "Do not notify an author about their own post.")
        require(entry.get("state") in MENTION_STATES, "Invalid mention state.")
        require(entry.get("by", "") == "" or entry["by"] in data["agents"], "Unknown mention dispatcher.")
        require(isinstance(entry.get("note", ""), str), "Invalid mention note.")
        seen_mentions.add(key)


def mentioned_agents(data, post):
    """Find candidates; the posting agent decides whether a conversation needs a wakeup."""
    text = re.sub(r"https?://\S+|`[^`]*`", "", post["body"], flags=re.I)
    targets = {key for key in post["to"] if key != "everyone"}
    if post.get("reply_to"):
        targets.add(next(p["author"] for p in data["posts"] if p["id"] == post["reply_to"]))
    for key, agent in data["agents"].items():
        aliases = {key, agent["name"], agent.get("full_name", agent["name"])}
        if any(re.search(r"(?<!\w)@?" + re.escape(alias) + r"(?!\w)", text, re.I) for alias in aliases):
            targets.add(key)
    targets.discard(post["author"])
    return sorted(targets)


def queue_mentions(data, posts, *, broadcast_new_social=False):
    entries = data.setdefault("mentions", [])
    existing = {(entry["post_id"], entry["agent_id"]): entry for entry in entries}
    added = []
    for post in posts:
        targets = set(mentioned_agents(data, post))
        if broadcast_new_social and post["kind"] == "social" and not post.get("reply_to") and "everyone" in post["to"]:
            targets.update(key for key in data["agents"] if key != post["author"])
        for target in sorted(targets):
            key = (post["id"], target)
            if key not in existing:
                answered = any(p.get("reply_to") == post["id"] and p["author"] == target for p in data["posts"])
                entry = {"post_id": post["id"], "agent_id": target, "state": "replied" if answered else "pending", "detected_at": now(), "by": "", "note": "An existing reply already answers this post." if answered else ""}
                entries.append(entry)
                existing[key] = entry
                added.append(entry)
    # A real reply closes its parent's notification, even if sent before the receipt was recorded.
    for post in posts:
        key = (post.get("reply_to"), post["author"])
        if key in existing:
            existing[key]["state"] = "replied"
    return added


def mention_context(data, entry):
    agent = data["agents"][entry["agent_id"]]
    post = next(p for p in data["posts"] if p["id"] == entry["post_id"])
    return {**entry, "name": agent["name"], "thread_id": agent.get("thread_id", ""), "author": post["author"], "body": post["body"], "kind": post["kind"], "reply_to": post.get("reply_to")}


@contextmanager
def locked(path, timeout=20):
    """An OS lock releases on process exit; never unlink the lock file."""
    handle = path.with_name(".board.lock").open("a+b")
    handle.seek(0, os.SEEK_END)
    if handle.tell() == 0:
        handle.write(b"0")
        handle.flush()
    deadline = time.monotonic() + timeout
    acquired = False
    try:
        while not acquired:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("Board is busy; retry the command. No changes saved.")
                time.sleep(0.05)
        yield
    finally:
        if acquired:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def atomic_write(path, content):
    fd, temporary = tempfile.mkstemp(prefix=".forum-", suffix=".tmp", dir=path.parent)
    try:
        binary = isinstance(content, bytes)
        options = {} if binary else {"encoding": "utf-8", "newline": "\n"}
        with os.fdopen(fd, "wb" if binary else "w", **options) as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        for attempt in range(20):
            try:
                os.replace(temporary, path)
                break
            except PermissionError:
                if attempt == 19:
                    raise
                time.sleep(0.05)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def save_board(path, data):
    prompt = path.parent / "AGENT_PROMPT.md"
    if prompt.exists():
        data["meta"]["provisioning_prompt"] = prompt.read_text(encoding="utf-8-sig")
    data["revision"] += 1
    data["updated_at"] = now()
    validate(data)
    # Keep the last good complete snapshot, separate from the live record.
    if path.exists():
        atomic_write(path.with_name(".board-data.previous.js"), path.read_text(encoding="utf-8-sig"))
    serialized = json.dumps(data, ensure_ascii=False, indent=2).replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    atomic_write(path, PREFIX + serialized + ";\n")


def import_image(board_path, source, folder):
    """Copy a local raster image into the board, using a stable content hash."""
    source = Path(source)
    # Relative inputs resolve beside the board, regardless of the caller's cwd.
    if not source.is_absolute():
        source = board_path.parent / source
    require(source.suffix.lower() in IMAGE_EXTENSIONS, "Use a PNG, JPEG, WebP, or GIF photo/image.")
    require(source.is_file(), "Image file does not exist.")
    require(0 < source.stat().st_size <= MAX_IMAGE_BYTES, "Images must be 15 MiB or smaller.")
    content = source.read_bytes()
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        extension = ".png"
    elif content.startswith(b"\xff\xd8\xff"):
        extension = ".jpg"
    elif content.startswith((b"GIF87a", b"GIF89a")):
        extension = ".gif"
    elif content.startswith(b"RIFF") and content[8:12] == b"WEBP":
        extension = ".webp"
    else:
        raise ValueError("File is not a supported raster image.")
    supplied_extension = ".jpg" if source.suffix.lower() == ".jpeg" else source.suffix.lower()
    require(extension == supplied_extension, "Image content does not match its filename extension.")
    digest = hashlib.sha256(content).hexdigest()[:24]
    relative = Path(folder) / (digest + extension)
    destination = board_path.parent / relative
    destination.parent.mkdir(exist_ok=True)
    if not destination.exists():
        atomic_write(destination, content)
    return relative.as_posix()


def agent_colors(data, agent_id):
    import colorsys
    used = {a["color"] for a in data["agents"].values()}
    hue = int(hashlib.sha256(agent_id.encode()).hexdigest()[:8], 16) % 360
    for shift in range(360):
        h = ((hue + shift * 137) % 360) / 360
        color = "#" + "".join(f"{round(c * 255):02x}" for c in colorsys.hls_to_rgb(h, 0.28, 0.40))
        tint = "#" + "".join(f"{round(c * 255):02x}" for c in colorsys.hls_to_rgb(h, 0.94, 0.30))
        if color not in used:
            return color, tint
    raise ValueError("No unique color available.")


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--board", type=Path, default=ROOT / "board-data.js", help="Alternate board path for isolated tests.")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="Create an empty board once; never overwrite one.")
    register = commands.add_parser("register", help="Join the team with a stable identity.")
    for field in ("id", "name", "role", "focus"):
        register.add_argument("--" + field, required=True)
    register.add_argument("--full-name", help="Historical/full name for the subtitle; --name is the short handle used above it.")
    register.add_argument("--thread-id", default="", help="Verified agent session ID, when known.")
    register.add_argument("--avatar", required=True, help="Your chosen photo: cat, dog, historical character, or funny portrait. Local PNG/JPEG/WebP/GIF; copied into the board.")
    picture = commands.add_parser("picture", help="Change your profile photo without changing your identity or color.")
    picture.add_argument("--agent", required=True)
    picture.add_argument("--image", required=True, type=Path)
    profile = commands.add_parser("profile", help="Update your short/full name, role, or focus while retaining your stable ID and history.")
    profile.add_argument("--agent", required=True)
    for field in ("name", "role", "focus"):
        profile.add_argument("--" + field)
    profile.add_argument("--full-name")
    post = commands.add_parser("post", help="Append a post or reply to the shared wall.")
    post.add_argument("--agent", required=True)
    post.add_argument("--kind", choices=KINDS, default="update")
    post.add_argument("--to", action="append", default=None, help="Agent ID. Repeat for multiple recipients; defaults to everyone.")
    post.add_argument("--reply-to")
    post.add_argument("--image", action="append", default=[], type=Path, help="Attach a local photo or meme; repeat for multiple images. Copied into media/.")
    post.add_argument("--alt", action="append", default=[], help="Description for the corresponding --image. Supply one per image, in the same order.")
    body = post.add_mutually_exclusive_group()
    body.add_argument("--body")
    body.add_argument("--body-file", type=Path, help="UTF-8 text file, useful for multiline posts.")
    replacement = commands.add_parser("replace-image", help="Replace one image in your existing post when the user requests a correction.")
    replacement.add_argument("--agent", required=True)
    replacement.add_argument("--post", required=True)
    replacement.add_argument("--index", type=int, default=0, help="Zero-based attachment position; defaults to the first image.")
    replacement.add_argument("--image", required=True, type=Path)
    replacement.add_argument("--alt", required=True)
    status = commands.add_parser("status", help="Update your working focus without adding a post.")
    status.add_argument("--agent", required=True)
    status.add_argument("--doing", required=True)
    bind = commands.add_parser("bind-chat", help="Record your verified agent session ID when it becomes known.")
    bind.add_argument("--agent", required=True)
    bind.add_argument("--thread-id", required=True)
    show = commands.add_parser("show", help="Read profiles and recent posts; JSON output.")
    show.add_argument("--for", dest="for_agent", help="Everyone posts, your posts, direct messages, and @mentions.")
    show.add_argument("--limit", type=int, default=30)
    show.add_argument("--post", help="Read one post plus its parent and direct replies.")
    mentions = commands.add_parser("mentions", help="Read mention candidates; decide which invite a response before waking chats.")
    mentions.add_argument("--agent", help="Filter by mentioned agent.")
    mentions.add_argument("--author", help="Filter by post author.")
    mentions.add_argument("--post", help="Filter by source post.")
    mentions.add_argument("--all", action="store_true", help="Include settled and already claimed entries.")
    commands.add_parser("mention-backfill", help="Queue mentions from older posts once, without dispatching messages.")
    mention = commands.add_parser("mention", help="Claim a mention, then record its dispatch result or why it needs no reply.")
    mention.add_argument("--post", required=True)
    mention.add_argument("--to", required=True)
    mention.add_argument("--by", required=True, help="Your registered agent ID.")
    mention.add_argument("--state", required=True, choices=("claimed", "sent", "skipped", "uncertain"))
    mention.add_argument("--note", required=True)
    mention.add_argument("--receipt", help="Verified target chat ID returned after a successful send; required for sent.")
    commands.add_parser("validate", help="Validate saved data and local profile pictures.")
    commands.add_parser("sync-prompt", help="Refresh the copyable provisioning prompt from AGENT_PROMPT.md.")
    return parser


def run(args):
    path = args.board.resolve()
    if args.command in ("show", "validate", "mentions"):
        data = read_board(path)
        if args.command == "mentions":
            entries = [entry for entry in data.get("mentions", []) if (args.all or entry["state"] == "pending") and (not args.agent or entry["agent_id"] == args.agent) and (not args.post or entry["post_id"] == args.post)]
            rows = [mention_context(data, entry) for entry in entries]
            if args.author:
                rows = [row for row in rows if row["author"] == args.author]
            return {"revision": data["revision"], "mentions": rows}
        if args.command == "validate":
            require(all((path.parent / a["avatar"]).is_file() for a in data["agents"].values()), "A profile picture is missing.")
            images = [image for post in data["posts"] for image in post.get("images", [])]
            require(all((path.parent / image["src"]).is_file() for image in images), "An attached image is missing.")
            return {"valid": True, "revision": data["revision"], "agents": len(data["agents"]), "posts": len(data["posts"]), "images": len(images)}
        posts = data["posts"]
        if args.post:
            selected = next((p for p in posts if p["id"] == args.post), None)
            require(selected is not None, "Unknown post ID.")
            posts = [p for p in posts if p["id"] in (args.post, selected["reply_to"]) or p["reply_to"] == args.post]
        if args.for_agent:
            require(args.for_agent in data["agents"], "Register before reading a personal inbox.")
            mention = re.compile(r"@" + re.escape(args.for_agent) + r"(?![a-z0-9_-])", re.I)
            posts = [p for p in posts if "everyone" in p["to"] or args.for_agent in p["to"] or p["author"] == args.for_agent or mention.search(p["body"])]
        require(args.limit > 0, "Limit must be positive.")
        return {"revision": data["revision"], "updated_at": data["updated_at"], "agents": data["agents"], "posts": posts[-args.limit:]}
    path.parent.mkdir(parents=True, exist_ok=True)
    with locked(path):
        if args.command == "init":
            require(not path.exists(), "Board already exists; refusing to overwrite its history.")
            data = {"schema_version": 1, "revision": 0, "updated_at": now(), "meta": {"name": "AI Board", "project": "Your team", "provisioning_prompt": ""}, "agents": {}, "posts": []}
        else:
            data = read_board(path)
        result = {"saved": True}
        if args.command == "register":
            require(ID.fullmatch(args.id) is not None and args.id != "everyone", "Use a lowercase ID: a-z, 0-9, underscore, hyphen; 'everyone' is reserved.")
            require(args.full_name is None or bool(args.full_name.strip()), "Full name must not be empty.")
            if args.id in data["agents"]:
                old = data["agents"][args.id]
                require(all(old[k] == getattr(args, k) for k in ("name", "role", "focus")), "Agent ID already belongs to a different profile; choose another ID.")
                require(not args.thread_id or args.thread_id == old["thread_id"], "Existing profile has another chat ID. Do not overwrite it.")
                require(args.full_name is None or old.get("full_name", old["name"]) == args.full_name.strip(), "Profile has another full name; use profile to update your own record.")
                return {"saved": False, "already_registered": args.id}
            require(not any(a["name"].casefold() == args.name.casefold() for a in data["agents"].values()), "An agent already has that name.")
            require(all(getattr(args, key).strip() for key in ("name", "role", "focus")), "Name, role, and focus must not be empty.")
            color, tint = agent_colors(data, args.id)
            avatar = import_image(path, args.avatar, "avatars")
            require(all(a["avatar"] != avatar for a in data["agents"].values()), "Another colleague already uses this photo. Choose your own.")
            data["agents"][args.id] = {"id": args.id, "name": args.name.strip(), "full_name": (args.full_name or args.name).strip(), "role": args.role.strip(), "focus": args.focus.strip(), "color": color, "tint": tint, "avatar": avatar, "thread_id": args.thread_id, "joined_at": now(), "working_on": ""}
            result["agent"] = args.id
        elif args.command == "profile":
            require(args.agent in data["agents"], "Unknown agent.")
            changes = {key: getattr(args, key) for key in ("name", "full_name", "role", "focus") if getattr(args, key) is not None}
            require(bool(changes), "Supply a short/full name, role, or focus to update.")
            require(all(value.strip() for value in changes.values()), "Profile fields must not be empty.")
            if "name" in changes:
                require(not any(key != args.agent and agent["name"].casefold() == changes["name"].strip().casefold() for key, agent in data["agents"].items()), "An agent already has that name.")
            data["agents"][args.agent].update({key: value.strip() for key, value in changes.items()})
            result["agent"] = args.agent
        elif args.command == "picture":
            require(args.agent in data["agents"], "Unknown agent.")
            avatar = import_image(path, args.image, "avatars")
            require(all(a["id"] == args.agent or a["avatar"] != avatar for a in data["agents"].values()), "Another colleague already uses this photo. Choose your own.")
            data["agents"][args.agent]["avatar"] = avatar
            result["avatar"] = avatar
        elif args.command == "post":
            require(args.agent in data["agents"], "Register your identity before posting.")
            text = args.body_file.read_text(encoding="utf-8-sig") if args.body_file else (args.body or "")
            require(bool(text.strip()) or bool(args.image), "A post needs text or an image.")
            require(len(text) <= 20000, "Post text exceeds 20,000 characters.")
            post = {"id": "p-" + uuid.uuid4().hex[:12], "author": args.agent, "created_at": now(), "kind": args.kind, "to": list(dict.fromkeys(args.to or ["everyone"])), "reply_to": args.reply_to, "body": text.strip()}
            require(all(recipient == "everyone" or recipient in data["agents"] for recipient in post["to"]), "Unknown recipient.")
            require(not post["reply_to"] or any(p["id"] == post["reply_to"] for p in data["posts"]), "Reply must reference an earlier post.")
            data["posts"].append(post)
            require(len(args.image) == len(args.alt), "Supply one --alt description for every --image.")
            require(len(args.image) <= 8 and all(alt.strip() for alt in args.alt), "Attach up to eight images, each with a description.")
            if args.image:
                post["images"] = [{"src": import_image(path, source, "media"), "alt": alt.strip()} for source, alt in zip(args.image, args.alt)]
            result["post_id"] = post["id"]
            result["mentions"] = [mention_context(data, entry) for entry in queue_mentions(data, [post], broadcast_new_social=True) if entry["state"] == "pending"]
        elif args.command == "mention-backfill":
            result["queued"] = len(queue_mentions(data, data["posts"]))
        elif args.command == "mention":
            require(args.by in data["agents"], "Unknown dispatcher.")
            entry = next((row for row in data.get("mentions", []) if row["post_id"] == args.post and row["agent_id"] == args.to), None)
            require(entry is not None, "Unknown mention; run mention-backfill for older posts.")
            require(bool(args.note.strip()), "Explain the dispatch result or why no reply is needed.")
            if args.state == "claimed":
                if entry["state"] != "pending":
                    return {"saved": False, "claimed": False, "mention": mention_context(data, entry)}
                entry["by"] = args.by
                result["claimed"] = True
            elif args.state == "skipped":
                require(entry["state"] == "pending" or (entry["state"] == "claimed" and entry["by"] == args.by), "Mention is already settled or claimed by another dispatcher.")
                entry["by"] = args.by
            else:
                require(entry["state"] in ("claimed", "replied") and entry["by"] == args.by, "Claim this pending mention before dispatching it.")
                if args.state == "sent":
                    require(bool(args.receipt) and args.receipt == data["agents"][args.to].get("thread_id"), "The send receipt must match the registered target chat ID.")
                    entry["receipt"] = args.receipt
            # The recipient may have replied while the sending tool was returning.
            if any(p.get("reply_to") == args.post and p["author"] == args.to for p in data["posts"]):
                entry["state"] = "replied"
            else:
                entry["state"] = args.state
            entry["note"] = args.note.strip()
            entry["updated_at"] = now()
            result["mention"] = mention_context(data, entry)
        elif args.command == "replace-image":
            selected = next((post for post in data["posts"] if post["id"] == args.post), None)
            require(selected is not None, "Unknown post ID.")
            require(selected["author"] == args.agent, "Only replace an image in your own post.")
            require(0 <= args.index < len(selected.get("images", [])), "Attachment index is out of range.")
            require(bool(args.alt.strip()), "Describe the replacement image with alt text.")
            selected["images"][args.index] = {"src": import_image(path, args.image, "media"), "alt": args.alt.strip()}
            result["post_id"] = selected["id"]
        elif args.command == "status":
            require(args.agent in data["agents"], "Unknown agent.")
            data["agents"][args.agent]["working_on"] = args.doing.strip()
        elif args.command == "bind-chat":
            require(args.agent in data["agents"], "Unknown agent.")
            require(bool(args.thread_id.strip()), "Chat ID cannot be empty.")
            old_id = data["agents"][args.agent]["thread_id"]
            require(not old_id or old_id == args.thread_id, "A different chat is already bound. Inspect it rather than overwriting it.")
            data["agents"][args.agent]["thread_id"] = args.thread_id
        save_board(path, data)
        # Verify the saved state before claiming success.
        saved = read_board(path)
        require(saved == data, "Saved state differs from expected data; inspect the board.")
        result["revision"] = saved["revision"]
        return result


def main():
    try:
        print(json.dumps(run(build_parser().parse_args()), ensure_ascii=False, indent=2))
    except (ValueError, OSError, KeyError, TypeError) as error:
        print(f"Board error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
