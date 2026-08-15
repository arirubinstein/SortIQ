# Trainer PC setup

The sorting machine (the Pi) deliberately does **not** train — it runs an
inference-only runtime. Training runs on a PC on the same network: the PC
mirrors the machine's dataset over HTTP, retrains the **embedding model**
from that snapshot, and installs the new generation (model + exemplar
gallery) back on the machine, which hot-reloads it. Day-to-day dataset
growth doesn't even need that — new classes and photos take effect with a
gallery rebuild, no training at all.

Since you already drive the machine from a browser on this PC, the whole
workflow is two tabs: the machine's app for collecting and sorting, and
`http://localhost:5000` for training.

## One-time setup

### Windows 11

1. Install **Python 3.12** — not the newest version python.org offers.
   TensorFlow ships wheels for a limited range of Python versions, and the
   latest Python is usually ahead of it ("could not find a distribution
   for tensorflow" means exactly this).

   Download and run this installer:
   <https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe>
   — check **"Add python.exe to PATH"** during install. Other Pythons
   already on the PC are fine; they can coexist.
   (If you use the Python Install Manager instead: `py install 3.12`.)
2. Get the SortIQ repo (Git or a source zip). A GitHub zip extracts to a
   folder named `SortIQ-master` — and Windows "Extract All" often nests it
   (`SortIQ-master\SortIQ-master`). Open a terminal **in the folder where
   `dir` shows `requirements.txt`**; every command below runs from there.
3. ```bat
   py -3.12 -m venv .venv
   .venv\Scripts\pip install -r requirements.txt
   ```
   (`py -3.12` matters: plain `python` may resolve to a newer install.
   If `py -3.12` isn't accepted, use `py -V:3.12`.) The TensorFlow
   download is a few hundred MB — give it a few minutes.
4. Start the app:
   ```bat
   .venv\Scripts\python webui\server.py --port 5000
   ```
   The first launch may trigger a Windows Firewall prompt — allow access
   on private networks so the trainer can reach the machine.
5. Browse to `http://localhost:5000` → **Train**.
6. **Recommended — make it hands-free:** in File Explorer, open the
   `tools` folder inside the SortIQ folder and double-click
   **`trainer_autostart_windows.bat`**. The trainer server then starts
   silently every time you log in (no console window), so steps 4–5
   never happen again: the machine's Train page simply finds the trainer
   whenever this PC is on. Undo any time by running the same file from a
   terminal with `remove`:
   `tools\trainer_autostart_windows.bat remove`.
7. **Also recommended — the watchdog:** double-click
   **`trainer_watchdog_windows.bat`** in the same folder. It installs a
   scheduled task that checks every 2 minutes and silently relaunches
   the trainer if it has stopped (background servers do occasionally
   die without a trace). Each revival is noted in `watchdog.log`, and
   the same file with `remove` uninstalls it. Autostart covers login;
   the watchdog covers the rest of the day.

Note on GPUs: recent TensorFlow has no native Windows GPU support, so
the trainer offers two ways to run a full retraining. **CPU** works out
of the box — an overnight job at low priority, the PC stays usable.
**GPU** cuts that to about an hour but needs a one-time WSL2 (Ubuntu)
setup with `pip install tensorflow[and-cuda]` in a venv at
`/opt/sortiq-gpu312`; once present, the Train page probes it at startup
and offers it as a choice. Day-to-day dataset growth needs neither —
gallery rebuilds are minutes on CPU.

### macOS / Linux

```sh
python3 -m venv .venv           # needs 3.10+; 3.12 recommended (TensorFlow
.venv/bin/pip install -r requirements.txt      # wheels lag new Pythons)
.venv/bin/python webui/server.py --port 5000
```

## Using it

1. On the Train page, set **Sorting machine** to the machine's address
   (e.g. `http://pisortiq.local:5000`) — or click **Find machine…**, which
   scans the local network and sets it in one click (immune to mDNS
   moods). "machine reachable ✓" confirms the link.
2. Click **Pull dataset**. Phases: dataset pull (incremental — only new
   or changed images transfer after the first run), then crop rebuild.
   Progress shows throughout.
3. To retrain, pick a device under **Train a new model** — GPU (about an
   hour, if the WSL sandbox is set up) or CPU (overnight, low priority) —
   and press **Start training**. The run trains a large teacher network
   on every crop, then distills the fast student the machine runs. The
   result is staged as a **candidate** with its bench numbers; press
   **Install** to archive the current generation (model and gallery
   together — restorable from the Train page) and push the new pair to
   the machine. A GPU failure is never papered over: the app asks
   whether to retry the GPU or run on CPU instead.

The trainer keeps a local mirror of the machine's dataset under
`calibers/<caliber>/<model>/` — deletions and renames on the machine
propagate on the next pull, so the mirror never resurrects removed classes.

## Keeping the trainer up to date

The trainer PC never needs the git repo after the first install — it
updates **from the machine**. Every `tools/pi_deploy.sh` run puts fresh
code on the machine; both installs expose a manifest of their deployable
code files (per-file SHA-256 rolled up into one digest — that digest is
the version, no git involved), and the two Train UIs compare them:

- the machine's **Train models** window and the trainer's own Train page
  both warn when the trainer's code doesn't match the machine's, and
  **training is blocked until it does** — a stale trainer rebuilds crops
  with drifted imaging code and silently trains models the machine can't
  reproduce at sort time (this is not hypothetical; it drifted twice
  before this existed);
- one click — **Update trainer from machine** — pulls exactly the files
  that differ, deletes what the machine no longer ships, verifies every
  byte against the manifest, and restarts the trainer into the new code.
  Config, the dataset mirror, trained models, logs, and the venv are
  never touched.

Two things to know:

- The very first check after installing this feature may report that
  *every* file differs — line endings (a Windows zip vs the machine's
  files) count as differences. One click normalizes everything; after
  that, updates are only ever the files that really changed.
- A trainer that is a **git checkout** (a dev box) refuses self-update on
  purpose: the machine must never overwrite uncommitted work. Update it
  with `git pull` instead.
