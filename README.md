# Share your shop

Pictures that players post in **#share-your-shop** on our Discord, shown in the main menu of
*Livin' the Cream: Ice Cream Simulator* with the poster's Discord name (`@name`).

Nothing reaches the game unless a moderator approves it.

## How it works

```
Discord #share-your-shop
   |  a moderator reacts with a check mark to a post
   v
GitHub Actions, every hour (.github/workflows/sync.yml runs sync.py)
   |  reads the channel with a read-only bot, keeps approved posts only,
   |  makes 1280x720 JPEGs without photo metadata, writes manifest.json
   v
GitHub Pages: https://makifarslan.github.io/share-your-shop/
   |  manifest.json + img/*.jpg   (index.html shows what is live)
   v
The game: reads manifest.json when the main menu opens, downloads new pictures,
keeps them on disk, and shows them on the left of the menu.
```

A quiet hour costs a few Discord calls and deploys nothing.

## Moderating

- **Approve:** react with ✅ to the post. Approvals count from the server owner, anyone with
  **Administrator** or **Manage Messages**, and anyone listed in `config.json`. A player's own
  check mark does nothing.
- Every picture in an approved post is used, newest posts first: at most 3 per player and 40 in all.
  Spoiler pictures, pictures smaller than 640x360 and non-pictures are left out.
- **Remove a picture:** take the ✅ off, or delete the post. It leaves the site within the hour;
  each player's game drops its copy the next time the menu opens online.
- **Right now:** Actions > *Sync shops from Discord* > *Run workflow* updates the site at once.
- **Turn it all off:** set `"enabled": false` in `config.json` and commit. The site is rebuilt in a
  minute or two, and the game hides the panel and deletes its copies.
- **Block someone:** put their Discord user id in `blocked_user_ids` (or one post's message id in
  `blocked_message_ids`). Their posts never show, even with a ✅.

A pinned message in the channel should tell players that a picture posted there may be shown in
the game's main menu with their Discord name, and that deleting the post takes it out again.

## Settings (`config.json`)

| Setting | What it does |
| --- | --- |
| `enabled` | `false` = the game shows nothing and deletes its copies. |
| `channel_name` | The channel to read, found by name in the bot's servers. |
| `channel_id`, `guild_id` | Set these to pin the channel (survives a rename). The run log prints both. |
| `approve_emoji` | Emoji that approve. A server emoji by name or `name:id`. |
| `approver_role_ids`, `approver_user_ids` | Extra people who may approve. |
| `blocked_user_ids`, `blocked_message_ids` | Never shown. |
| `max_messages` | How many of the newest posts are read. |
| `max_images`, `max_per_author` | How many pictures in all, and per player. |
| `min_width`, `min_height` | Smaller pictures are left out. |
| `image_width`, `image_height`, `jpeg_quality` | The pictures the game gets. Changing them makes every game download them again. |
| `rotate_seconds` | Seconds each picture stays up in the menu (4 to 60). |

## Set up once

1. **Discord bot** (discord.com/developers): *Message Content Intent* on (without it Discord hides
   the pictures from bots), *Public Bot* off, added to the server with *View Channel* and
   *Read Message History* only.
2. **Token:** Settings > Secrets and variables > Actions > secret `DISCORD_BOT_TOKEN`.
3. **Pages:** Settings > Pages > Source: *GitHub Actions*.

## Costs and limits

Free. Public repositories do not pay for Actions minutes, and GitHub Pages allows about
100 GB of downloads a month: with ~40 pictures of ~200 KB, about 12,000 new players a month even if
none of them had a copy before (a game only downloads pictures it does not have).

GitHub stops the schedule of a public repository after 60 days without a commit, so the workflow
makes an empty commit after 45 quiet days.

The run logs are public. The script prints counts only, plus the names of approved posters, which
are on the site anyway.

## Tests

```
python -m unittest discover -s tests
```

They run against a fake Discord. The picture tests need Pillow and are skipped without it.
