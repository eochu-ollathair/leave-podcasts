# Leave the Podcasts — Get the information in three lines

![A microphone and long podcast sound wave becoming three short lines](assets/github-hero.png)

Choose podcasts and get three short Telegram lines instead of listening for hours. Search by show name, choose how many recent episodes to read, preview the report, and send one yourself.

## Download the program

**[Download Leave the Podcasts here](https://eochu.app/leave-podcasts/source.zip).** It downloads one ZIP file. Open it, then read **[START_HERE.md](START_HERE.md)** inside. The starter walks you through Telegram and opens your private settings page. You do not need GitHub's Branch button.

This version runs on a Mac or Linux computer with Python 3.10 or newer. It is not a phone app, and the Windows starter is not ready yet. The computer must stay on to send the morning report.

The free report quotes useful speech and needs no AI account. If you connect your own text AI, it can combine claims, say why they matter, and put your sceptical angle at the end. A suspected motive stays a possibility unless there is evidence. The maker's own AI is not available to other users.

## For people who want to run it by hand

The starter runs `app.py serve` and checks once a minute whether the morning report is due. The private page controls podcasts, episode counts, delivery time and previews. It searches the public Apple Podcasts directory or uses a feed address you paste. It reads published transcripts when available; otherwise a local speech recogniser reads temporary episode audio. The audio is deleted after reading, while recognised speech is kept in `data/` for later reports. An episode with no readable speech is left out.

You can also run `python3 app.py daily` from a scheduled task if you prefer. The starter keeps your bot code and chat number in the private `data/telegram.json` file, which is excluded from GitHub. A manual setup may instead set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` in its environment. The optional text AI uses `PODCAST_MODEL_URL`, `PODCAST_MODEL`, and if needed `PODCAST_MODEL_KEY`.

This version reads English speech. Longer episodes take longer to process. The report describes what speakers said; it does not independently prove their claims.

To put your own settings page on a public website, put it behind HTTPS and forward a private path to this app. Set `PODCAST_BASE` to that path. Keep `data/` and its private opening key out of public files.
