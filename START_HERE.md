# Start your own Leave the Podcasts

**Download one file:** [leave-podcasts-source.zip](https://eochu.app/leave-podcasts/source.zip). Open it and move its contents into a normal folder on your computer. You do not need GitHub's “Branch” button.

This version runs on a **Mac or Linux computer**. Your phone receives the messages, but the program runs on that computer. A Windows starter is not ready yet.

1. Install [Python](https://www.python.org/downloads/) if your computer does not already have version 3.10 or newer. Python is the free program needed to run Leave the Podcasts.
2. Open **Terminal** in the folder you just made. Type `python3 start.py` and press Enter. The starter prepares the program. The first time may take several minutes.
3. The starter tells you how to make your own Telegram bot through [BotFather](https://t.me/BotFather). It asks for the code BotFather gives you, then finds your Telegram conversation after you send your bot `/start`.
4. Your private settings page opens in your browser. Search for podcasts, add the ones you want, and choose how many popular Irish episodes to add. Press **Make preview**. When you are happy with it, turn on **Send to Telegram each morning** and choose a time.

The first report may take a while. When an episode has no written speech, the program downloads a free speech recogniser and turns the episode audio into text on your computer. It deletes the downloaded audio afterwards.

Leave the Terminal window open and keep the computer on for morning messages. To start again later, open Terminal in the same folder and type `python3 start.py` again. Your choices and Telegram details stay in the private `data` folder on that computer.

For yesterday's popular picks, leave the computer on the evening before. If it was off, the report names the day when the popular list was actually checked.

Reply to one podcast message with **kill that podcast** to stop that show. Send **add podcast** followed by the full show name to add one. Telegram confirms the change. If the search finds several similar shows, it asks for the full name.

**What the free report does:** it quotes useful things people actually said, separately for each podcast. To cover different subjects in an episode, explain why each claim matters and add your sceptical reading beneath that podcast, connect an artificial intelligence program that you run or pay for yourself. The basic report needs no such account.

**If something goes wrong:** copy the exact error shown in Terminal when asking for help. Never post your Telegram bot code or private page link.
