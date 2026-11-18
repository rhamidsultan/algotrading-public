# Setting up your own server

This guide takes a new person from nothing to their own trading server and computer (Mac or Windows). When it's done they have:

- **The dashboard:** the portfolio, paper and live trading, the Data pages, and the phone app.
- **Price recording:** the server records prices after each close.
- **Algo Lab on your computer** for backtests.

Everything here uses your own accounts. Nothing in the repo points at anyone else's server. Allow about an hour.
US stocks (IBKR) is optional: see step 8.

## Mac or Windows?

Steps 1–8 happen in the Lightsail console and on the server, so they are the same on both. Only step 2 (opening the
server's terminal) and step 9 (Algo Lab on your computer) differ. Follow your column:

| | **Mac** | **Windows 10 / 11** |
|---|---|---|
| Step 2: the server's terminal | Terminal app ([2a](#2a-on-a-mac)) | PowerShell ([2b](#2b-on-windows)), or the browser button on either |
| Step 9: Algo Lab and the launchers | Everything works ([9a](#9a-on-a-mac)) | **Full:** in WSL, Ubuntu inside Windows ([9b](#9b-on-windows-everything-through-wsl-recommended)). **Backtests only:** double-click files ([9c](#9c-on-windows-backtests-only-no-wsl)) |
| What doesn't work | — | `ibkr-login` (it uses Mac Screen Sharing); approve IBKR logins in IBKR Mobile instead |

## What you need

| | |
|---|---|
| A copy of this repo on GitHub | Your own private repo with the code in it. The person sharing it runs `bash deploy/make-share-copy.command`: it pushes a clean copy into your empty repo, or gives you a file to push yourself. The server updates from the repo you clone in step 3. |
| An AWS Lightsail account | A server with 2 GB of memory (about US$12 a month). 1 GB works without US stocks. |
| A Zerodha account with Kite Connect | A Kite Connect app at <https://developers.kite.trade> (a paid subscription). It gives the API key and secret. |
| A Mac or a Windows 10/11 PC | For Algo Lab and the launchers. Python 3.10 or newer (step 9 says how to get it). |
| *(optional)* A web address | For HTTPS, which the phone app needs. A free one from duckdns.org works. |
| *(optional)* An Interactive Brokers account | Only for US stocks. |

## 1. Make the server (Lightsail console)

1. **Create instance:**
   - Linux/Unix, OS only, **Ubuntu 24.04 LTS**, the 2 GB plan.
   - Pick the region nearest to you; Mumbai (ap-south-1) is closest to the NSE.
2. **Networking › Create static IP**, attached to the instance. Its address is the server's address from now on.
3. **Networking › IPv4 firewall**, add rules:
   - TCP **5010**: the dashboard.
   - TCP **8080**: the morning Zerodha login page.
   - SSH (22) is already there.
4. **Account › SSH keys:** download the default key (a `.pem` file) into your Downloads folder.

## 2. Connect to the server

On any computer, the **Connect using SSH** button in the Lightsail console opens the server's terminal in the
browser; that is enough for steps 3–8. To use your own terminal instead:

### 2a. On a Mac

In Terminal (use your key's file name and the static IP):

```
chmod 600 ~/Downloads/LightsailDefaultKey-*.pem
ssh -i ~/Downloads/LightsailDefaultKey-*.pem ubuntu@<server address>
```

### 2b. On Windows

In **PowerShell** (Start menu › type PowerShell). `ssh` is built into Windows 10 and 11. First make the key
private to you; Windows' ssh refuses a key other users can read:

```
$key = "$HOME\Downloads\LightsailDefaultKey-ap-south-1.pem"
icacls $key /inheritance:r /grant:r "${env:USERNAME}:R"
ssh -i $key ubuntu@<server address>
```

Change the file name on the first line to your key's name.

## 3. Get the code onto the server (once)

On the server, make a read-only key for GitHub and show it:

```
sudo mkdir -p /opt/algo-trading/secrets && sudo chmod 700 /opt/algo-trading/secrets
sudo ssh-keygen -q -t ed25519 -N "" -C algolab-server-read-only -f /opt/algo-trading/secrets/deploy_key
sudo cat /opt/algo-trading/secrets/deploy_key.pub
```

On GitHub, open your repo › **Settings › Deploy keys › Add deploy key**:

- Paste the line that `cat` printed.
- Leave **Allow write access** unticked.

Then, on the server (put in your GitHub user and repo name):

```
sudo apt-get update && sudo apt-get install -y git
sudo GIT_SSH_COMMAND="ssh -i /opt/algo-trading/secrets/deploy_key -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new" \
  git clone git@github.com:<owner>/<repo>.git /opt/algo-trading/src
```

The dashboard's **Update now** button uses this same copy and key later.

## 4. Install everything (one command, about 10 minutes)

```
sudo bash /opt/algo-trading/src/deploy/new_server.sh
```

It runs four steps in order and says what each does:

1. **Server basics:**
   - India time;
   - SSH by key only;
   - the firewall;
   - the morning Zerodha login page on port 8080.
2. **The portfolio dashboard** (port 5010). It makes a sign-in password and prints it at the end. The password is also saved in `/root/dashboard-first-run.txt`.
3. **The Algo Lab engine:**
   - paper and live sessions;
   - the price recording after each close (15:45 IST);
   - the margin checks.
4. **The phone app, extra logins, and the Update button.**

It is safe to run again; it keeps settings, passwords and data.

## 5. Connect Zerodha

1. Put the Kite Connect app's details in the server's settings:
   ```
   sudo nano /opt/trader/.env
   ```
   Fill in `KITE_API_KEY`, `KITE_API_SECRET` and `KITE_USER_ID` (your Zerodha client ID). Save with Ctrl+O, Enter, then Ctrl+X.
2. Restart the services so they read the keys:
   ```
   sudo systemctl restart trader-auth trader-app algolab-paper
   ```
3. In the Kite developer console, set the app's **Redirect URL** to `http://<server address>:5010/callback`. Use `https://<your.domain>/callback` once you have done step 7.
4. **Every trading morning,** log in once:
   - open the dashboard and tap the Zerodha login, or open `http://<server address>:8080/`;
   - finish Zerodha's login and 2FA.

   Zerodha logins end each night. The engine and the price recording use the morning login and never log in by themselves.

## 6. First sign-in, and the Update button

1. Open `http://<server address>:5010/` and sign in as `admin` with the password from step 4.
2. Change the password: tap **Change password** on the Profile page, if it shows there. Otherwise run `sudo nano /opt/trader/config.json`, change `dashboard_password`, and run `sudo systemctl restart trader-app`. Then delete the note:
   ```
   sudo rm /root/dashboard-first-run.txt
   ```
3. Open **Profile › Server version** and tap **Check**. It should say it checked GitHub; the deploy key from step 3 is already in place.
4. From now on, after you merge a change on GitHub, tap **Update now**. The server:
   - pulls the merged code;
   - backs up the running version;
   - checks that the new version starts, and puts the old one back if it doesn't.

**Live trading** stays off until you turn it on yourself on the Live pages. Paper sessions use real prices and never place orders.

## 7. Optional: your own web address with HTTPS

The phone app (install to home screen, Face ID, notifications) needs HTTPS.

1. Point a name at the server's static IP: a DNS "A" record, or a free duckdns.org name.
2. In Lightsail › Networking, allow TCP **80** and **443**.
3. On the server:
   ```
   sudo bash /opt/algo-trading/src/deploy/add_https.sh <your.domain>
   ```
   It gets a free certificate that renews by itself, and checks the address works.
4. Change the Kite redirect URL to `https://<your.domain>/callback`.
5. You can now remove the Lightsail rule for 5010.

## 8. Optional: US stocks (Interactive Brokers)

Skip this if you have no IBKR account. The dashboard hides **US** and the Summary's US card until it is installed.

```
sudo bash /opt/algo-trading/src/deploy/add_us_stocks.sh
```

- It asks for your IBKR user name and password. They are kept only on the server, in `~/ibkr/.env`.
- It runs IB Gateway in Docker, **read-only**: the gateway cannot place orders and is reachable only inside the server.
- It installs the sync that reads your holdings every minute in US market hours, and adds the US page.
- Approve the login in **IBKR Mobile** when it asks. IBKR asks again about once a week. On the Mac, `server/portfolio/server/ibkr/mac/ibkr-login` opens the gateway's screen if a login gets stuck.

Needs a 2 GB server.

## 9. Algo Lab and the launchers on your computer

### 9a. On a Mac

If `git` is missing, macOS offers to install it the first time you type it. Get Python from <https://www.python.org/downloads/> if setup says it is missing.

```
git clone https://github.com/<owner>/<repo>.git ~/Trading/algotrading
cd ~/Trading/algotrading
bash setup_mac.sh            # once: Python packages, a few minutes
bash start_dashboard.sh      # Algo Lab (backtests) in the browser
```

The first time you open a launcher in `deploy/`, it asks for two things and saves them in `~/.algo-trading/server.env` on the Mac only:

- the server's address;
- the `.pem` key from step 1.

The launchers:

| Launcher | What it does |
|---|---|
| `bash deploy/get-server-data.command` | Copies the prices the server recorded into your computer's `data/`, for backtests. |
| `bash deploy/send-data-to-server.command` | Copies your computer's data to the server. It refuses in market hours. |
| `bash deploy/make_server_update.sh && bash deploy/update-server.command` | Updates the server from your computer. Use it when the Update button can't, for example before the deploy key is added. |

### 9b. On Windows: everything, through WSL (recommended)

WSL runs Ubuntu inside Windows, so Algo Lab and the launchers work as on a Mac (all but `ibkr-login`, which needs Mac Screen Sharing).

1. **Install WSL once.** Open PowerShell **as administrator** (right-click › Run as administrator), run the line below, then restart the PC:
   ```
   wsl --install
   ```
2. **Open Ubuntu** from the Start menu. The first time, it asks you to choose a user name and password for Ubuntu.
3. **Install what Algo Lab needs,** in the Ubuntu window:
   ```
   sudo apt update && sudo apt install -y git python3-venv python3-pip rsync
   ```
4. **Copy the server key into Ubuntu.** `C:` is `/mnt/c` there; put your Windows user name and the key's file name in:
   ```
   mkdir -p ~/.ssh && cp /mnt/c/Users/<windows user>/Downloads/LightsailDefaultKey-*.pem ~/.ssh/
   chmod 600 ~/.ssh/*.pem
   ```
   The key must be in `~/.ssh`: Windows folders under `/mnt/c` can't be made private, and ssh refuses such a key.
5. **Get Algo Lab** (same commands as on a Mac):
   ```
   git clone https://github.com/<owner>/<repo>.git ~/Trading/algotrading
   cd ~/Trading/algotrading
   bash setup_mac.sh
   bash start_dashboard.sh
   ```
   If no browser opens, open <http://localhost:8501> in Windows.
6. **The launchers** work as in the 9a table, run with `bash deploy/…` in the Ubuntu window. The first one asks for the server's address and the key: enter `~/.ssh/<key name>.pem`.

To reach the files from Windows Explorer, type `\\wsl$\Ubuntu\home\<ubuntu user>\Trading` in the address bar.

### 9c. On Windows: backtests only (no WSL)

For running backtests without copying data to or from the server, for example with data from a friend's bundle:

1. Install Python 3.12 from <https://www.python.org/downloads/>. On the first installer screen, tick **Add python.exe to PATH**.
2. Get the code:
   - with Git for Windows: `git clone https://github.com/<owner>/<repo>.git`;
   - or on GitHub: **Code › Download ZIP**, then unzip it.
3. In the folder, double-click **`setup.bat`** once, then **`start_dashboard.bat`** each time. The second one opens the dashboard in the browser.

The `deploy/` launchers don't run this way, because they need `bash` and `rsync`. Use 9b for them.

## When something is wrong

| What to check | Command (on the server) |
|---|---|
| Is everything running? | `systemctl status trader-app trader-auth algolab-paper --no-pager` |
| The dashboard's log | `sudo journalctl -u trader-app -n 50` |
| The engine's log | `sudo journalctl -u algolab-paper -n 50` |
| Last price recording | `sudo journalctl -u algolab-collect -n 30` (also on the Data page) |
| Last update from GitHub | Profile › Server version, or `cat /opt/algolab/runtime/self-update.log` |

To remove Algo Lab and restore the plain dashboard: `sudo bash /opt/algo-trading/src/deploy/uninstall_server.sh`.

## Where things live

| Path | What it holds |
|---|---|
| `/opt/trader` | The dashboard. Its keys are in `.env` and `config.json`; holdings history is in `portfolio.db`. |
| `/opt/algolab` | The engine: `data/` (recorded prices), `runtime/` (paper and live sessions), `config.yaml` (market settings). |
| `/opt/algo-trading/src` | The copy of the repo the Update button pulls. |
| `/opt/algo-trading/secrets` | The deploy key. |
| `/opt/algo-trading/repo` | The name of the repo it pulls from. |
| `~/ibkr` | US stocks only: IB Gateway and the sync. |
| `~/.algo-trading/server.env` *(your computer: the Mac, or Ubuntu in WSL)* | Which server and key the launchers use. |
