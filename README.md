# Circuit Watch

A website that scans every NSE and BSE stock each evening, finds the ones that have been closing at their
upper price limit day after day, and flags signs that the run may be breaking. It also measures, from
history, how often each warning sign was followed by a real drop.

The scan runs on GitHub's servers for free. You only open a web address. Nothing runs on your computer.

**Status: not yet tested on the real exchange files.** The code was tested end to end on fabricated files
in the same formats as the exchanges' files, but the first run against the real ones may need a small fix.
If it fails, see "If something goes wrong" below and send the message to Claude.

---

## Set it up (about 15 minutes, once)

1. **Make a free GitHub account** at github.com, if you don't have one.

2. **Create a repository.** Click the **+** at the top right, then **New repository**.
   Name it `circuit-watch`. Choose **Public** (GitHub's free website hosting needs a public repository).
   Leave everything else alone and click **Create repository**.
   The website will be public: anyone with the address can open it. It contains only market data, nothing about you.

3. **Upload the files.** Unzip the download. On the new repository page click **uploading an existing file**.
   Drag in these from the unzipped folder: `scan.py`, `requirements.txt`, `README.md`, `.gitignore` (if you
   can see it), and the whole `site` folder. Click **Commit changes**.

4. **Add the daily job.** Click **Add file**, then **Create new file**. In the name box type exactly
   `.github/workflows/daily.yml` (typing the slashes makes the folders). Open `daily-workflow.yml` from the
   unzipped folder in any text editor, copy everything, paste it into the big box, and click **Commit changes**.

5. **Turn on the website.** Go to **Settings**, then **Pages**. Under **Build and deployment**, set
   **Source** to **GitHub Actions**.

6. **Run it the first time.** Click the **Actions** tab, choose **Daily scan** on the left, click
   **Run workflow**, then the green **Run workflow** button. The first run downloads about a year of exchange
   files and can take 15 to 40 minutes. Later runs take a few minutes.

7. **Open your site.** When the run turns green, your address is
   `https://YOUR-GITHUB-USERNAME.github.io/circuit-watch/`. Bookmark it. On an iPhone you can use
   Share, then Add to Home Screen.

After this it updates by itself every weekday at about 7 pm IST.

---

## If something goes wrong

Open the **Actions** tab, click the failed run, click the step with the red cross and read the last lines.

- **"the exchange refused or timed out"**: the exchanges sometimes block GitHub's servers. Run it on your own
  computer instead (below), or wait a day and run it again, since blocks are often temporary.
- **"could not read the file (unrecognised file layout...)"**: an exchange changed its file format.
  Copy the message and send it to Claude. It needs one small change.
- **"only N sessions of data were available"**: not enough days could be downloaded. Run it again.
- **The site shows "Could not load the scan results"**: the first run has not finished or failed. Check Actions.
- **GitHub says an action version is outdated**: tell Claude which one.

### Run it on your own Mac instead

```
cd ~/Downloads/circuit-watch
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python scan.py
cd site
python3 -m http.server 8000
```

Then open http://localhost:8000 in your browser. Run `python scan.py` again each evening to refresh.
The first run is slow because it downloads a year of files. They are saved in a `cache` folder after that.

---

## What it does

Each evening `scan.py`:

1. Downloads the day's price file from NSE and from BSE and saves it.
2. Marks every stock-day that closed at its upper or lower price limit (2%, 5%, 10% or 20%).
3. Keeps stocks with a run of 5 or more upper-circuit closes that is still going or ended within 15 sessions.
4. Scores each on warning signs: the streak ending, closing below an upper circuit it touched, a volume
   spike, a widened trading range, a fall, a drop in delivery share (NSE), a lower-circuit lock, no trades in
   the latest session, and thin trading.
5. Replays the same rules over the saved history and records what happened over the next 5 sessions.
6. Writes `site/data.json`, which `site/index.html` displays.

Change the settings by editing the line `python scan.py --out site/data.json` in the workflow, for example
`--min-streak 8` or `--min-price 20`. Run `python scan.py --help` for the options.

## What it cannot see

- The order book, which is the best clue to a coming lock. It is not in the exchanges' end-of-day files.
- Overnight news. A stock that opens at its lower circuit gives no earlier warning in daily data.
- Stocks that did not trade. They are absent from the files, so a locked stock shows up only as having vanished.
- Surveillance listings (ASM, GSM), price band changes and bulk deals. Check the exchanges for these.

The point values in `evaluate()` in `scan.py` are a first guess. The table on the website shows how each sign has
actually performed, and the values should be changed to match once you have real results.

This is information from public files, not investment advice.

## Testing without internet

```
python tests/make_fixture.py /tmp/fixture
python scan.py --offline-dir /tmp/fixture --cache /tmp/cache --out site/data.json --end-date 2026-11-13
```

The fixture makes up random stocks, so its results mean nothing for real trading.
