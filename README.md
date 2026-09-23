# Leave the Podcasts — Get the information from each show

## Let an AI assistant set it up for you

Give the assistant this GitHub page and say: **“Read [AGENTS.md](AGENTS.md). Set up my own copy on my Mac or Linux computer, connect my Telegram bot, send a real test report, and fix anything that fails.”**

Your part is to provide your own Telegram bot and let the assistant use that computer. It handles the download and setup. You do not need a paid artificial intelligence account. Keep the computer on for morning reports.

![A microphone and long podcast sound wave becoming three short lines](assets/github-hero.png)

Choose podcasts and get a short Telegram report instead of listening for hours. You can also include episodes from Ireland's popular podcast list, saved the evening before. Set how many episodes and points you want on the webpage. The free version quotes speech; if you connect your own text AI, it can explain why each point matters and add your sceptical view beneath each podcast.

### The webpage you get

![Leave the Podcasts settings page showing example podcasts and morning delivery choices](assets/settings-preview.png)

*A fresh copy with example choices. Your own settings page opens on your computer.*

## Download the program

**[Download Leave the Podcasts here](https://eochu.app/leave-podcasts/source.zip).** It downloads one ZIP file. Open it, then read **[START_HERE.md](START_HERE.md)** inside. The starter walks you through Telegram and opens your private settings page. You do not need GitHub's Branch button.

This version runs on a Mac or Linux computer with Python 3.10 or newer. It is not a phone app, and the Windows starter is not ready yet. The computer must stay on to send the morning report.

## Which Telegram chat?

Eochu's live reports come from [@cuntz2_bot](https://t.me/cuntz2_bot). Open that chat and send **“add podcast The Daily”** to add a show. Reply to a **new** podcast report with **“kill that podcast”** to remove and ignore that show. Each podcast gets its own message, so “that” names one show. For an older report, send **“kill podcast The Daily”** using the show's name. You can also send **“add YouTube The United Stand”** or **“kill YouTube The United Stand”** there when Leave YouTube is running beside it.

If you download this program for yourself, the starter helps you create **your own** Telegram bot. Eochu's chat does not control your copy.

## What the free version does

The free report quotes useful speech and needs no artificial intelligence account. If you connect an artificial intelligence program of your own, it can choose different subjects within each episode, say why each matters, and put your sceptical reading beneath each podcast. A suspected motive stays a possibility until checked against records or direct testimony. The maker's program is not available to other users.

## For people who want to run it by hand

The starter runs `app.py serve` and checks once a minute whether the morning report is due. The private page controls podcasts, episode counts, points per podcast, popular picks, delivery time and previews. It searches the public Apple Podcasts directory or uses an episode-list address you paste. It saves the order of Apple's Ireland popular-episode list each evening; this is a changing ranking, not a count of listeners. If yesterday's list was not saved because the program was off, it clearly labels the current day's list instead. It reads published speech text when available; otherwise your computer listens to temporary episode audio and turns it into words. The audio is deleted after reading, while recognised speech is kept in `data/` for later reports. An episode with no readable speech is left out. After an episode is sent, it is removed from the page and never chosen for another report.

You can also run `python3 app.py daily` from a scheduled task if you prefer. The starter keeps your bot code and chat number in the private `data/telegram.json` file, which is excluded from GitHub. A manual setup may instead set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` in its environment. The optional text AI uses `PODCAST_MODEL_URL`, `PODCAST_MODEL`, and if needed `PODCAST_MODEL_KEY`.

This version reads English speech. Longer episodes take longer to process. The report describes what speakers said; it does not independently prove their claims.

To put your own settings page on a public website, put it behind HTTPS and forward a private path to this app. Set `PODCAST_BASE` to that path. Keep `data/` and its private opening key out of public files.
