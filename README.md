# Leave the Podcasts — Get the information from each show

![A microphone and long podcast sound wave becoming three short lines](assets/github-hero.png)

Choose podcasts and get a short Telegram report instead of listening for hours. Choose how many recent episodes to read and how many points to get from **each podcast**. The starting choice is three points per podcast; change it on the page. Each point says why it matters, with your own sceptical reading beneath that podcast.

## Download the program

**[Download Leave the Podcasts here](https://eochu.app/leave-podcasts/source.zip).** It downloads one ZIP file. Open it, then read **[START_HERE.md](START_HERE.md)** inside. The starter walks you through Telegram and opens your private settings page. You do not need GitHub's Branch button.

This version runs on a Mac or Linux computer with Python 3.10 or newer. It is not a phone app, and the Windows starter is not ready yet. The computer must stay on to send the morning report.

## Give this to your own AI

Send it this [project link](https://github.com/eochu-ollathair/leave-podcasts) and say: **“Read AGENTS.md and get my own copy working. Help me pick podcasts, make a real preview, and connect my Telegram. Show me what actually worked.”** The [assistant instructions](AGENTS.md) give it the exact checks. It should use your own accounts and keep your private details off GitHub.

The free report quotes useful speech and needs no artificial intelligence account. If you connect an artificial intelligence program of your own, it can choose different subjects within each episode, say why each matters, and put your sceptical reading beneath each podcast. A suspected motive stays a possibility until checked against records or direct testimony. The maker's program is not available to other users.

## For people who want to run it by hand

The starter runs `app.py serve` and checks once a minute whether the morning report is due. The private page controls podcasts, episode counts, points per podcast, delivery time and previews. It searches the public Apple Podcasts directory or uses an episode-list address you paste. It reads published speech text when available; otherwise your computer listens to temporary episode audio and turns it into words. The audio is deleted after reading, while recognised speech is kept in `data/` for later reports. An episode with no readable speech is left out. After an episode is sent, it is removed from the page and never chosen for another report.

You can also run `python3 app.py daily` from a scheduled task if you prefer. The starter keeps your bot code and chat number in the private `data/telegram.json` file, which is excluded from GitHub. A manual setup may instead set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` in its environment. The optional text AI uses `PODCAST_MODEL_URL`, `PODCAST_MODEL`, and if needed `PODCAST_MODEL_KEY`.

This version reads English speech. Longer episodes take longer to process. The report describes what speakers said; it does not independently prove their claims.

To put your own settings page on a public website, put it behind HTTPS and forward a private path to this app. Set `PODCAST_BASE` to that path. Keep `data/` and its private opening key out of public files.
